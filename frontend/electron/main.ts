import { app, BrowserWindow, Menu, dialog, ipcMain, safeStorage, shell, webContents } from 'electron'
import { execFile, spawn } from 'node:child_process'
import type { ChildProcessWithoutNullStreams } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import path from 'node:path'
import { OPEN_PLAY_COMPANION, openOfficialCompanionPage } from './play-companion'
import { FullscreenIntent } from './fullscreen'
import { WindowOwnership } from './ownership'
import { runGracefulShutdown } from './shutdown'
import { CloudSync } from './cloud-sync'
import { CaptureAuthority } from './capture-authority'
import { CompanionCloudBridge, CompanionCloudCoordinator, isMainOnlyCompanionRequest } from './companion-cloud'

let cloudSync: CloudSync | null = null
let companionCloud: CompanionCloudCoordinator | null = null
let companionAuthState = ''
let accountChanging = false
let accountChangeQueue: Promise<unknown> = Promise.resolve()
const accountViews = new Set<number>()
const accountWaiters = new Map<number, () => void>()
const companionBridge = new CompanionCloudBridge((request) => sendToPython(request).status === 'sent')
const captureAuthority = new CaptureAuthority({
  userId: () => { const status = cloudSync?.status(); return !accountChanging && status?.signedIn && status.verified && !status.signingIn ? status.userId : null },
  unlimited: async () => (await cloudSync?.entitlement())?.time_limit_enabled === false,
  reserve: (id, seconds) => { if (!cloudSync) throw new Error('auth-required'); return cloudSync.reserveCapture(id, seconds) },
  start: reservation => { if (!cloudSync) throw new Error('auth-required'); return cloudSync.startCaptureReservation(reservation) },
  finish: (reservation, outcome) => cloudSync?.finishCapture(reservation, outcome) ?? Promise.resolve(),
  stop: (command, id) => { sendToPython({ id: `quota-stop-${id}`, command, payload: command === 'stop_capture' ? { capture_id: id } : { guard_id: id } }) },
})

// All open measurement windows. Each mounts its own renderer (its own
// LiveCaptureProvider) so it runs an INDEPENDENT measurement; the single Python
// sidecar already multiplexes concurrent captures by capture_id. The hard part is
// routing the sidecar's stdout back to the RIGHT window — see routeFromPython.
const windows = new Set<BrowserWindow>()
const closePermitted = new Set<number>()
const closeWaiters = new Map<number, { promise: Promise<void>; resolve(): void }>()

function changeAccount<T>(operation: () => Promise<T>): Promise<T> {
  const queued = accountChangeQueue.catch(() => {}).then(async () => {
    accountChanging = true
    captureAuthority.revoke()
    try {
      await Promise.all([...windows].filter(win => !win.isDestroyed() && accountViews.has(win.webContents.id)).map(win => new Promise<void>(resolve => {
        const id = win.webContents.id
        const timer = setTimeout(() => { accountWaiters.delete(id); captureAuthority.revoke(); resolve() }, 30_000)
        accountWaiters.set(id, () => { clearTimeout(timer); accountWaiters.delete(id); resolve() })
        win.webContents.send('prepare-account-change')
      })))
      return await operation()
    } finally { accountChanging = false }
  })
  accountChangeQueue = queued
  return queued
}

function prepareWindowClose(win: BrowserWindow): Promise<void> {
  if (win.isDestroyed() || closePermitted.has(win.webContents.id)) return Promise.resolve()
  const ownerId = win.webContents.id
  const existing = closeWaiters.get(ownerId)
  if (existing) return existing.promise
  let finish!: () => void
  const promise = new Promise<void>((resolve) => { finish = resolve })
  const timer = setTimeout(() => {
    // A broken renderer or sidecar must never leave a native window uncloseable.
    closePermitted.add(ownerId)
    closeWaiters.delete(ownerId)
    finish()
  }, 30_000)
  closeWaiters.set(ownerId, {
    promise,
    resolve() {
      clearTimeout(timer)
      closePermitted.add(ownerId)
      closeWaiters.delete(ownerId)
      finish()
    },
  })
  win.webContents.send('prepare-window-close')
  return promise
}

const fullscreenStates = new Map<number, FullscreenIntent>()

// Response routing. Renderer request ids include a per-window nonce (nextId in
// ipc.ts). We still unicast every correlated response so no other measurement
// window can resolve it or claim the resource returned by it.
const ownership = new WindowOwnership()

let pythonProcess: ChildProcessWithoutNullStreams | null = null
// Set on app.will-quit so the sidecar's `close` handler can tell an intentional
// shutdown from a crash (only a crash should surface an engine-error banner).
let appQuitting = false
const SIDECAR_SHUTDOWN_TIMEOUT_MS = 8_000
const SIDECAR_FORCE_KILL_TIMEOUT_MS = 4_000
let shutdownComplete = false
let shutdownPromise: Promise<void> | null = null
// Last known sidecar health, re-sent to the renderer once it finishes loading
// (the sidecar starts before the page mounts, so the first event can be missed).
let lastEngineStatus: { ok: boolean; error?: string } | null = null

interface EngineStatus {
  ok: boolean
  error?: string
}

/** Send to a specific webContents if it's still alive. Returns false if gone. */
function sendToWc(wcId: number, channel: string, payload: unknown): boolean {
  const wc = webContents.fromId(wcId)
  if (wc && !wc.isDestroyed()) {
    wc.send(channel, payload)
    return true
  }
  return false
}

