import {
  Download,
  FileSpreadsheet,
  FileText,
  Loader2,
  Maximize2,
  Minimize2,
  Play,
  Pause,
  Scissors,
  Plus,
  Square,
  X,
  Wifi,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { CloudPanel } from '../components/CloudPanel'
import { DeviceInfoCard } from '../components/DeviceInfoCard'
import { DeviceSelector } from '../components/DeviceSelector'
import { IosReadinessBadge } from '../components/IosReadinessBadge'
import { LiveCharts } from '../components/LiveCharts'
import { RenderPipelinePanel } from '../components/RenderPipelinePanel'
import { MetricVisibilityMenu } from '../components/MetricVisibilityMenu'
import { PackageSelector } from '../components/PackageSelector'
import { PlayCompanionSetup } from '../components/PlayCompanionSetup'
import { StatusBar } from '../components/StatusBar'
import { WirelessSetup } from '../components/WirelessSetup'
import { formatHms } from '../lib/format'
import { formatSessionRangeTime } from '../lib/sessionRangeTime'
import { friendlyError } from '../lib/friendlyError'
import { useMessages } from '../lib/i18n'
import {
  formatAutoStopTimeParts,
  normalizeAutoStopTimePart,
  parseAutoStopTimeParts,
  type AutoStopTimeParts,
} from '../lib/autoStop'
import {
  DEVICE_OVERLAY_METRIC_KEYS,
  type DeviceOverlayMetricKey,
  type DeviceOverlayReason,
} from '../lib/deviceOverlay'
import {
  DEFAULT_METRIC_VISIBILITY,
  LIVE_METRICS,
  type LiveMetricKey,
  type MetricVisibility,
} from '../lib/liveMetrics'
import { sampleIntervalSec, useLiveCapture } from '../lib/liveCaptureState'
import { SessionNameModal } from '../components/SessionNameModal'
import {
  call,
  getWindowFullscreen,
  onWindowFullscreenChanged,
  openFiles,
  openMeasurementWindow,
  setWindowFullscreen,
} from '../lib/ipc'
import {
  migrateLegacyStorageKey,
} from '../lib/storageMigration'
import type { MetricsSnapshot } from '../lib/types'
import { temperatureZoneForSnapshots } from '../lib/thermalMetrics'
import {
  MAX_TEMPERATURE_THRESHOLD_C,
  MIN_TEMPERATURE_THRESHOLD_C,
  TEMPERATURE_THRESHOLD_STEP_C,
} from '../lib/temperatureGuard'

type ConnectionMode = 'usb' | 'wireless'

const VISIBILITY_KEY = 'mpi.liveMetrics.v1'
const ORDER_KEY = 'mpi.liveMetricsOrder.v1'
const LEGACY_VISIBILITY_KEY = 'perfinspect.liveMetrics.v1'
const LEGACY_ORDER_KEY = 'perfinspect.liveMetricsOrder.v1'

function isStoredMetricVisibility(value: string): boolean {
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false
    const knownKeys = new Set<string>([
      ...LIVE_METRICS.map((metric) => metric.key),
      'cpu_pct',
    ])
    return Object.entries(parsed).every(
      ([key, setting]) => !knownKeys.has(key) || typeof setting === 'boolean'
    )
  } catch {
    return false
  }
}

function isStoredMetricOrder(value: string): boolean {
  try {
    const parsed = JSON.parse(value) as unknown
    return Array.isArray(parsed) && parsed.every((key) => typeof key === 'string')
  } catch {
    return false
  }
}

function loadVisibility(): MetricVisibility {
  try {
    migrateLegacyStorageKey(
      VISIBILITY_KEY,
      LEGACY_VISIBILITY_KEY,
      isStoredMetricVisibility
    )
    const raw = localStorage.getItem(VISIBILITY_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const { cpu_pct: legacyCpuApp, ...current } = parsed as MetricVisibility
        if (current.cpu_app === undefined && typeof legacyCpuApp === 'boolean') {
          current.cpu_app = legacyCpuApp
        }
        return { ...DEFAULT_METRIC_VISIBILITY, ...current }
      }
    }
  } catch {
    /* ignore */
  }
  return DEFAULT_METRIC_VISIBILITY
}

/** Saved card order, reconciled with the current metric set: keep known saved
 *  keys in order, then append any metrics added since (so new cards still show). */
function loadOrder(): LiveMetricKey[] {
  const all = LIVE_METRICS.map((m) => m.key)
  try {
    migrateLegacyStorageKey(
      ORDER_KEY,
      LEGACY_ORDER_KEY,
      isStoredMetricOrder
    )
    const raw = localStorage.getItem(ORDER_KEY)
    if (raw) {
      const saved = (JSON.parse(raw) as string[]).filter((k): k is LiveMetricKey =>
        all.includes(k as LiveMetricKey)
      )
      const missing = all.filter((k) => !saved.includes(k))
      return [...saved, ...missing]
    }
  } catch {
    /* ignore */
  }
  return all
}

