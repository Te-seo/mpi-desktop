import { Download, Loader2, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { call, iosTunnelInstall, iosTunnelStatus } from '../lib/ipc'
import type { IosTunnelStatus } from '../lib/ipc'
import { useMessages } from '../lib/i18n'

const TUNNEL_POLL_MS = 5000
// After an install request the elevated child task is still registering /
// starting tunneld in the background, so poll faster for a number of ticks.
const TUNNEL_POLL_FAST_MS = 1500
const FAST_POLL_TICKS = 20
// The CLI probe spawns a subprocess, so poll it slowly and only while missing.
const TOOL_POLL_MS = 15_000

interface IosPreflight {
  platform: string
  tool: string
  available: boolean
  version?: string | null
  error?: string
  installable?: boolean
}

interface IosReadinessBadgeProps {
  connectionMode: 'usb' | 'wireless'
  deviceConnectionType?: 'usb' | 'wireless' | null
  deviceSerial?: string | null
  /** Temperature waiting always uses pymobiledevice3 9.x, including on macOS.
   * Ordinary capture keeps its existing xcrun preflight when this is false. */
  temperatureGuardEnabled?: boolean
}

type Tone = 'ok' | 'wait' | 'off' | 'error'
type TunnelStartState = 'idle' | 'waiting' | 'failed'

const DOT_CLASS: Record<Tone, string> = {
  ok: 'bg-emerald-500',
  wait: 'bg-amber-500',
  off: 'bg-neutral-400 dark:bg-neutral-600',
  error: 'bg-red-500',
}

/**
 * iOS capture readiness for the toolbar: checks the pymobiledevice3 CLI and
 * the elevated RemoteXPC tunnel daemon, each with a one-click in-app fix
 * (pip install / UAC-elevated scheduled task) so the user never opens a shell.
 */
export function IosReadinessBadge({
  connectionMode,
  deviceConnectionType = null,
  deviceSerial = null,
  temperatureGuardEnabled = false,
}: IosReadinessBadgeProps) {
  const t = useMessages()
  const [tool, setTool] = useState<IosPreflight | null>(null)
  const [tunnel, setTunnel] = useState<IosTunnelStatus | null>(null)
  const [installingTool, setInstallingTool] = useState(false)
  const [installingTunnel, setInstallingTunnel] = useState(false)
  // Remaining fast-poll ticks after a tunnel setup request.
  const [fastPolls, setFastPolls] = useState(0)
  const [tunnelStartState, setTunnelStartState] = useState<TunnelStartState>('idle')
  const [autoTunnelAttempted, setAutoTunnelAttempted] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const connectedDeviceSerial = useRef<string | null>(null)
  // Consecutive "tunnel down" probe results. tunneld can briefly report down
  // while it is busy, so the pollers only commit a down status after two misses
  // in a row — otherwise the badge flickers 완료 -> 시작 중 -> 완료 and needlessly
  // re-runs the tunnel setup. Owned by the polling effects (refs read in render
  // are disallowed), so refreshTunnel stays ref-free.
  const tunnelDownStreak = useRef(0)
  const nextConnectedDeviceSerial = deviceSerial?.trim() || null

  const selectedTransport = connectionMode === 'wireless' || deviceConnectionType === 'wireless'
    ? 'wireless'
    : 'usb'

  const refreshTool = useCallback(async () => {
    try {
      setTool(
        await call<IosPreflight>(
          temperatureGuardEnabled
            ? 'ios_temperature_guard_preflight'
            : 'ios_preflight',
          {},
          45_000
        )
      )
    } catch {
      setTool(null)
    }
  }, [temperatureGuardEnabled])

  // Probes the tunnel and commits "running" immediately (good news is applied
  // at once). A "down" result is returned raw but NOT committed here — the
  // polling effects debounce it via tunnelDownStreak. Kept ref-free so it can
  // be reached from render (via the action button) without tripping the
  // refs-in-render lint rule.
  const refreshTunnel = useCallback(async () => {
    let next: IosTunnelStatus | null
    try {
      next = await iosTunnelStatus()
    } catch {
      next = null
    }

    if (next?.tunneldRunning) {
      setTunnel(next)
      if (!next.migrationRequired) {
        setTunnelStartState('idle')
        setFastPolls(0)
        setAutoTunnelAttempted(false)
        setError(null)
      }
    }
    return next
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshTool()
      void (async () => {
        const next = await refreshTunnel()
        if (!next?.tunneldRunning) {
          // First reading: show the down/unknown state right away.
          tunnelDownStreak.current = 1
          setTunnel(next)
        }
      })()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [refreshTool, refreshTunnel])

  const fastMode = fastPolls > 0
  useEffect(() => {
    const timer = setInterval(() => {
      void (async () => {
        const nextTunnel = await refreshTunnel()

        if (nextTunnel?.tunneldRunning && !nextTunnel.migrationRequired) {
          tunnelDownStreak.current = 0
        } else {
          // Commit "down" only after two consecutive misses so a transient
          // probe hiccup can't flicker the badge or re-trigger tunnel setup.
          tunnelDownStreak.current += 1
          if (tunnelDownStreak.current >= 2) setTunnel(nextTunnel)
        }

        if (tunnelStartState !== 'waiting') return

        if (nextTunnel?.tunneldRunning && !nextTunnel.migrationRequired) {
          setTunnelStartState('idle')
          setFastPolls(0)
          setError(null)
          return
        }

        setFastPolls((n) => {
          if (n <= 1) {
            setTunnelStartState('failed')
            setError(t.iosReadiness.tunnelSetupTimeout)
            return 0
          }
          return n - 1
        })
      })()
    }, fastMode ? TUNNEL_POLL_FAST_MS : TUNNEL_POLL_MS)
    return () => clearInterval(timer)
  }, [refreshTunnel, fastMode, tunnelStartState, t])

  // Once available the CLI doesn't vanish; only re-probe while missing.
  const toolMissing = !tool?.available
  useEffect(() => {
    if (!toolMissing) return
    const timer = setInterval(() => {
      void refreshTool()
    }, TOOL_POLL_MS)
    return () => clearInterval(timer)
  }, [refreshTool, toolMissing])

  async function installTool() {
    setInstallingTool(true)
    setError(null)
    try {
      setTool(
        await call<IosPreflight>(
          temperatureGuardEnabled
            ? 'ios_temperature_guard_install_tool'
            : 'ios_install_tool',
          {},
          900_000
        )
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setInstallingTool(false)
    }
  }

  const installTunnel = useCallback(async () => {
    setInstallingTunnel(true)
    setAutoTunnelAttempted(true)
    setError(null)
    setFastPolls(0)
    setTunnelStartState('waiting')
    try {
      const result = await iosTunnelInstall()
      if (!result.ok) {
        setTunnelStartState('failed')
        setFastPolls(0)
        setError(
          result.error === 'UAC prompt was declined'
            ? t.iosReadiness.uacDeclined
            : (result.error ?? t.iosReadiness.tunnelSetupFailed)
        )
      } else {
        const nextTunnel = await refreshTunnel()
        if (nextTunnel?.tunneldRunning && !nextTunnel.migrationRequired) {
          setTunnelStartState('idle')
          setFastPolls(0)
          setError(null)
        } else {
          setFastPolls(FAST_POLL_TICKS)
        }
      }
    } catch (err) {
      setTunnelStartState('failed')
      setFastPolls(0)
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setInstallingTunnel(false)
    }
  }, [refreshTunnel, t])

  useEffect(() => {
    if (nextConnectedDeviceSerial !== connectedDeviceSerial.current) {
      connectedDeviceSerial.current = nextConnectedDeviceSerial
      setAutoTunnelAttempted(false)
    }

    if (
      !nextConnectedDeviceSerial ||
      (tunnel?.tunneldRunning && !tunnel.migrationRequired)
    ) {
      return
    }

    if (
      autoTunnelAttempted ||
      installingTunnel ||
      tunnelStartState !== 'idle' ||
      !tool?.available ||
      !tunnel?.supported
    ) {
      return
    }
    const timer = window.setTimeout(() => {
      void installTunnel()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [
    autoTunnelAttempted,
    installingTunnel,
    tool?.available,
    nextConnectedDeviceSerial,
    installTunnel,
    tunnel?.supported,
    tunnel?.migrationRequired,
    tunnel?.tunneldRunning,
    tunnelStartState,
  ])

  // Ordinary macOS capture uses Xcode tools and has no tunnel badge. Temperature
  // guard mode is different: it requires pmd3 9.x on every OS, so keep missing /
  // unsupported-version status and its install action visible there.
  if (!temperatureGuardEnabled && tool?.platform === 'darwin') return null
  if (tool?.available && tunnel && !tunnel.supported) return null

  let tone: Tone
  let label: string
  let hint: string | undefined
  let action: { text: string; run: () => void; busy: boolean; icon: 'install' | 'shield' } | null = null

  if (!tool && !tunnel) {
    tone = 'off'
    label = t.iosReadiness.checkingReadiness
  } else if (tool && !tool.available) {
    tone = 'error'
    label = t.iosReadiness.toolMissingLabel
    if (tool.installable) {
      hint = t.iosReadiness.toolMissingHint
      action = { text: t.iosReadiness.installToolAction, run: installTool, busy: installingTool, icon: 'install' }
    } else {
      hint = t.iosReadiness.pythonMissingHint
    }
  } else if (tunnel?.migrationRequired) {
    tone = 'off'
    label = t.iosReadiness.tunnelStartNeededLabel
    hint = t.iosReadiness.tunnelReconfigureHint
    action = {
      text: t.iosReadiness.reconfigureAction,
      run: installTunnel,
      busy: installingTunnel,
      icon: 'shield',
    }
  } else if (tunnelStartState === 'failed') {
    tone = 'error'
    label = t.iosReadiness.tunnelStartFailedLabel
    hint = tunnel?.taskState
      ? t.iosReadiness.tunnelFailedHintWithState(tunnel.taskState)
      : t.iosReadiness.tunnelFailedHint
    action = { text: t.iosReadiness.autoSetupAction, run: installTunnel, busy: installingTunnel, icon: 'shield' }
  } else if (tunnel && !tunnel.tunneldRunning && !tunnel.taskInstalled) {
    tone = 'off'
    label = t.iosReadiness.tunnelOffLabel
    hint = t.iosReadiness.tunnelOffHint
    action = { text: t.iosReadiness.autoSetupAction, run: installTunnel, busy: installingTunnel, icon: 'shield' }
  } else if (
    tunnel &&
    !tunnel.tunneldRunning &&
    (tunnelStartState === 'waiting' || fastMode || installingTunnel)
  ) {
    tone = 'wait'
    label = t.iosReadiness.tunnelStartingLabel
    hint = t.iosReadiness.tunnelStartingHint
  } else if (tunnel && !tunnel.tunneldRunning) {
    tone = 'error'
    label = tunnel.taskLastResult && tunnel.taskLastResult !== 0
      ? t.iosReadiness.tunnelStartFailedWithCode(tunnel.taskLastResult)
      : t.iosReadiness.tunnelStartNeededLabel
    hint = tunnel.taskState
      ? t.iosReadiness.tunnelReconfigureHintWithState(tunnel.taskState)
      : t.iosReadiness.tunnelReconfigureHint
    action = { text: t.iosReadiness.reconfigureAction, run: installTunnel, busy: installingTunnel, icon: 'shield' }
  } else if (tunnel && tunnel.tunnels === 0) {
    tone = 'wait'
    label = t.iosReadiness.tunnelRunningLabel
    hint = t.iosReadiness.tunnelRunningHint
  } else if (tunnel) {
    tone = 'ok'
    label = selectedTransport === 'wireless'
      ? t.iosReadiness.wirelessReadyLabel(tunnel.tunnels)
      : t.iosReadiness.usbReadyLabel(tunnel.tunnels)
    hint = tool?.version ? t.iosReadiness.readyHintWithVersion(tool.version) : t.iosReadiness.readyHint
  } else {
    tone = 'off'
    label = t.iosReadiness.checkingReadiness
    hint = t.iosReadiness.checkingReadinessHint
  }

  return (
    <div
      className="inline-flex items-center gap-1.5 rounded-md border border-neutral-300 px-2 py-1 text-xs text-neutral-600 dark:border-neutral-700 dark:text-neutral-300"
      title={hint}
    >
      <span className={`h-2 w-2 rounded-full ${DOT_CLASS[tone]}`} aria-hidden />
      <span className="whitespace-nowrap">{label}</span>
      {action && (
        <button
          type="button"
          onClick={action.run}
          disabled={action.busy}
          className="inline-flex items-center gap-1 rounded bg-neutral-900 px-1.5 py-0.5 font-medium text-white transition-colors hover:bg-neutral-700 disabled:cursor-wait disabled:opacity-60 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300"
          title={hint}
        >
          {action.busy ? (
            <Loader2 size={12} className="animate-spin" />
          ) : action.icon === 'install' ? (
            <Download size={12} />
          ) : (
            <ShieldCheck size={12} />
          )}
          {action.busy ? (action.icon === 'install' ? t.iosReadiness.installingBusy : t.iosReadiness.configuringBusy) : action.text}
        </button>
      )}
      {error && <span className="max-w-48 truncate text-red-600 dark:text-red-400" title={error}>{error}</span>}
    </div>
  )
}