/** Broadcast to every open window as a fallback when a specific target is gone. */
function broadcast(channel: string, payload: unknown): void {
  for (const win of windows) {
    if (!win.isDestroyed()) win.webContents.send(channel, payload)
  }
}

/** Route one parsed sidecar message to the correct window:
 *  - an id-correlated RESPONSE -> only the window that issued the request (ids are
 *    belongs only to its issuing renderer, so it is never broadcast normally);
 *  - a capture, guard, or preview event -> the window that owns its resource id;
 *  - anything else -> broadcast.
 *  An unknown/closed owner falls back to a broadcast: the renderer ignores any
 *  message whose request id isn't pending or whose resource id isn't its own, so
 *  a stray message is dropped, never mis-handled. */
function routeFromPython(parsed: unknown): void {
  if (companionBridge.consume(parsed)) return
  captureAuthority.consume(parsed)
  const msg = parsed as {
    id?: unknown
    result?: unknown
    event?: unknown
    guard_id?: unknown
    capture_id?: unknown
    preview_id?: unknown
  }
  const id = typeof msg.id === 'string' ? msg.id : null
  if (id) {
    const request = ownership.takeRequest(id)
    // Deliver the response only to its issuing window; other renderers must not
    // resolve the request or claim the returned guard/capture/preview resource.
    const result = msg.result as
      | { capture_id?: unknown; guard_id?: unknown; preview_id?: unknown }
      | undefined
    const captureId =
      result && typeof result.capture_id === 'string' ? result.capture_id : null
    const guardId = result && typeof result.guard_id === 'string' ? result.guard_id : null
    const previewId =
      result && typeof result.preview_id === 'string' ? result.preview_id : null

    // Bind before delivery: resolving a guard/preview create lets the renderer
    // activate immediately, and its first event must already have an owner.
    if (request.ownerId !== null && request.kind === 'capture' && captureId) {
      ownership.bindCapture(captureId, request.ownerId)
    }
    if (request.ownerId !== null && request.kind === 'guard' && guardId) {
      ownership.bindGuard(guardId, request.ownerId)
    }
    if (request.ownerId !== null && request.kind === 'preview' && previewId) {
      ownership.bindPreview(previewId, request.ownerId)
    }

    const delivered =
      request.ownerId !== null && sendToWc(request.ownerId, 'from-python', parsed)

    // A start_capture response carries the new capture_id. If the owner window is
    // still alive, bind the capture to it (for event routing + stop-on-close). If
    // it closed DURING the open handshake (its requestOwner entry was already
    // dropped by stopCapturesForWindow, or the send just failed), immediately stop
    // the just-created capture — otherwise its pump samples forever and the serial
    // stays reserved, bricking the device for the rest of the session.
    if (!delivered && request.kind === 'capture' && captureId) {
      ownership.unbindCapture(captureId)
      sendToPython({
        id: `orphan-stop-${captureId}`,
        command: 'stop_capture',
        payload: { capture_id: captureId },
      })
    }
    if (!delivered && request.kind === 'guard' && guardId) {
      ownership.unbindGuard(guardId)
      sendToPython({
        id: `orphan-guard-stop-${guardId}`,
        command: 'stop_temperature_guard',
        payload: { guard_id: guardId },
      })
    }
    if (!delivered && request.kind === 'preview' && previewId) {
      ownership.unbindPreview(previewId)
      sendToPython({
        id: `orphan-preview-stop-${previewId}`,
        command: 'stop_temperature_preview',
        payload: { preview_id: previewId },
      })
    }

    if (delivered) return
    broadcast('from-python', parsed)
    return
  }
  const guardId = typeof msg.guard_id === 'string' ? msg.guard_id : null
  const captureId = typeof msg.capture_id === 'string' ? msg.capture_id : null
  const previewId = typeof msg.preview_id === 'string' ? msg.preview_id : null
  if (msg.event && (guardId || captureId || previewId)) {
    const route = ownership.routeEvent(msg.event, guardId, captureId, previewId)
    if (route.orphanedCaptureId) {
      sendToPython({
        id: `orphan-trigger-stop-${route.orphanedCaptureId}`,
        command: 'stop_capture',
        payload: { capture_id: route.orphanedCaptureId },
      })
    }
    if (route.ownerId !== null) {
      if (sendToWc(route.ownerId, 'from-python', parsed)) return
      const orphanedAfterTransfer = ownership.failEventDelivery(msg.event, captureId)
      if (orphanedAfterTransfer) {
        sendToPython({
          id: `failed-route-stop-${orphanedAfterTransfer}`,
          command: 'stop_capture',
          payload: { capture_id: orphanedAfterTransfer },
        })
      }
    }
    broadcast('from-python', parsed)
    return
  }
  broadcast('from-python', parsed)
}

/** When a window closes, stop the resources it owned (so the engine releases the
 *  device and no orphaned pump thread keeps sampling) and drop its routing state. */
