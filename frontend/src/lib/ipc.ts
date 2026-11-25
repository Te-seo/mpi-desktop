// Typed wrapper around the contextBridge API exposed in preload.ts.
// Correlates each request to a unique id so multiple in-flight calls don't get crossed.

import type {
  DeleteSessionsResponse,
  ImportResponse,
  ListSessionsResponse,
  LoadAnalysesResponse,
} from './types'

declare global {
  interface Window {
    api: {
      send(channel: string, data?: unknown): void
      receive(channel: string, listener: (...args: unknown[]) => void): () => void
      invoke<T = unknown>(channel: string, data?: unknown): Promise<T>
      getPathForFile(file: File): string
      clipboard?: {
        writeText(text: string): Promise<void>
        writeImage(bytes: ArrayBuffer | Uint8Array): Promise<void>
      }
    }
  }
}

export function getDroppedFilePaths(files: FileList | File[]): string[] {
  return Array.from(files)
    .map((file) => window.api.getPathForFile(file))
    .filter((path) => path.length > 0)
}

export interface BackendResponse<T = unknown> {
  id: string | null
  ok: boolean
  result?: T
  error?: string
  traceback?: string
  // streaming events have no `id` and use `event` instead
  event?: string
  data?: unknown
  capture_id?: string
  guard_id?: string
  preview_id?: string
  seq?: number
  // Event-specific fields (temperature readings/state, capture errors, etc.)
  // stay typed by their feature event unions at the consumer boundary.
  [key: string]: unknown
}

type Pending = {
  resolve: (value: unknown) => void
  reject: (err: Error) => void
}

const pending = new Map<string, Pending>()
const eventListeners = new Set<(msg: BackendResponse) => void>()

let unsubscribe: (() => void) | null = null

function ensureSubscribed() {
  if (unsubscribe) return
  // The preload bridge is only present inside Electron. Outside it (e.g. a plain
  // browser opened for design review) degrade gracefully instead of crashing the
  // whole React tree on mount.
  if (typeof window === 'undefined' || !window.api) return
  unsubscribe = window.api.receive('from-python', (...args: unknown[]) => {
    const msg = args[0] as BackendResponse | undefined
    if (!msg) return

    if (msg.event) {
      eventListeners.forEach((cb) => cb(msg))
      return
    }

    const id = msg.id ?? null
    if (id && pending.has(id)) {
      const slot = pending.get(id)!
      pending.delete(id)
      if (msg.ok) {
        slot.resolve(msg.result)
      } else {
        slot.reject(new Error(msg.error ?? 'unknown backend error'))
      }
    }
  })
}

// Each Electron renderer has its own module instance/counter. Prefix ids with a
// per-window nonce so simultaneous first requests from two measurement windows
// cannot collide in the main-process ownership registry.
const rendererInstanceId = (() => {
  try {
    return globalThis.crypto.randomUUID()
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  }
})()
let counter = 0
function nextId() {
  counter += 1
  return `${rendererInstanceId}-${counter.toString(36)}`
}

export async function call<T = unknown>(
  command: string,
  payload?: Record<string, unknown>,
  timeoutMs = 60_000
): Promise<T> {
  ensureSubscribed()
  const id = nextId()
  const promise = new Promise<T>((resolve, reject) => {
    pending.set(id, {
      resolve: (v: unknown) => resolve(v as T),
      reject,
    })
    if (timeoutMs > 0) {
      setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id)
          reject(new Error(`backend timeout after ${timeoutMs}ms (cmd=${command})`))
        }
      }, timeoutMs)
    }
  })
  const dispatch = await window.api.invoke<{ status?: string }>('send-to-python', {
    id,
    command,
    payload: payload ?? {},
  })
  // The measurement engine (data-engine sidecar) isn't running — fail fast with
  // an actionable message instead of letting the request hang until the timeout.
  if (dispatch?.status === 'no-process') {
    pending.delete(id)
    // Stable sentinel; friendlyError() localizes it for display.
    throw new Error('engine-not-running')
  }
  return promise
}

export function onEvent(listener: (msg: BackendResponse) => void): () => void {
  ensureSubscribed()
  eventListeners.add(listener)
  return () => eventListeners.delete(listener)
}

export interface IosTunnelStatus {
  supported: boolean
  tunneldRunning: boolean
  tunnels: number
  taskInstalled: boolean
  taskState?: string | null
  taskLastResult?: number | null
  legacyTaskInstalled: boolean
  migrationRequired: boolean
}