export function LiveTracing() {
  const t = useMessages()
  const capture = useLiveCapture()
  const [connectionMode, setConnectionMode] = useState<ConnectionMode>('usb')
  const [connectionRevision, setConnectionRevision] = useState(0)
  // True while WirelessSetup has a prepare_wireless switch in flight. Locks the
  // USB/Wireless toggle so it can't be clicked mid-switch (which wipes the
  // selection and lets the late result clobber the new one).
  const [wirelessPreparing, setWirelessPreparing] = useState(false)
  const [visibility, setVisibility] = useState<MetricVisibility>(loadVisibility)
  const [metricOrder, setMetricOrder] = useState<LiveMetricKey[]>(loadOrder)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const fullscreenDesiredRef = useRef(false)
  const fullscreenRequestRevisionRef = useRef(0)
  const [comparison, setComparison] = useState<{ name: string; snapshots: MetricsSnapshot[] } | null>(null)
  const [comparisonEnabled, setComparisonEnabled] = useState(false)
  const [comparisonLoading, setComparisonLoading] = useState(false)
  const [comparisonError, setComparisonError] = useState<string | null>(null)
  const [autoStopTimeParts, setAutoStopTimeParts] = useState(() =>
    formatAutoStopTimeParts(capture.autoStopAfterSec)
  )

  async function loadComparison() {
    const [path] = await openFiles({ multi: false, filters: [{ name: 'CSV', extensions: ['csv'] }] })
    if (!path) return
    setComparisonLoading(true)
    setComparisonError(null)
    try {
      const result = await call<{ snapshots: MetricsSnapshot[] }>('load_file', { path })
      if (!Array.isArray(result.snapshots) || result.snapshots.length === 0) {
        throw new Error(t.liveTracing.emptyComparison)
      }
      setComparison({ name: path.split(/[\\/]/).pop() ?? path, snapshots: result.snapshots })
      setComparisonEnabled(true)
    } catch (error) {
      setComparisonError(error instanceof Error ? error.message : String(error))
    } finally {
      setComparisonLoading(false)
    }
  }

  function removeComparison() {
    setComparison(null)
    setComparisonEnabled(false)
    setComparisonError(null)
  }

  useEffect(() => {
    try {
      localStorage.setItem(VISIBILITY_KEY, JSON.stringify(visibility))
    } catch {
      /* ignore */
    }
  }, [visibility])

  useEffect(() => {
    try {
      localStorage.setItem(ORDER_KEY, JSON.stringify(metricOrder))
    } catch {
      /* ignore */
    }
  }, [metricOrder])

  // Renderer state follows the native BrowserWindow state. This keeps rapid
  // toggles, Escape and OS-driven exits synchronized per measurement window.
  useEffect(() => {
    let active = true
    const acceptFullscreenState = (state: {
      fullscreen: boolean
      revision: number
    }) => {
      if (
        !active ||
        state.revision < fullscreenRequestRevisionRef.current
      ) {
        return
      }
      fullscreenRequestRevisionRef.current = state.revision
      fullscreenDesiredRef.current = state.fullscreen
      setIsFullscreen(state.fullscreen)
    }
    const unsubscribe = onWindowFullscreenChanged(acceptFullscreenState)
    void getWindowFullscreen().then(acceptFullscreenState)
    const requestFullscreen = (fullscreen: boolean) => {
      const revision = fullscreenRequestRevisionRef.current + 1
      fullscreenRequestRevisionRef.current = revision
      fullscreenDesiredRef.current = fullscreen
      setIsFullscreen(fullscreen)
      void setWindowFullscreen(fullscreen, revision).then(acceptFullscreenState)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey && (e.key === 'Enter' || e.code === 'Enter' || e.code === 'NumpadEnter')) {
        e.preventDefault()
        requestFullscreen(!fullscreenDesiredRef.current)
      } else if (e.key === 'Escape') {
        requestFullscreen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      active = false
      unsubscribe()
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  function requestFullscreen(fullscreen: boolean): void {
    const revision = fullscreenRequestRevisionRef.current + 1
    fullscreenRequestRevisionRef.current = revision
    fullscreenDesiredRef.current = fullscreen
    setIsFullscreen(fullscreen)
    void setWindowFullscreen(fullscreen, revision).then((state) => {
      if (state.revision < fullscreenRequestRevisionRef.current) return
      fullscreenRequestRevisionRef.current = state.revision
      fullscreenDesiredRef.current = state.fullscreen
      setIsFullscreen(state.fullscreen)
    })
  }

  const busy = capture.selectionLocked
  const intervalSec = sampleIntervalSec(capture.platform)

  const latest = capture.latest
  const sampleCount = capture.snapshots.length

  const status: Parameters<typeof StatusBar>[0]['state'] = capture.error
    ? 'error'
    : capture.isRunning
      ? capture.isPaused ? 'paused' : 'tracing'
      : sampleCount > 0
        ? 'stopped'
        : 'idle'

  const statusMessage = useMemo(() => {
    if (capture.error) return friendlyError(capture.error, t.errors)
    if (capture.phase === 'waiting' && capture.guardId) return `guard_id=${capture.guardId}`
    if (capture.isRunning && capture.captureId) return `capture_id=${capture.captureId}`
    if (!capture.isRunning && capture.snapshots.length > 0) return t.liveTracing.canStartNew
    return null
  }, [capture.error, capture.phase, capture.guardId, capture.isRunning, capture.captureId, capture.snapshots.length, t])

  const chip = useMemo(() => buildChip(capture, t.liveTracing), [capture, t])
  const overlayMetricLabels = buildOverlayMetricLabels(t.liveTracing)
  const parsedAutoStopTime = parseAutoStopTimeParts(autoStopTimeParts)
  const autoStopTimeInvalid = capture.autoStopEnabled && parsedAutoStopTime === null

  const canStart =
    !!capture.serial &&
    !!capture.bundleId &&
    !capture.selectionLocked &&
    !autoStopTimeInvalid
  const canStop = capture.selectionLocked && !capture.stopping
  const guardPending =
    capture.phase === 'preparing' ||
    capture.phase === 'waiting' ||
    capture.phase === 'triggering' ||
    (capture.phase === 'stopping' && capture.guardSnapshot !== null)
  const showTemperatureGuardStatus =
    capture.phase === 'waiting' ||
    capture.phase === 'triggering' ||
    (capture.temperatureGuardEnabled &&
      (capture.phase === 'idle' ||
        capture.phase === 'preparing' ||
        capture.phase === 'error'))
  const guardStatusSample =
    capture.phase === 'waiting' || capture.phase === 'triggering'
      ? capture.guardSample
      : null
  const previewStatusSample =
    capture.phase === 'idle' && capture.temperaturePreviewId
      ? capture.temperaturePreviewSample
      : null
  const temperatureMeasurementUnavailable =
    capture.phase === 'error' || capture.temperaturePreviewError !== null
  const statusTemperatureC =
    temperatureMeasurementUnavailable
      ? undefined
      : guardStatusSample?.temperatureC ?? previewStatusSample?.temperatureC
  const hasSamples = sampleCount > 0
  const displaySnapshots = capture.snapshots

  function selectMode(mode: ConnectionMode) {
    if (capture.selectionLocked || wirelessPreparing) return
    setConnectionMode(mode)
    capture.setDeviceSelection(null, null)
    capture.clearError()
  }

  function updateAutoStopPart(
    key: keyof AutoStopTimeParts,
    value: string
  ): void {
    if (!/^\d{0,2}$/.test(value)) return
    const next = { ...autoStopTimeParts, [key]: value }
    setAutoStopTimeParts(next)
    const parsed = parseAutoStopTimeParts(next)
    if (parsed !== null) capture.setAutoStopAfterSec(parsed)
  }

  function blurAutoStopPart(key: keyof AutoStopTimeParts): void {
    setAutoStopTimeParts((current) => ({
      ...current,
      [key]: normalizeAutoStopTimePart(current[key]),
    }))
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mpi-live-layout flex min-h-0 flex-1 overflow-hidden">
        {/* ───────── LEFT SIDEBAR ───────── */}
        <aside
          className={
            isFullscreen
              ? 'mpi-live-sidebar flex w-[240px] flex-none flex-col gap-3 overflow-y-auto border-r border-line px-3 py-4'
              : 'mpi-live-sidebar flex w-[312px] flex-none flex-col gap-5 overflow-y-auto border-r border-line px-5 py-5'
          }
        >
          <div className="flex items-center justify-between gap-3"><h1 className="text-[18px] font-semibold tracking-tight text-ink">{t.dashboard.nav.live}</h1><span className="rounded-md bg-surface-hi px-2 py-1 font-mono text-[11px] font-medium text-muted">MPI</span></div>
          {/* New independent measurement window: each window runs its own device +
              capture concurrently. Hidden in fullscreen to keep the bar compact. */}
          {!isFullscreen && (
            <button
              type="button"
              onClick={() => void openMeasurementWindow()}
              title={t.liveTracing.newWindowTitle}
              className="mpi-control flex items-center justify-center gap-2 border border-line bg-surface px-3 py-2 text-[13px] font-medium text-ink hover:border-accent"
            >
              <Plus size={14} className="flex-none" />
              {t.liveTracing.newWindow}
            </button>
          )}

          {/* connection */}
          <div>
            <div className="mpi-eyebrow mb-3">
              {t.liveTracing.connectionSettings}
            </div>
            <div className="flex flex-col gap-2">
              <Segmented
                value={connectionMode}
                disabled={busy || wirelessPreparing}
                onChange={selectMode}
                options={[
                  { value: 'usb', label: 'USB' },
                  { value: 'wireless', label: 'Wireless', icon: <Wifi size={13} /> },
                ]}
              />
            </div>
            {capture.device?.platform === 'ios' && (
              <div className="mt-2">
                <IosReadinessBadge
                  key={`ios-readiness-${connectionMode}-${capture.device?.connection_type ?? 'none'}-${capture.temperatureGuardEnabled ? 'guard' : 'capture'}`}
                  connectionMode={connectionMode}
                  deviceConnectionType={capture.device?.connection_type}
                  deviceSerial={capture.serial}
                  temperatureGuardEnabled={capture.temperatureGuardEnabled}
                />
              </div>
            )}
          </div>

          {/* device */}
          {connectionMode === 'usb' ? (
            <DeviceSelector
              platform="all"
              label={t.liveTracing.deviceList}
              value={capture.serial}
              onChange={capture.setDeviceSelection}
              onReconnect={() => setConnectionRevision((revision) => revision + 1)}
              disabled={busy}
              connectionType="usb"
            />
          ) : (
            <WirelessSetup
              key={`wireless-setup-${connectionMode}-${capture.serial ?? 'none'}-${capture.device?.connection_type ?? 'none'}`}
              disabled={busy}
              serial={capture.serial}
              device={capture.device}
              onChange={capture.setDeviceSelection}
              onPreparingChange={setWirelessPreparing}
            />
          )}

          {/* app */}
          <PackageSelector
            platform={capture.platform}
            serial={capture.serial}
            connectionRevision={connectionRevision}
            value={capture.bundleId}
            onChange={capture.setBundleId}
            disabled={busy || !capture.serial}
          />

          {/* Optional temperature-triggered start. At the threshold, an already
              running app is attached without changing its state; otherwise the
              app is launched. Only preferences are persisted; the runtime phase
              always starts idle after relaunch. */}
          <div className="mpi-panel">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[13px] font-semibold text-ink">
                  {t.liveTracing.temperatureGuardTitle}
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={capture.temperatureGuardEnabled}
                aria-label={t.liveTracing.temperatureGuardToggle}
                disabled={capture.selectionLocked}
                onClick={() =>
                  capture.setTemperatureGuardEnabled(!capture.temperatureGuardEnabled)
                }
                className={[
                  'relative mt-0.5 h-5 w-9 flex-none rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-50',
                  capture.temperatureGuardEnabled ? 'bg-accent' : 'bg-line',
                ].join(' ')}
              >
                <span
                  aria-hidden="true"
                  className={[
                    'absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform',
                    capture.temperatureGuardEnabled ? 'translate-x-4' : 'translate-x-0',
                  ].join(' ')}
                />
              </button>
            </div>
            <label className="mt-3 flex items-center justify-between gap-3 text-[12px] text-muted">
              <span>{t.liveTracing.temperatureGuardThreshold}</span>
              <span className="inline-flex items-center gap-1">
                <input
                  type="number"
                  aria-label={t.liveTracing.temperatureGuardThreshold}
                  min={MIN_TEMPERATURE_THRESHOLD_C}
                  max={MAX_TEMPERATURE_THRESHOLD_C}
                  step={TEMPERATURE_THRESHOLD_STEP_C}
                  value={capture.temperatureGuardThresholdC.toFixed(1)}
                  disabled={!capture.temperatureGuardEnabled || capture.selectionLocked}
                  onChange={(event) =>
                    capture.setTemperatureGuardThresholdC(event.currentTarget.valueAsNumber)
                  }
                  className="w-[72px] rounded-md border border-line bg-bg px-2 py-1 text-right font-mono text-[12px] text-ink disabled:cursor-not-allowed disabled:opacity-50"
                />
                <span>°C</span>
              </span>
            </label>
          </div>

          {/* Optional duration limit. The backend starts this deadline at the
              first real sample, then stops this capture and its exact target. */}
          <div className="mpi-panel">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[13px] font-semibold text-ink">
                  {t.liveTracing.autoStopTitle}
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={capture.autoStopEnabled}
                aria-label={t.liveTracing.autoStopToggle}
                disabled={capture.selectionLocked}
                onClick={() => capture.setAutoStopEnabled(!capture.autoStopEnabled)}
                className={[
                  'relative mt-0.5 h-5 w-9 flex-none rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-50',
                  capture.autoStopEnabled ? 'bg-accent' : 'bg-line',
                ].join(' ')}
              >
                <span
                  aria-hidden="true"
                  className={[
                    'absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform',
                    capture.autoStopEnabled ? 'translate-x-4' : 'translate-x-0',
                  ].join(' ')}
                />
              </button>
            </div>
            <div className="mt-3 block text-[12px] text-muted">
              <span>{t.liveTracing.autoStopEndTime}</span>
              <div
                className="mt-1 grid grid-cols-3 gap-1.5"
                aria-label={t.liveTracing.autoStopEndTime}
              >
                {(
                  [
                    ['hours', t.liveTracing.autoStopHours, '00'],
                    ['minutes', t.liveTracing.autoStopMinutes, '40'],
                    ['seconds', t.liveTracing.autoStopSeconds, '00'],
                  ] as const
                ).map(([key, label, placeholder]) => (
                  <label key={key} className="min-w-0">
                    <input
                      type="text"
                      inputMode="numeric"
                      maxLength={2}
                      placeholder={placeholder}
                      value={autoStopTimeParts[key]}
                      aria-label={label}
                      aria-invalid={autoStopTimeInvalid}
                      disabled={!capture.autoStopEnabled || capture.selectionLocked}
                      onChange={(event) =>
                        updateAutoStopPart(key, event.currentTarget.value)
                      }
                      onBlur={() => blurAutoStopPart(key)}
                      className={[
                        'w-full rounded-md border bg-bg px-1.5 py-1.5 text-center font-mono text-[12px] text-ink tabular-nums disabled:cursor-not-allowed disabled:opacity-50',
                        autoStopTimeInvalid ? 'border-danger' : 'border-line',
                      ].join(' ')}
                    />
                    <span className="mt-1 block text-center text-[11px] text-muted">
                      {label}
                    </span>
                  </label>
                ))}
              </div>
              <span
                className={[
                  'mt-2 block text-[12px] leading-relaxed',
                  autoStopTimeInvalid ? 'text-danger' : 'text-faint',
                ].join(' ')}
              >
                {autoStopTimeInvalid
                  ? t.liveTracing.autoStopInvalidTime
                  : t.liveTracing.autoStopTimeHint}
              </span>
            </div>
          </div>

          {capture.platform === 'android' && (
            <PlayCompanionSetup
              key={capture.serial ?? 'no-device'}
              selected={!!capture.serial}
              status={capture.deviceOverlayStatus}
              locked={capture.selectionLocked}
              check={capture.checkPlayCompanion}
            />
          )}

          {/* Android-only on-device HUD. The Android platform is the initial
              capture mode, so the preference is available before a device is
              connected. Unlike the settings above, this one stays editable while a
              capture runs — the provider pushes changes to the live capture. */}
          {capture.platform === 'android' && (
            <div className="mpi-panel">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-[13px] font-semibold text-ink">
                    {t.liveTracing.deviceOverlayTitle}
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={capture.deviceOverlayEnabled}
                  aria-label={t.liveTracing.deviceOverlayToggle}
                  onClick={() => capture.setDeviceOverlayEnabled(!capture.deviceOverlayEnabled)}
                  className={[
                    'relative mt-0.5 h-5 w-9 flex-none rounded-full transition-colors',
                    capture.deviceOverlayEnabled ? 'bg-accent' : 'bg-line',
                  ].join(' ')}
                >
                  <span
                    aria-hidden="true"
                    className={[
                      'absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform',
                      capture.deviceOverlayEnabled ? 'translate-x-4' : 'translate-x-0',
                    ].join(' ')}
                  />
                </button>
              </div>
              <div className="mt-3 text-[12px] text-muted">
                <span>{t.liveTracing.deviceOverlayMetricsLabel}</span>
                <div className="mt-1.5 grid grid-cols-2 gap-x-2 gap-y-1">
                  {DEVICE_OVERLAY_METRIC_KEYS.map((key) => (
                    <label key={key} className="flex items-center gap-1.5 text-[11px]">
                      <input
                        type="checkbox"
                        checked={capture.deviceOverlayMetrics[key]}
                        disabled={!capture.deviceOverlayEnabled}
                        onChange={(event) =>
                          capture.setDeviceOverlayMetric(key, event.currentTarget.checked)
                        }
                        className="h-3.5 w-3.5 accent-accent disabled:cursor-not-allowed disabled:opacity-50"
                      />
                      {overlayMetricLabels[key]}
                    </label>
                  ))}
                </div>
              </div>
              {/* Every failure mode here is silent on the device — a missing
                  companion or a denied overlay permission just shows nothing —
                  so say why rather than letting the switch look broken. Only
                  once the user has actually asked for the HUD. */}
              {capture.deviceOverlayEnabled &&
                capture.deviceOverlayStatus &&
                ['overlay_permission_denied', 'overlay_unsupported_os'].includes(capture.deviceOverlayStatus.reason ?? '') && (
                  <p role="status" className="mt-2 text-[10px] leading-relaxed text-danger">
                    {overlayUnavailableMessage(
                      capture.deviceOverlayStatus.reason,
                      t.liveTracing
                    )}
                  </p>
                )}
            </div>
          )}

          {/* device info */}
          <DeviceInfoCard
            device={capture.device}
            platform={capture.platform}
            connectionMode={connectionMode}
            appLabel={latest?.package.label ?? null}
            bundleId={capture.bundleId}
            intervalSec={intervalSec}
          />

          {/* Local endpoint transfer settings */}
          <CloudPanel />
        </aside>

        {/* ───────── CENTER ───────── */}
        <section className="mpi-live-center flex min-w-0 flex-1 flex-col overflow-hidden">
          {/* control bar */}
          <div
            className={[
              'mpi-panel mpi-capture-toolbar flex flex-wrap items-center gap-2.5',
              isFullscreen ? 'm-3 mb-0 px-4 py-3' : 'm-5 mb-0 px-4 py-4',
            ].join(' ')}
          >
            <div className="mpi-capture-primary flex min-w-0 flex-wrap items-center gap-3">
              <span
                className="inline-flex w-fit items-center gap-1.5 rounded-full border px-2 py-1"
                style={{ background: chip.bg, borderColor: chip.bd }}
              >
                <span
                  className="h-2 w-2 flex-none rounded-full"
                  style={{
                    background: chip.dot,
                    boxShadow: chip.halo ? `0 0 0 4px ${chip.halo}` : 'none',
                    animation: chip.pulse && !capture.isPaused ? 'recpulse 1.3s ease-in-out infinite' : 'none',
                  }}
                />
                <span className="text-xs font-semibold tracking-wide" style={{ color: chip.ink }}>
                  {capture.isPaused ? t.capture.paused : chip.text}
                </span>
              </span>
              <span
                className={[
                  'font-mono text-[24px] font-semibold leading-none tracking-tight text-ink tabular-nums',
                ].join(' ')}
              >
                {formatHms(capture.elapsedSec)}
              </span>
              <button
                type="button"
                onClick={() => void capture.start()}
                disabled={!canStart}
                aria-busy={capture.starting}
                className="mpi-control mpi-press inline-flex flex-none items-center gap-2 whitespace-nowrap px-4 py-2 text-[13px] disabled:cursor-not-allowed disabled:opacity-50"
                style={
                  canStart
                    ? {
                        background: 'var(--color-accent)',
                        color: 'var(--color-on-accent)',
                        fontWeight: 700,
                        border: '1px solid transparent',
                      }
                    : {
                        background: 'var(--color-surface-hi)',
                        color: 'var(--color-muted)',
                        fontWeight: 500,
                        border: '1px solid var(--color-line)',
                      }
                }
              >
                {capture.starting ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Play size={12} fill="currentColor" />
                )}
                {capture.starting
                  ? t.liveTracing.starting
                  : capture.temperatureGuardEnabled
                    ? t.liveTracing.prepareCapture
                    : t.liveTracing.startCapture}
              </button>

              <button
                type="button"
                onClick={capture.stop}
                disabled={!canStop}
                aria-busy={capture.stopping}
                className="mpi-control mpi-press inline-flex flex-none items-center gap-2 whitespace-nowrap px-4 py-2 text-[13px] disabled:cursor-not-allowed disabled:opacity-50"
                style={
                  canStop
                    ? {
                        background: 'var(--color-danger)',
                        color: 'var(--color-on-danger)',
                        fontWeight: 700,
                        border: '1px solid transparent',
                      }
                    : {
                        background: 'var(--color-surface-hi)',
                        color: 'var(--color-muted)',
                        fontWeight: 500,
                        border: '1px solid var(--color-line)',
                      }
                }
              >
                {capture.stopping ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Square size={11} fill="currentColor" />
                )}
                {capture.stopping
                  ? t.liveTracing.stopping
                  : guardPending
                    ? t.liveTracing.cancelWaiting
                    : t.liveTracing.stopCapture}
              </button>
            </div>

            {capture.isRunning && <div className="mt-2 flex flex-wrap items-center gap-2">
              <button type="button" onClick={() => void capture.togglePause()} disabled={capture.pausePending || capture.starting || capture.stopping || !hasSamples}
                title={t.capture.pauseHint} aria-pressed={capture.isPaused}
                className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-xs text-ink disabled:opacity-40">
                {capture.isPaused ? <Play size={13} /> : <Pause size={13} />}
                {capture.isPaused ? t.capture.resume : t.capture.pause}
              </button>
              <button type="button" onClick={capture.toggleCut} disabled={capture.isPaused || capture.pausePending || capture.stopping || capture.starting || !hasSamples}
                className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-xs text-ink disabled:opacity-40">
                <Scissors size={13} />{capture.activeCut ? t.capture.endCut : t.capture.addCut}
              </button>
              {capture.activeCut && <span role="status" className="text-xs text-accent">{capture.activeCut.name} · {t.capture.cutStart} {formatSessionRangeTime(capture.activeCut.start_sec)}</span>}
              {capture.isPaused && <span className="text-xs text-muted">{t.capture.pauseHint}</span>}
            </div>}
            {(capture.cuts?.length ?? 0) > 0 && <div className="mt-2 flex flex-wrap gap-2" aria-label={t.capture.cuts}>
              {capture.cuts.map(cut => <span key={cut.id} className="rounded-md border border-line px-2 py-1 text-xs text-muted">{cut.name} · {formatSessionRangeTime(cut.start_sec)}–{formatSessionRangeTime(cut.end_sec)}</span>)}
            </div>}

            {showTemperatureGuardStatus && (
              <div
                data-testid="temperature-guard-status"
                className="grid grid-cols-3 gap-x-5 rounded-[10px] border border-accent-line bg-accent-soft px-4 py-2 text-[11px]"
              >
                <GuardReading
                  label={t.liveTracing.guardCurrentTemperature}
                  value={
                    temperatureMeasurementUnavailable
                      ? t.liveTracing.guardUnavailable
                      : statusTemperatureC !== undefined
                      ? `${statusTemperatureC.toFixed(1)}°C`
                      : t.liveTracing.guardNoSample
                  }
                />
                <GuardReading
                  label={t.liveTracing.guardThresholdTemperature}
                  value={`${(
                    guardStatusSample?.thresholdC ??
                    capture.guardSnapshot?.thresholdC ??
                    capture.temperatureGuardThresholdC
                  ).toFixed(1)}°C`}
                />
                <GuardReading
                  label={t.liveTracing.guardHitCount}
                  value={`${guardStatusSample?.consecutiveHits ?? 0}/${guardStatusSample?.requiredHits ?? 2}`}
                />
                {capture.phase === 'idle' && capture.temperaturePreviewError && (
                  <div className="col-span-3 mt-1 flex items-center justify-between gap-3 border-t border-accent-line pt-2">
                    <span
                      className="min-w-0 truncate text-danger"
                      title={capture.temperaturePreviewError}
                    >
                      {t.liveTracing.temperaturePreviewError}
                    </span>
                    <button
                      type="button"
                      className="flex-none rounded border border-accent-line px-2 py-1 font-semibold text-accent hover:bg-accent/10"
                      onClick={capture.retryTemperaturePreview}
                    >
                      {t.common.retry}
                    </button>
                  </div>
                )}
              </div>
            )}

            <div className="h-0 basis-full" aria-hidden="true" />

            <button
              type="button"
              onClick={() => void capture.exportXlsx()}
              disabled={capture.exportingXlsx || !hasSamples}
              className="inline-flex flex-none items-center gap-2 whitespace-nowrap rounded-[10px] border border-line bg-surface px-4 py-[11px] text-sm font-medium text-ink transition-colors hover:border-accent-line disabled:opacity-50"
            >
              <FileSpreadsheet size={15} className="text-muted" /> {t.liveTracing.saveXlsx}
            </button>

            <button
              type="button"
              onClick={() => void capture.exportCsv()}
              disabled={capture.exportingCsv || !hasSamples}
              className="inline-flex flex-none items-center gap-2 whitespace-nowrap rounded-[10px] border border-line bg-surface px-4 py-[11px] text-sm font-medium text-ink transition-colors hover:border-accent-line disabled:opacity-50"
            >
              <Download size={15} className="text-muted" /> CSV
            </button>

            <MetricVisibilityMenu visibility={visibility} onChange={setVisibility} temperatureZone={temperatureZoneForSnapshots(displaySnapshots, capture.platform)} />

            <div className="group/comparison inline-flex min-w-0 flex-none items-stretch overflow-hidden rounded-[10px] border border-line bg-surface transition-colors duration-150 hover:border-accent-line hover:bg-accent-soft focus-within:border-accent-line focus-within:ring-2 focus-within:ring-accent/25">
              <button type="button" onClick={() => void loadComparison()} disabled={comparisonLoading}
                title={comparisonError ?? (comparison ? `${t.liveTracing.loadedComparison}: ${comparison.name}` : t.liveTracing.loadComparisonTitle)}
                aria-label={comparison ? `${t.liveTracing.loadedComparison}: ${comparison.name}. ${t.liveTracing.replaceComparison}` : t.liveTracing.loadComparison}
                className="inline-flex max-w-[240px] min-w-0 items-center gap-2 px-3 py-[11px] text-sm text-ink outline-none transition-colors group-hover/comparison:text-accent disabled:opacity-50">
                {comparisonLoading ? <Loader2 size={14} className="shrink-0 animate-spin" /> : <FileText size={14} className="shrink-0" />}
                <span className="truncate">{comparison?.name ?? t.liveTracing.loadComparison}</span>
              </button>
              <button type="button" disabled={!comparison} onClick={() => setComparisonEnabled((v) => !v)}
                aria-pressed={comparisonEnabled}
                title={`${t.liveTracing.compare}: ${comparisonEnabled ? t.liveTracing.on : t.liveTracing.off}`}
                className="border-l border-line px-3 py-[11px] text-sm text-ink outline-none transition-colors hover:bg-accent/10 focus-visible:bg-accent/10 disabled:opacity-50">
                {t.liveTracing.compare}: {comparisonEnabled ? t.liveTracing.on : t.liveTracing.off}
              </button>
              {comparison && (
                <button type="button" onClick={removeComparison}
                  title={`${t.liveTracing.removeComparison}: ${comparison.name}`}
                  aria-label={`${t.liveTracing.removeComparison}: ${comparison.name}`}
                  className="inline-flex items-center border-l border-line px-2.5 text-muted outline-none transition-colors hover:bg-danger/10 hover:text-danger focus-visible:bg-danger/10 focus-visible:text-danger">
                  <X size={15} aria-hidden="true" />
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={() => requestFullscreen(!fullscreenDesiredRef.current)}
              className="inline-flex flex-none items-center gap-2 whitespace-nowrap rounded-[10px] border border-line bg-surface px-3 py-[11px] text-sm font-medium text-ink transition-colors hover:border-accent-line"
              title={t.liveTracing.fullscreenTitle}
              aria-label={t.liveTracing.fullscreenTitle}
            >
              {isFullscreen ? <Minimize2 size={15} className="text-muted" /> : <Maximize2 size={15} className="text-muted" />}
            </button>
          </div>

          {/* charts */}
          <div className="mpi-chart-area flex min-h-0 flex-1 overflow-hidden">
            <div className="mpi-chart-scroll relative min-w-0 flex-1 overflow-auto p-5">
              <RenderPipelinePanel data={latest?.render_pipeline} />
              <LiveCharts
                snapshots={displaySnapshots}
                comparisonSnapshots={comparisonEnabled ? comparison?.snapshots : undefined}
                visibility={visibility}
                order={metricOrder}
                onReorder={setMetricOrder}
              />
            </div>
          </div>
        </section>
      </div>

      <StatusBar
        state={status}
        message={statusMessage}
        rawMessage={capture.error}
        sampleCount={sampleCount}
        lastCapturedAt={latest?.captured_at ?? null}
        durationSec={latest?.duration_sec ?? null}
      />

      {capture.pendingSave && (
        <SessionNameModal
          defaultName={capture.pendingSave.defaultName}
          saving={capture.savingSession}
          onConfirm={(n) => void capture.confirmSessionSave(n)}
          onSkip={capture.skipSessionSave}
        />
      )}
    </div>
  )
}

interface Chip {
  text: string
  ink: string
  dot: string
  bg: string
  bd: string
  halo: string | null
  pulse: boolean
}

function GuardReading({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex min-w-[86px] flex-col gap-0.5">
      <span className="text-faint">{label}</span>
      <span className="font-mono text-[13px] font-semibold text-ink tabular-nums">{value}</span>
    </span>
  )
}

function buildOverlayMetricLabels(
  m: ReturnType<typeof useMessages>['liveTracing']
): Record<DeviceOverlayMetricKey, string> {
  return {
    elapsed: m.deviceOverlayMetricElapsed,
    fps: m.deviceOverlayMetricFps,
    cpu: m.deviceOverlayMetricCpu,
    gpu: m.deviceOverlayMetricGpu,
    skin: m.deviceOverlayMetricSkin,
    net: m.deviceOverlayMetricNet,
  }
}

function overlayUnavailableMessage(
  reason: DeviceOverlayReason | null,
  m: ReturnType<typeof useMessages>['liveTracing']
): string {
  switch (reason) {
    case 'companion_not_installed':
      return m.deviceOverlayUnavailableCompanionNotInstalled
    case 'companion_too_old':
      return m.deviceOverlayUnavailableCompanionTooOld
    case 'usb_unauthorized':
      return m.deviceOverlayUsbUnauthorized
    case 'device_unreachable':
      return m.deviceOverlayDeviceUnreachable
    case 'companion_sign_in_required':
      return m.deviceOverlaySignInRequired
    case 'overlay_unsupported_os':
      return m.deviceOverlayUnavailableUnsupportedOs
    case 'overlay_permission_denied':
      return m.deviceOverlayUnavailablePermissionDenied
    default:
      return m.deviceOverlayUnavailableCompanionUnreachable
  }
}

function buildChip(
  capture: ReturnType<typeof useLiveCapture>,
  m: ReturnType<typeof useMessages>['liveTracing']
): Chip {
  if (capture.error) {
    return {
      text: m.chipError,
      ink: 'var(--color-danger)',
      dot: 'var(--color-danger)',
      bg: 'var(--color-rec-soft)',
      bd: 'rgba(240,88,77,0.32)',
      halo: 'rgba(240,88,77,0.2)',
      pulse: false,
    }
  }
  if (capture.phase === 'waiting' || capture.phase === 'triggering') {
    return {
      text:
        capture.phase === 'waiting'
          ? m.chipWaiting
          : m.chipTriggering,
      ink: 'var(--color-accent)',
      dot: 'var(--color-accent)',
      bg: 'var(--color-accent-soft)',
      bd: 'var(--color-accent-line)',
      halo: null,
      pulse: true,
    }
  }
  if (capture.starting || capture.stopping) {
    return {
      text: capture.starting
        ? capture.temperatureGuardEnabled
          ? m.chipPreparing
          : m.chipStarting
        : m.chipStopping,
      ink: 'var(--color-accent)',
      dot: 'var(--color-accent)',
      bg: 'var(--color-accent-soft)',
      bd: 'var(--color-accent-line)',
      halo: null,
      pulse: true,
    }
  }
  if (capture.isRunning) {
    return {
      text: m.chipRunning,
      ink: 'var(--color-rec)',
      dot: 'var(--color-rec)',
      bg: 'var(--color-rec-soft)',
      bd: 'rgba(240,88,77,0.32)',
      halo: 'rgba(240,88,77,0.2)',
      pulse: true,
    }
  }
  if (capture.snapshots.length > 0) {
    return {
      text: m.chipDone,
      ink: 'var(--color-ok)',
      dot: 'var(--color-ok)',
      bg: 'rgba(34,179,120,0.12)',
      bd: 'rgba(34,179,120,0.3)',
      halo: null,
      pulse: false,
    }
  }
  return {
    text: m.chipIdle,
    ink: 'var(--color-muted)',
    dot: 'var(--color-faint)',
    bg: 'var(--color-icon)',
    bd: 'var(--color-line)',
    halo: null,
    pulse: false,
  }
}

function Segmented<T extends string>({
  value,
  options,
  disabled = false,
  onChange,
}: {
  value: T
  options: { value: T; label: string; icon?: React.ReactNode }[]
  disabled?: boolean
  onChange(v: T): void
}) {
  return (
    <div className="inline-flex w-full gap-1 rounded-[15px] border border-line bg-surface p-1">
      {options.map((opt) => {
        const active = value === opt.value
        return (
          <button
            key={opt.value}
            type="button"
            disabled={disabled}
            onClick={() => onChange(opt.value)}
            aria-pressed={active}
            className={[
              'inline-flex min-h-10 flex-1 items-center justify-center gap-2 rounded-[10px] px-3 py-2 text-[13px] font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50',
              active
                ? 'bg-accent-soft text-accent'
                : 'bg-surface text-muted hover:bg-hover hover:text-ink',
            ].join(' ')}
          >
            {opt.icon}
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}