function stopCapturesForWindow(wcId: number): void {
  captureAuthority.closeOwner(wcId)
  const released = ownership.releaseWindow(wcId)
  for (const captureId of released.captureIds) {
    sendToPython({
      id: `winclose-stop-${captureId}`,
      command: 'stop_capture',
      payload: { capture_id: captureId },
    })
  }
  for (const guardId of released.guardIds) {
    sendToPython({
      id: `winclose-guard-stop-${guardId}`,
      command: 'stop_temperature_guard',
      payload: { guard_id: guardId },
    })
  }
  for (const previewId of released.previewIds) {
    sendToPython({
      id: `winclose-preview-stop-${previewId}`,
      command: 'stop_temperature_preview',
      payload: { preview_id: previewId },
    })
  }
}

function stopAllOwnedResources(): void {
  for (const win of Array.from(windows)) {
    if (!win.isDestroyed()) stopCapturesForWindow(win.webContents.id)
  }
}

// Tell the renderer whether the measurement engine (data-engine sidecar) is
// healthy, so it can show a friendly in-app banner instead of letting every
// backend call hang for 60s (or, worse, crashing the main process with a raw
// "spawn ... ENOENT" dialog). Stored so a late-loading window still gets it.
function notifyEngineStatus(status: EngineStatus): void {
  lastEngineStatus = status
  broadcast('fromMain', { type: 'engine-status', ...status })
}

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged

const PRELOAD_PATH = path.join(__dirname, 'preload.js')
const RENDERER_DIST = path.join(__dirname, '../dist/index.html')

function publishFullscreenState(
  win: BrowserWindow,
  intent: FullscreenIntent
): void {
  if (!win.isDestroyed() && !win.webContents.isDestroyed()) {
    win.webContents.send('window-fullscreen-changed', intent.snapshot())
  }
}

function reconcileFullscreenState(
  win: BrowserWindow,
  intent: FullscreenIntent
): void {
  if (win.isDestroyed()) return
  const reconciliation = intent.reconcile(win.isFullScreen())
  if (reconciliation.kind === 'reassert') {
    // A superseded native transition can still arrive after a rapid
    // true -> false request. Reassert the latest requested state and do not
    // publish the obsolete intermediate event.
    win.setFullScreen(reconciliation.state.fullscreen)
    return
  }
  publishFullscreenState(win, intent)
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1400,
    height: 900,
    show: true,
    // Hide the native menu bar (File/Edit/View/Window/Help). Combined with the
    // app-wide Menu.setApplicationMenu(null) below this removes the top strip
    // entirely rather than just collapsing it.
    autoHideMenuBar: true,
    webPreferences: {
      preload: PRELOAD_PATH,
      nodeIntegration: false,
      contextIsolation: true,
      // DevTools are only available while developing. In the shipped app they
      // are disabled outright so openDevTools() and the Chromium keyboard
      // shortcuts (F12 / Ctrl+Shift+I,J,C) become no-ops.
      devTools: isDev,
    },
  })
  windows.add(win)
  // Capture the webContents id now; it's needed in 'closed' after the contents
  // may already be torn down.
  const wcId = win.webContents.id
  const fullscreenIntent = new FullscreenIntent(win.isFullScreen())
  fullscreenStates.set(wcId, fullscreenIntent)
  const reconcileWindowFullscreen = () =>
    reconcileFullscreenState(win, fullscreenIntent)

  win.setMenuBarVisibility(false)
  win.on('enter-full-screen', reconcileWindowFullscreen)
  win.on('leave-full-screen', reconcileWindowFullscreen)

  if (isDev) {
    void win.loadURL(process.env.VITE_DEV_SERVER_URL ?? 'http://localhost:5173')
    win.webContents.openDevTools()
  } else {
    void win.loadFile(RENDERER_DIST)
    hardenAgainstDevTools(win.webContents)
  }

  // The sidecar can start (and fail) before the renderer subscribes to events,
  // so replay the latest engine status to THIS window once its page is ready.
  win.webContents.on('did-finish-load', () => {
    reconcileWindowFullscreen()
    if (lastEngineStatus) {
      win.webContents.send('fromMain', { type: 'engine-status', ...lastEngineStatus })
    }
  })

  win.on('close', (event) => {
    if (closePermitted.has(wcId)) return
    event.preventDefault()
    void prepareWindowClose(win).then(() => { if (!win.isDestroyed()) win.close() })
  })
  win.on('closed', () => {
    closeWaiters.get(wcId)?.resolve()
    closePermitted.delete(wcId)
    windows.delete(win)
    fullscreenStates.delete(wcId)
    // Stop captures this window owned so the engine releases the device and
    // doesn't keep an orphaned pump thread sampling forever.
    stopCapturesForWindow(wcId)
  })
  return win
}

// Belt-and-suspenders DevTools lockout for the packaged app. `devTools: false`
// already neuters the toggle, but we also swallow the well-known shortcuts and
// force-close DevTools if anything (an extension, a future menu) manages to
// open them, so there is no path to the inspector in production.
function hardenAgainstDevTools(contents: Electron.WebContents) {
  contents.on('before-input-event', (event, input) => {
    const key = (input.key ?? '').toLowerCase()
    const ctrlOrCmd = input.control || input.meta
    const isDevToolsCombo =
      key === 'f12' || (ctrlOrCmd && input.shift && (key === 'i' || key === 'j' || key === 'c'))
    if (isDevToolsCombo) event.preventDefault()
  })
  contents.on('devtools-opened', () => {
    contents.closeDevTools()
  })
}