export async function iosTunnelStatus(): Promise<IosTunnelStatus> {
  return await window.api.invoke<IosTunnelStatus>('ios-tunnel-status')
}

export async function iosTunnelInstall(): Promise<{ ok: boolean; error?: string }> {
  return await window.api.invoke<{ ok: boolean; error?: string }>('ios-tunnel-install')
}

export async function openFolder(): Promise<string | null> {
  return (await window.api.invoke<string | null>('open-folder-dialog')) ?? null
}

/** Native open dialog. Returns absolute paths (empty array when cancelled).
 *  Defaults to multi-select CSV for the session-library import flow. */
export async function openFiles(opts?: {
  multi?: boolean
  filters?: { name: string; extensions: string[] }[]
}): Promise<string[]> {
  if (typeof window === 'undefined' || !window.api) return []
  return (await window.api.invoke<string[]>('open-file-dialog', opts ?? {})) ?? []
}

// --- Session library commands (analysis dashboard) --------------------------

export async function listSavedSessions(
  directory?: string
): Promise<ListSessionsResponse> {
  return await call<ListSessionsResponse>(
    'list_saved_sessions',
    directory ? { directory } : {}
  )
}

export async function importSessionFiles(
  paths: string[],
  directory?: string
): Promise<ImportResponse> {
  return await call<ImportResponse>('import_session_files', {
    paths,
    ...(directory ? { directory } : {}),
  })
}

export async function deleteSavedSessions(paths: string[]): Promise<DeleteSessionsResponse> {
  return await call<DeleteSessionsResponse>('delete_saved_sessions', { paths })
}

export async function loadSessionAnalyses(
  paths: string[],
  ranges: import('./types').SessionAnalysisRange[] = []
): Promise<LoadAnalysesResponse> {
  // Analysis of up to 4 CSVs; a large session can take a moment to parse.
  return await call<LoadAnalysesResponse>('load_session_analyses', { paths, ranges }, 120_000)
}

/** Open another independent measurement window (its own device + capture). */
export async function openMeasurementWindow(): Promise<void> {
  if (typeof window === 'undefined' || !window.api) return
  await window.api.invoke('open-measurement-window')
}

export interface WindowFullscreenState {
  fullscreen: boolean
  revision: number
}

export async function getWindowFullscreen(): Promise<WindowFullscreenState> {
  if (typeof window === 'undefined' || !window.api) {
    return { fullscreen: false, revision: 0 }
  }
  return await window.api.invoke<WindowFullscreenState>('window-fullscreen-get')
}

export async function setWindowFullscreen(
  fullscreen: boolean,
  revision: number
): Promise<WindowFullscreenState> {
  if (typeof window === 'undefined' || !window.api) {
    return { fullscreen: false, revision }
  }
  return await window.api.invoke<WindowFullscreenState>('window-fullscreen-set', {
    fullscreen,
    revision,
  })
}

export function onWindowFullscreenChanged(
  listener: (state: WindowFullscreenState) => void
): () => void {
  if (typeof window === 'undefined' || !window.api) return () => undefined
  return window.api.receive('window-fullscreen-changed', (value: unknown) => {
    const state = value as Partial<WindowFullscreenState>
    if (
      state &&
      typeof state.fullscreen === 'boolean' &&
      typeof state.revision === 'number' &&
      Number.isSafeInteger(state.revision)
    ) {
      listener({
        fullscreen: state.fullscreen,
        revision: state.revision,
      })
    }
  })
}

export interface PostToEndpointResult {
  ok: boolean
  status?: number
  error?: string
}

/** POST a JSON payload to a user-configured endpoint via the Electron main
 * process (keeps cross-origin requests and any auth header out of the renderer).
 * Resolves with {ok:false} on any failure instead of throwing. */
export async function postToEndpoint(opts: {
  url: string
  payload: unknown
  headers?: Record<string, string>
  timeoutMs?: number
}): Promise<PostToEndpointResult> {
  if (typeof window === 'undefined' || !window.api) {
    return { ok: false, error: 'desktop-only' }
  }
  return await window.api.invoke<PostToEndpointResult>('post-to-endpoint', opts)
}

export async function saveFile(opts: {
  defaultPath?: string
  filters?: { name: string; extensions: string[] }[]
}): Promise<string | null> {
  return (
    (await window.api.invoke<string | null>('save-file-dialog', opts)) ?? null
  )
}