function startPythonSidecar() {
  // windowsHide hides the spawned process's console window while retaining the
  // console-subsystem stdin/stdout pipes used by the JSON protocol.
  // PYTHONUTF8/PYTHONIOENCODING force the embedded Python interpreter to read
  // stdin as UTF-8 so non-ASCII paths (Korean, Chinese, ...) are not mangled
  // into surrogateescape sequences.
  const sidecarEnv = {
    ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    // Where the Python side looks for bundled helpers (e.g. the companion APK).
    // Dev: repo root (two levels up from frontend/dist-electron/). Prod: the
    // packaged app's resources/ dir (matches data-engine.exe / ios-tunnel.ps1).
    MPI_RESOURCES_DIR: isDev ? path.resolve(__dirname, '..', '..') : process.resourcesPath,
  }
  const spawnOptions = { windowsHide: true, env: sidecarEnv } as const

  // Stable sentinel code (not a message) sent to the renderer, which localizes
  // it via friendlyError() — replaces the raw Node "spawn ... ENOENT"
  // uncaught-exception dialog with a friendly, translated banner.
  const ENGINE_FAIL_MESSAGE = isDev ? 'engine-start-failed-dev' : 'engine-start-failed'

  try {
    if (isDev) {
      // Run via the installed mpi package (pip install -e ..).
      // Project root is two levels up from frontend/dist-electron/
      const repoRoot = path.resolve(__dirname, '..', '..')
      pythonProcess = spawn('python', ['-m', 'mpi.api'], {
        cwd: repoRoot,
        ...spawnOptions,
      })
    } else {
      // Bundled by electron-builder into resources/data-engine.exe
      const exePath = path.join(process.resourcesPath, 'data-engine.exe')
      // Guard the common failure mode (a packaging/AV issue removed the exe):
      // a missing target makes spawn emit an async 'error' event which, if
      // unhandled, crashes the whole main process. Fail soft with a friendly
      // banner instead and leave the rest of the app usable.
      if (!fs.existsSync(exePath)) {
        console.error('[mpi-api] data-engine.exe not found at', exePath)
        pythonProcess = null
        notifyEngineStatus({ ok: false, error: ENGINE_FAIL_MESSAGE })
        return
      }
      pythonProcess = spawn(exePath, [], spawnOptions)
    }
  } catch (err) {
    console.error('[mpi-api] failed to spawn sidecar:', err)
    pythonProcess = null
    notifyEngineStatus({ ok: false, error: ENGINE_FAIL_MESSAGE })
    return
  }

  // Async spawn failures (ENOENT/EACCES once the OS tries to exec) arrive here.
  // Without this listener Node rethrows them as an uncaught exception and
  // Electron pops the raw "A JavaScript error occurred in the main process"
  // dialog — exactly the window we are replacing with a friendly banner.
  pythonProcess.on('error', (err) => {
    console.error('[mpi-api] sidecar process error:', err)
    companionBridge.disconnect()
    pythonProcess = null
    notifyEngineStatus({ ok: false, error: ENGINE_FAIL_MESSAGE })
  })

  console.log(
    `[mpi-api] started (${isDev ? 'dev' : 'prod'}) pid=${pythonProcess.pid}`
  )
  notifyEngineStatus({ ok: true })

  // Line-buffered reader: tolerate JSON lines that span multiple stdout chunks.
  let stdoutBuffer = ''
  pythonProcess.stdout?.on('data', (chunk: Buffer) => {
    stdoutBuffer += chunk.toString('utf8')
    let newlineIndex = stdoutBuffer.indexOf('\n')
    while (newlineIndex !== -1) {
      const line = stdoutBuffer.slice(0, newlineIndex).trim()
      stdoutBuffer = stdoutBuffer.slice(newlineIndex + 1)
      if (line) {
        try {
          const parsed = JSON.parse(line)
          routeFromPython(parsed)
        } catch (e) {
          console.error('[mpi-api] non-JSON stdout line:', line, e)
        }
      }
      newlineIndex = stdoutBuffer.indexOf('\n')
    }
  })

  pythonProcess.stderr?.on('data', (chunk: Buffer) => {
    process.stderr.write(`[mpi-api stderr] ${chunk.toString('utf8')}`)
  })

  pythonProcess.on('close', (code) => {
    console.log(`[mpi-api] exited with code ${code}`)
    companionBridge.disconnect()
    pythonProcess = null
    // A non-zero exit while the app is still running means the engine crashed
    // (not an intentional quit) — surface it so the user isn't left with calls
    // that silently time out after 60s.
    if (!appQuitting && code) {
      notifyEngineStatus({ ok: false, error: 'engine-exited' })
    }
  })
}

// ---------------------------------------------------------------------------
// iOS tunnel (pymobiledevice3 tunneld) management — Windows only.
// iOS 17+ DVT services need a RemoteXPC tunnel. The tunnel daemon must run
// elevated, so we manage it through a logon scheduled task installed by
// scripts/ios-tunnel.ps1. The renderer gets a status probe plus a one-click
// install that surfaces a single UAC prompt — the user never opens a shell.

const TUNNELD_PROBE = { host: '127.0.0.1', port: 49151, timeoutMs: 1500 } as const
const TUNNEL_TASK_NAME = 'MPI iOS Tunnel'
const LEGACY_TUNNEL_TASK_NAME = 'PerfInspect iOS Tunnel'
let iosTunnelInstallPromise: Promise<{ ok: boolean; error?: string }> | null = null

interface IosTunnelStatus {
  supported: boolean
  tunneldRunning: boolean
  tunnels: number
  taskInstalled: boolean
  taskState?: string | null
  taskLastResult?: number | null
  legacyTaskInstalled: boolean
  migrationRequired: boolean
}

interface TunnelTaskInfo {
  installed: boolean
  state: string | null
  lastResult: number | null
}

function tunnelScriptPath(): string {
  if (isDev) {
    return path.resolve(__dirname, '..', '..', 'scripts', 'ios-tunnel.ps1')
  }
  return path.join(process.resourcesPath, 'ios-tunnel.ps1')
}

function normalizedTunnelKey(value: string): string {
  return value.trim().replace(/_/g, '-').toLowerCase()
}

function hasTunnelField(entry: Record<string, unknown>, names: string[]): boolean {
  const wanted = new Set(names.map(normalizedTunnelKey))
  return Object.entries(entry).some(([key, value]) => wanted.has(normalizedTunnelKey(key)) && value != null && value !== '')
}

function isActiveTunnelEntry(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const entry = value as Record<string, unknown>
  if (hasTunnelField(entry, ['rsd'])) return true
  return hasTunnelField(entry, ['tunnel-address', 'tunnelAddress', 'address', 'host', 'rsd-host']) &&
    hasTunnelField(entry, ['tunnel-port', 'tunnelPort', 'port', 'rsd-port'])
}

function countTunnels(payload: unknown): number {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return 0
  let count = 0
  for (const value of Object.values(payload as Record<string, unknown>)) {
    if (Array.isArray(value)) {
      count += value.filter(isActiveTunnelEntry).length
    } else if (isActiveTunnelEntry(value)) {
      count += 1
    }
  }
  return count
}

function probeTunneldOnce(): Promise<{ running: boolean; tunnels: number }> {
  return new Promise((resolve) => {
    const req = http.get(
      { host: TUNNELD_PROBE.host, port: TUNNELD_PROBE.port, path: '/', timeout: TUNNELD_PROBE.timeoutMs },
      (res) => {
        let body = ''
        res.on('data', (chunk: Buffer | string) => {
          body += chunk.toString()
        })
        res.on('end', () => {
          try {
            const parsed: unknown = JSON.parse(body)
            resolve({ running: true, tunnels: countTunnels(parsed) })
          } catch {
            resolve({ running: true, tunnels: 0 })
          }
        })
      }
    )
    req.on('timeout', () => {
      req.destroy()
      resolve({ running: false, tunnels: 0 })
    })
    req.on('error', () => resolve({ running: false, tunnels: 0 }))
  })
}

// tunneld answers in ~ms when healthy, but a momentary hiccup (it is busy
// (re)building a tunnel, or a localhost connect races) makes a single probe
// report "down". A lone false negative used to flip the readiness badge
// 완료 -> 시작 중 -> 완료 and even re-trigger the elevated tunnel install. Retry a
// few times and only conclude "down" when every attempt fails; a genuinely
// stopped tunneld refuses the connection immediately, so this stays fast.
async function probeTunneld(): Promise<{ running: boolean; tunnels: number }> {
  const attempts = 3
  for (let attempt = 0; attempt < attempts; attempt++) {
    const result = await probeTunneldOnce()
    if (result.running) return result
    if (attempt < attempts - 1) {
      await new Promise((r) => setTimeout(r, 200))
    }
  }
  return { running: false, tunnels: 0 }
}

function queryTunnelTaskInfo(taskNameValue = TUNNEL_TASK_NAME): Promise<TunnelTaskInfo> {
  const taskName = taskNameValue.replace(/'/g, "''")
  const command = [
    `$task = Get-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue`,
    "if (-not $task) { [pscustomobject]@{installed=$false; state=$null; lastResult=$null} | ConvertTo-Json -Compress; exit }",
    `$info = Get-ScheduledTaskInfo -TaskName '${taskName}'`,
    "[pscustomobject]@{installed=$true; state=$task.State.ToString(); lastResult=$info.LastTaskResult} | ConvertTo-Json -Compress",
  ].join('; ')
  return new Promise((resolve) => {
    execFile(
      'powershell.exe',
      ['-NoProfile', '-Command', command],
      { windowsHide: true },
      (err, stdout) => {
        if (err) {
          resolve({ installed: false, state: null, lastResult: null })
          return
        }
        try {
          const parsed = JSON.parse(stdout || '{}') as Partial<TunnelTaskInfo>
          resolve({
            installed: Boolean(parsed.installed),
            state: typeof parsed.state === 'string' ? parsed.state : null,
            lastResult: typeof parsed.lastResult === 'number' ? parsed.lastResult : null,
          })
        } catch {
          resolve({ installed: false, state: null, lastResult: null })
        }
      }
    )
  })
}

async function iosTunnelStatus(): Promise<IosTunnelStatus> {
  if (process.platform !== 'win32') {
    return {
      supported: false,
      tunneldRunning: false,
      tunnels: 0,
      taskInstalled: false,
      legacyTaskInstalled: false,
      migrationRequired: false,
    }
  }
  const [probe, task, legacyTask] = await Promise.all([
    probeTunneld(),
    queryTunnelTaskInfo(),
    queryTunnelTaskInfo(LEGACY_TUNNEL_TASK_NAME),
  ])
  return {
    supported: true,
    tunneldRunning: probe.running,
    tunnels: probe.tunnels,
    taskInstalled: task.installed,
    taskState: task.state,
    taskLastResult: task.lastResult,
    legacyTaskInstalled: legacyTask.installed,
    migrationRequired: legacyTask.installed,
  }
}

function iosTunnelInstall(): Promise<{ ok: boolean; error?: string }> {
  if (process.platform !== 'win32') {
    return Promise.resolve({ ok: false, error: 'iOS tunnel install is only supported on Windows' })
  }
  if (iosTunnelInstallPromise) return iosTunnelInstallPromise
  const script = tunnelScriptPath()
  const installPromise = new Promise<{ ok: boolean; error?: string }>((resolve) => {
    // The script self-elevates and waits for the elevated transaction. Keeping
    // this shared promise alive makes migration single-flight across all
    // measurement windows in this Electron process.
    void Promise.all([
      probeTunneld(),
      queryTunnelTaskInfo(),
      queryTunnelTaskInfo(LEGACY_TUNNEL_TASK_NAME),
    ]).then(([probe, task, legacyTask]) => {
      if (probe.running && task.installed && !legacyTask.installed) {
        resolve({ ok: true })
        return
      }
      execFile(
        'powershell.exe',
        ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-Install'],
        // The elevated script may wait up to three minutes for the global
        // migration mutex, then spend another 30 seconds probing the new API.
        // Keep Electron's cross-window single-flight alive beyond that entire
        // transaction so a second window cannot launch an overlapping UAC flow.
        { windowsHide: true, timeout: 360_000 },
        (err, _stdout, stderr) => {
          if (err) {
            const detail = (stderr || err.message || '').trim()
            const cancelled = /canceled|cancelled|operation was canceled/i.test(detail)
            resolve({
              ok: false,
              error: cancelled ? 'UAC prompt was declined' : detail || 'failed to launch installer',
            })
          } else {
            resolve({ ok: true })
          }
        }
      )
    })
  })
  iosTunnelInstallPromise = installPromise
  void installPromise.finally(() => {
    iosTunnelInstallPromise = null
  })
  return installPromise
}

function sendToPython(args: unknown): { status: 'sent' | 'no-process' } {
  if (pythonProcess?.stdin && !pythonProcess.stdin.destroyed) {
    try {
      pythonProcess.stdin.write(JSON.stringify(args) + '\n')
      return { status: 'sent' }
    } catch {
      return { status: 'no-process' }
    }
  }
  return { status: 'no-process' }
}

function waitForSidecarClose(target: ChildProcessWithoutNullStreams): Promise<boolean> {
  if (target.exitCode !== null || target.signalCode !== null) return Promise.resolve(true)
  return new Promise((resolve) => {
    const finish = (closed: boolean) => {
      target.removeListener('close', onClose)
      target.removeListener('error', onError)
      resolve(closed)
    }
    const onClose = () => finish(true)
    const onError = () => finish(false)
    target.once('close', onClose)
    target.once('error', onError)
  })
}

async function forceTerminateSidecarTree(
  target: ChildProcessWithoutNullStreams
): Promise<void> {
  try {
    target.stdin.end()
  } catch {
    /* ignore broken pipe */
  }
  if (target.exitCode !== null || target.signalCode !== null) return

  if (process.platform === 'win32' && target.pid) {
    await new Promise<void>((resolve) => {
      execFile(
        'taskkill.exe',
        ['/PID', String(target.pid), '/T', '/F'],
        { windowsHide: true, timeout: SIDECAR_FORCE_KILL_TIMEOUT_MS },
        () => {
          // taskkill can fail when the process exits between the check and the
          // command. A direct kill is a harmless final fallback in that race.
          if (target.exitCode === null && target.signalCode === null) {
            try {
              target.kill()
            } catch {
              /* already gone */
            }
          }
          resolve()
        }
      )
    })
    return
  }

  try {
    target.kill('SIGTERM')
  } catch {
    return
  }
  await new Promise((resolve) => setTimeout(resolve, 250))
  if (target.exitCode === null && target.signalCode === null) {
    try {
      target.kill('SIGKILL')
    } catch {
      /* already gone */
    }
  }
}

function ensureGracefulAppShutdown(): Promise<void> {
  if (shutdownPromise) return shutdownPromise
  appQuitting = true
  const target = pythonProcess

  shutdownPromise = (async () => {
    if (!target) {
      stopAllOwnedResources()
      return
    }
    const shutdownId = `electron-shutdown-${Date.now().toString(36)}`
    const result = await runGracefulShutdown({
      stopOwnedResources: stopAllOwnedResources,
      sendShutdown: () =>
        sendToPython({ id: shutdownId, command: 'shutdown', payload: {} }).status ===
        'sent',
      waitForExit: () => waitForSidecarClose(target),
      forceTerminate: () => forceTerminateSidecarTree(target),
      timeoutMs: SIDECAR_SHUTDOWN_TIMEOUT_MS,
    })
    console.log(`[mpi-api] app shutdown completed (${result})`)
    if (pythonProcess === target) pythonProcess = null
  })()
  return shutdownPromise
}

// Keep one desktop process so duplicate OS launches cannot start competing
// sidecars against the same connected devices. Additional measurements still
// use the in-app "new measurement window" action.
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    const win = BrowserWindow.getAllWindows()[0]
    if (win) {
      if (win.isMinimized()) win.restore()
      if (!win.isVisible()) win.show()
      win.focus()
    }
  })
}

app.whenReady().then(() => {
  if (!gotSingleInstanceLock) return

  // Drop the default application menu so the File/Edit/View/Window/Help strip
  // never renders. This also removes the menu's built-in DevTools accelerators.
  Menu.setApplicationMenu(null)
  app.setAppUserModelId('com.mpi.app')

  cloudSync = new CloudSync(app.getPath('userData'), {
    isEncryptionAvailable: () => safeStorage.isEncryptionAvailable() &&
      (process.platform !== 'linux' || safeStorage.getSelectedStorageBackend() !== 'basic_text'),
    encryptString: (value) => safeStorage.encryptString(value),
    decryptString: (value) => safeStorage.decryptString(value),
  }, (status) => {
    if (!status.signedIn || !status.verified || (companionAuthState && JSON.parse(companionAuthState)[2] !== status.userId)) captureAuthority.revoke()
    broadcast('cloud-status-changed', status)
    // Upload enable/disable does not grant or revoke a mobile login.
    const state = JSON.stringify([status.signedIn, status.signingIn, status.userId])
    if (state !== companionAuthState) {
      companionAuthState = state
      if (!status.signedIn || status.signingIn) companionCloud?.revoke()
      companionCloud?.sessionChanged()
    }
  }, undefined, undefined, {
    openExternal: (url) => shell.openExternal(url),
  })
  cloudSync.start()

  createWindow()
  startPythonSidecar()
  companionCloud = new CompanionCloudCoordinator(
    () => cloudSync?.companionSession() ?? Promise.resolve(null),
    (session) => companionBridge.sync(session),
  )
  companionCloud.start()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', (event) => {
  appQuitting = true
  companionCloud?.stop()
  if (shutdownComplete) return
  // Keep Electron alive just long enough for queued resource stops and the
  // backend's global shutdown cleanup to finish. A bounded fallback in
  // ensureGracefulAppShutdown prevents a wedged sidecar from hanging app quit.
  event.preventDefault()
  void Promise.all([...windows].map(prepareWindowClose))
    .then(() => ensureGracefulAppShutdown()).finally(() => {
    shutdownComplete = true
    app.quit()
  })
})

app.on('will-quit', () => {
  appQuitting = true
  cloudSync?.stop()
  companionBridge.disconnect()
})

ipcMain.handle('window-close-ready', (event) => {
  closeWaiters.get(event.sender.id)?.resolve()
  return { ok: true }
})

ipcMain.handle('account-view-ready', event => { accountViews.add(event.sender.id); return { ok: true } })
ipcMain.handle('account-change-ready', event => { accountWaiters.get(event.sender.id)?.(); return { ok: true } })

for (const channel of ['cloud-status', 'cloud-sign-in-google',
  'cloud-verify-session', 'cloud-entitlement', 'cloud-team-workspace', 'statistics-participation-get', 'statistics-participation-set',
  'cloud-cancel-sign-in', 'cloud-sign-out',
  'cloud-set-enabled', 'cloud-retry', 'cloud-enqueue'] as const) {
  ipcMain.handle(channel, (event, input: unknown) => {
    if (![...windows].some((win) => !win.isDestroyed() && win.webContents === event.sender) || !cloudSync)
      throw new Error('Cloud service unavailable')
    switch (channel) {
      case 'cloud-status': return cloudSync.status()
      case 'cloud-verify-session': return cloudSync.verifySession()
      case 'cloud-entitlement': return cloudSync.entitlement()
      case 'cloud-team-workspace': return cloudSync.teamWorkspace(input)
      case 'statistics-participation-get': return cloudSync.statisticsParticipation(input)
      case 'statistics-participation-set': return cloudSync.statisticsParticipation(input, true)
      case 'cloud-sign-in-google': {
        captureAuthority.revoke()
        companionCloud?.revoke()
        return changeAccount(() => cloudSync!.signInWithGoogle())
      }
      case 'cloud-cancel-sign-in': return cloudSync.cancelSignIn()
      case 'cloud-sign-out': {
        captureAuthority.revoke()
        companionCloud?.revoke()
        return changeAccount(() => cloudSync!.signOut())
      }
      case 'cloud-set-enabled': return cloudSync.setEnabled(input)
      case 'cloud-retry': return cloudSync.retry()
      case 'cloud-enqueue': return cloudSync.enqueue(input)
    }
  })
}

ipcMain.handle(OPEN_PLAY_COMPANION, (event) => openOfficialCompanionPage(
  [...windows].some((win) => !win.isDestroyed() && win.webContents === event.sender),
  !!event.senderFrame && event.senderFrame === event.sender.mainFrame,
  url => shell.openExternal(url),
))

ipcMain.handle('send-to-python', async (event, args) => {
  if (![...windows].some((win) => !win.isDestroyed() && win.webContents === event.sender)) throw new Error('Command unavailable')
  if (isMainOnlyCompanionRequest(args)) throw new Error('Command unavailable')
  let permitted
  try { permitted = await captureAuthority.authorize(args, event.sender.id) }
  catch (error) {
    const id = (args as { id?: unknown } | null)?.id
    if (typeof id === 'string') sendToWc(event.sender.id, 'from-python', { id, ok: false, error: error instanceof Error ? error.message : 'quota-unavailable' })
    return { status: 'denied' }
  }
  // Remember which window issued this request so the response (and, for
  // start_capture, the resulting capture's events) route back to it.
  const a = permitted
  if (a && typeof a.id === 'string') {
    ownership.rememberRequest(a.id, event.sender.id, a.command)
  }
  const result = sendToPython(permitted)
  captureAuthority.dispatched(permitted.id, result.status)
  return result
})

// Open another independent measurement window. The native menu is removed, so the
// in-app "새 측정 창" button drives this.
ipcMain.handle('open-measurement-window', async () => {
  const win = createWindow()
  win.focus()
  return { ok: true }
})

ipcMain.handle('window-fullscreen-get', (event) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  if (!win || win.isDestroyed()) return { fullscreen: false, revision: 0 }
  const intent =
    fullscreenStates.get(event.sender.id) ??
    new FullscreenIntent(win.isFullScreen())
  fullscreenStates.set(event.sender.id, intent)
  return intent.snapshot()
})

ipcMain.handle('window-fullscreen-set', (event, payload: unknown) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  if (!win || win.isDestroyed()) return { fullscreen: false, revision: 0 }
  const request = payload as { fullscreen?: unknown; revision?: unknown }
  if (
    !request ||
    typeof request.fullscreen !== 'boolean' ||
    typeof request.revision !== 'number' ||
    !Number.isSafeInteger(request.revision) ||
    request.revision < 0
  ) {
    throw new TypeError(
      'window-fullscreen-set requires { fullscreen: boolean, revision: integer }'
    )
  }
  const intent =
    fullscreenStates.get(event.sender.id) ??
    new FullscreenIntent(win.isFullScreen())
  fullscreenStates.set(event.sender.id, intent)
  const state = intent.request(request.fullscreen, request.revision)
  if (win.isFullScreen() !== state.fullscreen) {
    win.setFullScreen(state.fullscreen)
  } else {
    publishFullscreenState(win, intent)
  }
  return state
})

ipcMain.handle('ios-tunnel-status', async () => iosTunnelStatus())

ipcMain.handle('ios-tunnel-install', async () => iosTunnelInstall())

ipcMain.handle('open-folder-dialog', async (event) => {
  // Parent the dialog to the window that invoked it (not a single global window).
  const parent = BrowserWindow.fromWebContents(event.sender)
  const result = parent
    ? await dialog.showOpenDialog(parent, { properties: ['openDirectory'] })
    : await dialog.showOpenDialog({ properties: ['openDirectory'] })
  if (result.canceled || result.filePaths.length === 0) return null
  return result.filePaths[0]
})

ipcMain.handle(
  'open-file-dialog',
  async (event, opts: { multi?: boolean; filters?: Electron.FileFilter[] }) => {
    const parent = BrowserWindow.fromWebContents(event.sender)
    const properties: Array<'openFile' | 'multiSelections'> = ['openFile']
    if (opts?.multi !== false) properties.push('multiSelections')
    const filters = opts?.filters ?? [{ name: 'CSV', extensions: ['csv'] }]
    const result = parent
      ? await dialog.showOpenDialog(parent, { properties, filters })
      : await dialog.showOpenDialog({ properties, filters })
    if (result.canceled || result.filePaths.length === 0) return []
    return result.filePaths
  }
)

ipcMain.handle('save-file-dialog', async (event, opts: { defaultPath?: string; filters?: Electron.FileFilter[] }) => {
  const parent = BrowserWindow.fromWebContents(event.sender)
  const result = parent
    ? await dialog.showSaveDialog(parent, { defaultPath: opts?.defaultPath, filters: opts?.filters })
    : await dialog.showSaveDialog({ defaultPath: opts?.defaultPath, filters: opts?.filters })
  if (result.canceled || !result.filePath) return null
  return result.filePath
})

// POST a JSON payload to a user-configured endpoint. Running in the main process
// avoids renderer cross-origin restrictions. Best-effort: always resolves with
// {ok} so a transport failure cannot crash the capture flow.
ipcMain.handle(
  'post-to-endpoint',
  async (
    _event,
    opts: {
      url: string
      payload: unknown
      headers?: Record<string, string>
      timeoutMs?: number
    }
  ): Promise<{ ok: boolean; status?: number; error?: string }> => {
    const { url, payload, headers, timeoutMs = 30_000 } = opts ?? ({} as typeof opts)
    if (!url || typeof url !== 'string') return { ok: false, error: 'endpoint url required' }
    let target: URL
    try {
      target = new URL(url)
    } catch {
      return { ok: false, error: `invalid endpoint url: ${url}` }
    }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') {
      return { ok: false, error: `unsupported protocol: ${target.protocol}` }
    }
    const body = Buffer.from(JSON.stringify(payload), 'utf-8')
    const transport = target.protocol === 'https:' ? https : http
    return await new Promise((resolve) => {
      const req = transport.request(
        target,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': body.length,
            ...(headers ?? {}),
          },
          timeout: timeoutMs,
        },
        (res) => {
          res.resume() // drain so the socket is released
          const status = res.statusCode ?? 0
          if (status >= 200 && status < 300) resolve({ ok: true, status })
          else resolve({ ok: false, status, error: `HTTP ${status}` })
        }
      )
      req.on('timeout', () => {
        req.destroy()
        resolve({ ok: false, error: `endpoint timeout after ${timeoutMs}ms` })
      })
      req.on('error', (err) => resolve({ ok: false, error: err.message }))
      req.write(body)
      req.end()
    })
  }
)
