import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  ANDROID_SAMPLE_INTERVAL_SEC,
  IOS_SAMPLE_INTERVAL_SEC,
  LiveCaptureContext,
  type LiveCaptureState,
} from '../lib/liveCaptureState'
import {
  call,
  onEvent,
  postToEndpoint,
  saveFile,
  type PostToEndpointResult,
} from '../lib/ipc'
import { getEndpointUrl, getSendOnSave } from '../lib/endpointSettings'
import { cloudCommand, cloudErrorMessage, enqueueCloudSession } from '../lib/cloud-sync'
import { friendlyError } from '../lib/friendlyError'
import { useMessages, type Messages } from '../lib/i18n'
import { closeLiveCut, snapshotElapsed } from '../lib/sessionCuts'
import type { SessionCut, DeviceInfo, MetricsSnapshot, PackageInfo, Platform, RenderPipeline } from '../lib/types'
import {
  loadAutoStopSettings,
  normalizeAutoStopAfterSec,
  saveAutoStopSettings,
} from '../lib/autoStop'
import {
  deviceOverlayMetricList,
  loadDeviceOverlaySettings,
  saveDeviceOverlaySettings,
  toDeviceOverlayStatus,
  type CompanionOverlayStatusResult,
  type DeviceOverlayMetricKey,
  type DeviceOverlaySettings,
  type DeviceOverlayStatus,
} from '../lib/deviceOverlay'
import {
  loadTemperatureGuardSettings,
  normalizeTemperatureThreshold,
  saveTemperatureGuardSettings,
  TEMPERATURE_GUARD_REQUIRED_HITS,
  type CapturePhase,
  type CreateTemperaturePreviewResult,
  type CreateTemperatureGuardResult,
  type TemperatureGuardEvent,
  type TemperatureGuardSample,
  type TemperatureGuardSnapshot,
  type TemperaturePreviewEvent,
  type TemperaturePreviewSample,
} from '../lib/temperatureGuard'

const ROLLING_WINDOW_MIN = 60
const TEMPERATURE_PREVIEW_STOP_TIMEOUT_MS = 30_000

interface TemperaturePreviewEndedWaiter {
  promise: Promise<void>
  resolve(): void
  reject(error: Error): void
  timeoutId: ReturnType<typeof window.setTimeout>
}

/** Send saved data once to the configured endpoint when send-on-save is enabled.
 * Builds the JSON payload in the backend and never throws. */
async function sendSnapshotsToEndpoint(
  snapshots: MetricsSnapshot[],
  t: Messages,
  name?: string | null,
  sessionId?: string | null,
  cuts: SessionCut[] = [],
): Promise<PostToEndpointResult | null> {
  const url = getEndpointUrl()
  if (snapshots.length === 0 || !getSendOnSave() || !url) return null

  let payload: unknown
  try {
    payload = await call('build_endpoint_payload', { snapshots, name, session_id: sessionId, cuts })
  } catch (err) {
    return { ok: false, error: friendlyError(err instanceof Error ? err.message : String(err), t.errors) }
  }

  let result: PostToEndpointResult
  try {
    result = await postToEndpoint({ url, payload })
  } catch (err) {
    return {
      ok: false,
      error: t.capture.endpointSendFailed(
        friendlyError(err instanceof Error ? err.message : String(err), t.errors)
      ),
    }
  }
  if (!result.ok) {
    return {
      ...result,
      error: t.capture.endpointSendFailed(friendlyError(result.error ?? 'unknown error', t.errors)),
    }
  }
  return result
}

async function saveSnapshotsToCloud(
  snapshots: MetricsSnapshot[], sessionId: string, ownerId: string | null,
  name: string, t: Messages, shouldEnqueue?: () => boolean, cuts: SessionCut[] = [],
): Promise<string | null> {
  if (!ownerId || snapshots.length === 0) return null
  try {
    const payload = await call('build_endpoint_payload', { snapshots, name, session_id: sessionId, cuts })
    if (shouldEnqueue && !shouldEnqueue()) return null
    const result = await enqueueCloudSession({ ownerId, sessionId, name, payload })
    return result.ok ? null : cloudErrorMessage(result.status.error, t.cloud)
  } catch {
    return t.cloud.storageError
  }
}

/** Prefilled default name for the post-capture naming modal: the app label (or
 *  bundle id / device model) plus the local capture time. Module-scope so it can
 *  be called from the capture_ended event handler without tripping the
 *  react-hooks "no Date in render" rule. */
function buildDefaultSessionName(snapshots: MetricsSnapshot[]): string {
  const last = snapshots[snapshots.length - 1]
  const label = last?.package.label || last?.package.bundle_id || last?.device.model || 'session'
  const basis = last?.captured_at ? new Date(last.captured_at) : new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  const stamp = `${basis.getFullYear()}-${p(basis.getMonth() + 1)}-${p(basis.getDate())} ${p(basis.getHours())}:${p(basis.getMinutes())}`
  return `${label} ${stamp}`
}

interface StartCaptureResult {
  capture_id: string
  session_id?: string
  device: DeviceInfo
  package: PackageInfo
  duration_sec: number
}

export function LiveCaptureProvider({ children }: { children: ReactNode }) {
  const t = useMessages()
  const [phase, setPhase] = useState<CapturePhase>('idle')
  const [platform, setPlatformState] = useState<Platform>('android')
  const [serial, setSerial] = useState<string | null>(null)
  const [device, setDevice] = useState<DeviceInfo | null>(null)
  const [bundleId, setBundleIdState] = useState<string | null>(null)
  const [captureId, setCaptureId] = useState<string | null>(null)
  const [snapshots, setSnapshots] = useState<MetricsSnapshot[]>([])
  const [isPaused, setIsPaused] = useState(false)
  const isPausedRef = useRef(false)
  const [pausePending, setPausePending] = useState(false)
  const pausePendingRef = useRef(false)
  const [cuts, setCuts] = useState<SessionCut[]>([])
  const cutsRef = useRef<SessionCut[]>([])
  const [activeCut, setActiveCut] = useState<Omit<SessionCut, 'end_sec'> | null>(null)
  const activeCutRef = useRef<Omit<SessionCut, 'end_sec'> | null>(null)
  // Wall-clock anchors for the elapsed timer. `captureStart` is the epoch ms of
  // the first received sample; `nowTick` is advanced by an interval so the clock
  // follows real time instead of the number of samples received.
  const [captureStart, setCaptureStart] = useState<number | null>(null)
  const [nowTick, setNowTick] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [sampleRetrying, setSampleRetrying] = useState(false)
  const [exportingCsv, setExportingCsv] = useState(false)
  const [exportingXlsx, setExportingXlsx] = useState(false)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [autoStopSettings, setAutoStopSettings] = useState(loadAutoStopSettings)
  const [overlaySettings, setOverlaySettings] = useState(loadDeviceOverlaySettings)
  const overlaySettingsRef = useRef(overlaySettings)
  // Null until the probe answers. The HUD is drawn by a sideloaded app that may
  // be absent, outdated, or denied the overlay permission, and every one of those
  // fails silently on the device — so the toggle has to be able to say why.
  const [overlayStatus, setOverlayStatus] = useState<DeviceOverlayStatus | null>(null)
  const [guardSettings, setGuardSettings] = useState(loadTemperatureGuardSettings)
  const [guardId, setGuardId] = useState<string | null>(null)
  const [guardSample, setGuardSample] = useState<TemperatureGuardSample | null>(null)
  const [guardSnapshot, setGuardSnapshot] = useState<TemperatureGuardSnapshot | null>(null)
  const [temperaturePreviewId, setTemperaturePreviewId] = useState<string | null>(null)
  const [temperaturePreviewSample, setTemperaturePreviewSample] =
    useState<TemperaturePreviewSample | null>(null)
  const [temperaturePreviewLoading, setTemperaturePreviewLoading] = useState(false)
  const [temperaturePreviewError, setTemperaturePreviewError] = useState<string | null>(null)
  const [temperaturePreviewRetryNonce, setTemperaturePreviewRetryNonce] = useState(0)
  // A finished capture awaiting a session name before its endpoint transfer.
  const [pendingSave, setPendingSave] = useState<{
    snapshots: MetricsSnapshot[]
    defaultName: string
    sessionId: string
    ownerId: string | null
    cuts: SessionCut[]
  } | null>(null)
  const [savingSession, setSavingSession] = useState(false)
  const captureIdRef = useRef<string | null>(null)
  const captureStartRef = useRef<number | null>(null)
  const capturePlatformRef = useRef<Platform>('android')
  const guardIdRef = useRef<string | null>(null)
  const temperaturePreviewIdRef = useRef<string | null>(null)
  const temperaturePreviewGenerationRef = useRef(0)
  const temperaturePreviewOwnerGenerationRef = useRef(-1)
  const temperaturePreviewLifecycleRef = useRef<Promise<void>>(Promise.resolve())
  const temperaturePreviewStopPromisesRef = useRef<Map<string, Promise<void>>>(new Map())
  const temperaturePreviewEndedWaitersRef = useRef<Map<string, TemperaturePreviewEndedWaiter>>(
    new Map()
  )
  const mountedRef = useRef(true)
  const phaseRef = useRef<CapturePhase>('idle')
  const startInFlightRef = useRef(false)
  const cancelRequestedRef = useRef(false)
  const operationRef = useRef(0)
  // Latest snapshots, mirrored into a ref so the once-mounted event listener can
  // auto-save the session on capture_ended without re-subscribing each sample.
  const snapshotsRef = useRef<MetricsSnapshot[]>([])
  // Charts use a rolling window; persistence and exports retain the complete
  // capture even after the visible chart discards its oldest samples.
  const completeSnapshotsRef = useRef<MetricsSnapshot[]>([])
  const sessionIdRef = useRef('')
  const sessionOwnerRef = useRef<string | null>(null)
  const sessionNameRef = useRef<string | null>(null)
  const messagesRef = useRef(t)
  const capturePersistenceRef = useRef<Promise<void>>(Promise.resolve())
  const closeCaptureEndedRef = useRef<(() => void) | null>(null)
  const closingRef = useRef(false)
  useEffect(() => { messagesRef.current = t }, [t])
  const overlayTargetSerialRef = useRef<string | null>(null)
  const overlayProbeRevisionRef = useRef(0)
  // An overlay toggle made while no capture id exists yet. The card stays
  // interactive through `preparing`/`waiting`, so with the temperature guard the
  // user can flip the HUD minutes before the guard launches a capture; parking
  // the request here lets whichever path learns the id replay it.
  const overlayPendingPushRef = useRef<DeviceOverlaySettings | null>(null)
  // Renderer IPC commands run on a worker pool, so preserve user action order
  // explicitly. Otherwise a slow "on" request can land after a later "off".
  const overlayPushLifecycleRef = useRef<{
    captureId: string | null
    promise: Promise<void>
  }>({ captureId: null, promise: Promise.resolve() })

  const enqueueOverlayPush = useCallback(
    (targetCaptureId: string, next: DeviceOverlaySettings) => {
      const current =
        overlayPushLifecycleRef.current.captureId === targetCaptureId
          ? overlayPushLifecycleRef.current.promise
          : Promise.resolve()
      const promise = current
        .catch(() => {})
        .then(() =>
          call('set_capture_overlay', {
            capture_id: targetCaptureId,
            enabled: next.enabled,
            metrics: deviceOverlayMetricList(next),
          })
        )
        .then(() => undefined, () => undefined)
      overlayPushLifecycleRef.current = { captureId: targetCaptureId, promise }
    },
    []
  )

  const transitionTo = useCallback((next: CapturePhase) => {
    phaseRef.current = next
    setPhase(next)
  }, [])

  const updateGuardId = useCallback((next: string | null) => {
    guardIdRef.current = next
    setGuardId(next)
  }, [])

  const updateCaptureId = useCallback((next: string | null) => {
    captureIdRef.current = next
    setCaptureId(next)
    setSampleRetrying(false)
  }, [])

  // Replay a parked HUD toggle the moment a capture id appears. Nothing is
  // parked unless the user changed the HUD after the start command went out —
  // that command already carried the settings of its time — so the common path
  // costs no extra round trip. Fire-and-forget with the same swallowed failures
  // as the live push: the companion is allowed to be absent.
  // Declared up here because the once-mounted event listener below lists it as a
  // dependency, and a dep array is read during render.
  const reconcileOverlayForCapture = useCallback((newCaptureId: string) => {
    const pending = overlayPendingPushRef.current
    overlayPendingPushRef.current = null
    // capturePlatformRef is locked by start() and a capture id can only exist
    // after that, so this reliably filters out iOS, which has no companion HUD.
    if (!pending || capturePlatformRef.current !== 'android') return
    enqueueOverlayPush(newCaptureId, pending)
  }, [enqueueOverlayPush])

  const invalidateTemperaturePreview = useCallback(() => {
    temperaturePreviewGenerationRef.current += 1
    setTemperaturePreviewId(null)
    setTemperaturePreviewSample(null)
    setTemperaturePreviewLoading(false)
    setTemperaturePreviewError(null)
  }, [])

  const settleTemperaturePreviewEnded = useCallback((previewId: string) => {
    const waiter = temperaturePreviewEndedWaitersRef.current.get(previewId)
    if (!waiter) return
    temperaturePreviewEndedWaitersRef.current.delete(previewId)
    window.clearTimeout(waiter.timeoutId)
    waiter.resolve()
  }, [])

  const waitForTemperaturePreviewEnded = useCallback((previewId: string): Promise<void> => {
    const existing = temperaturePreviewEndedWaitersRef.current.get(previewId)
    if (existing) return existing.promise

    let resolvePromise!: () => void
    let rejectPromise!: (error: Error) => void
    const promise = new Promise<void>((resolve, reject) => {
      resolvePromise = resolve
      rejectPromise = reject
    })
    const timeoutId = window.setTimeout(() => {
      const current = temperaturePreviewEndedWaitersRef.current.get(previewId)
      if (!current) return
      temperaturePreviewEndedWaitersRef.current.delete(previewId)
      current.reject(new Error(`temperature preview ${previewId} did not end`))
    }, TEMPERATURE_PREVIEW_STOP_TIMEOUT_MS)
    temperaturePreviewEndedWaitersRef.current.set(previewId, {
      promise,
      resolve: resolvePromise,
      reject: rejectPromise,
      timeoutId,
    })
    return promise
  }, [])

  const stopTemperaturePreviewId = useCallback(
    (previewId: string): Promise<void> => {
      const existing = temperaturePreviewStopPromisesRef.current.get(previewId)
      if (existing) return existing

      const task = (async () => {
        if (!mountedRef.current) {
          await call('stop_temperature_preview', { preview_id: previewId })
          if (temperaturePreviewIdRef.current === previewId) {
            temperaturePreviewIdRef.current = null
            temperaturePreviewOwnerGenerationRef.current = -1
          }
          return
        }
        // Register before sending stop: the backend may emit `ended`
        // synchronously before the correlated stop response reaches us.
        const ended = waitForTemperaturePreviewEnded(previewId)
        try {
          const result = await call<{ status?: string }>('stop_temperature_preview', {
            preview_id: previewId,
          })
          if (result?.status === 'not_running') settleTemperaturePreviewEnded(previewId)
          await ended
          if (temperaturePreviewIdRef.current === previewId) {
            temperaturePreviewIdRef.current = null
            temperaturePreviewOwnerGenerationRef.current = -1
            if (mountedRef.current) setTemperaturePreviewId(null)
          }
        } catch (err) {
          const waiter = temperaturePreviewEndedWaitersRef.current.get(previewId)
          if (waiter) {
            temperaturePreviewEndedWaitersRef.current.delete(previewId)
            window.clearTimeout(waiter.timeoutId)
            waiter.reject(err instanceof Error ? err : new Error(String(err)))
            await waiter.promise.catch(() => {})
          }
          throw err
        }
      })()
      temperaturePreviewStopPromisesRef.current.set(previewId, task)
      task.then(
        () => temperaturePreviewStopPromisesRef.current.delete(previewId),
        () => temperaturePreviewStopPromisesRef.current.delete(previewId)
      )
      return task
    },
    [settleTemperaturePreviewEnded, waitForTemperaturePreviewEnded]
  )

  const stopTemperaturePreviewAndWait = useCallback(async () => {
    // Wait for a pending create to return. Its generation check will stop a
    // late-created preview before this method continues.
    await temperaturePreviewLifecycleRef.current
    const activePreviewId = temperaturePreviewIdRef.current
    if (activePreviewId) await stopTemperaturePreviewId(activePreviewId)
  }, [stopTemperaturePreviewId])

  // Release every device reservation owned by this provider. A pending preview
  // create observes the bumped generation after it returns and stops its own
  // late-created resource.
  useEffect(() => {
    mountedRef.current = true
    const previewEndedWaiters = temperaturePreviewEndedWaitersRef.current
    return () => {
      mountedRef.current = false
      temperaturePreviewGenerationRef.current += 1
      const activePreviewId = temperaturePreviewIdRef.current
      const activeGuardId = guardIdRef.current
      const activeCaptureId = captureIdRef.current
      if (activePreviewId) {
        void stopTemperaturePreviewId(activePreviewId).catch(() => {})
      }
      if (activeGuardId) {
        void call('stop_temperature_guard', { guard_id: activeGuardId }).catch(() => {})
      }
      if (activeCaptureId) {
        void call('stop_capture', { capture_id: activeCaptureId }).catch(() => {})
      }
      for (const waiter of previewEndedWaiters.values()) {
        window.clearTimeout(waiter.timeoutId)
        waiter.resolve()
      }
      previewEndedWaiters.clear()
    }
  }, [stopTemperaturePreviewId])

  useEffect(() => {
    captureIdRef.current = captureId
  }, [captureId])

  useEffect(() => {
    guardIdRef.current = guardId
  }, [guardId])

  useEffect(() => {
    phaseRef.current = phase
  }, [phase])

  useEffect(() => {
    saveTemperatureGuardSettings(guardSettings)
  }, [guardSettings])

  useEffect(() => {
    saveAutoStopSettings(autoStopSettings)
  }, [autoStopSettings])

  useEffect(() => {
    saveDeviceOverlaySettings(overlaySettings)
  }, [overlaySettings])

  useEffect(() => {
    snapshotsRef.current = snapshots
  }, [snapshots])

  useEffect(() => {
    const unsubscribe = onEvent((msg) => {
      if (msg.event?.startsWith('temperature_preview_')) {
        const previewEvent = msg as unknown as TemperaturePreviewEvent
        if (!previewEvent.preview_id) return
        const isCurrentGeneration =
          previewEvent.preview_id === temperaturePreviewIdRef.current &&
          temperaturePreviewOwnerGenerationRef.current ===
            temperaturePreviewGenerationRef.current

        if (previewEvent.event === 'temperature_preview_ended') {
          // Always resolve teardown, even for a stale generation. Ignoring a
          // late `ended` here would deadlock a new guard/capture start.
          settleTemperaturePreviewEnded(previewEvent.preview_id)
          if (temperaturePreviewIdRef.current === previewEvent.preview_id) {
            temperaturePreviewIdRef.current = null
            temperaturePreviewOwnerGenerationRef.current = -1
            setTemperaturePreviewId(null)
          }
          if (isCurrentGeneration) {
            setTemperaturePreviewSample(null)
            setTemperaturePreviewLoading(false)
          }
          return
        }

        if (!isCurrentGeneration) return

        if (previewEvent.event === 'temperature_preview_state') {
          if (previewEvent.state === 'preparing' || previewEvent.state === 'running') {
            setTemperaturePreviewLoading(true)
          } else if (previewEvent.state === 'error') {
            setTemperaturePreviewSample(null)
            setTemperaturePreviewLoading(false)
          }
          return
        }

        if (previewEvent.event === 'temperature_preview_sample') {
          setTemperaturePreviewSample({
            temperatureC: previewEvent.temperature_c,
            sampledAt: previewEvent.sampled_at,
            source: previewEvent.source,
          })
          setTemperaturePreviewLoading(false)
          setTemperaturePreviewError(null)
          return
        }

        setTemperaturePreviewLoading(false)
        setTemperaturePreviewSample(null)
        setTemperaturePreviewError(
          previewEvent.code
            ? `${previewEvent.code}: ${previewEvent.error}`
            : previewEvent.error
        )
        return
      }

      if (msg.event?.startsWith('temperature_guard_')) {
        const guardEvent = msg as unknown as TemperatureGuardEvent
        if (!guardEvent.guard_id || guardEvent.guard_id !== guardIdRef.current) return

        if (guardEvent.event === 'temperature_guard_state') {
          if (cancelRequestedRef.current || phaseRef.current === 'stopping') return
          if (
            guardEvent.state === 'preparing' ||
            guardEvent.state === 'waiting' ||
            guardEvent.state === 'triggering'
          ) {
            setStarting(guardEvent.state === 'preparing')
            transitionTo(guardEvent.state)
          }
          return
        }

        if (guardEvent.event === 'temperature_guard_sample') {
          setGuardSample({
            temperatureC: guardEvent.temperature_c,
            sampledAt: guardEvent.sampled_at,
            source: guardEvent.source,
            thresholdC: guardEvent.threshold_c,
            consecutiveHits: guardEvent.consecutive_hits,
            requiredHits: guardEvent.required_hits,
          })
          return
        }

        if (guardEvent.event === 'temperature_guard_started') {
          // The guard and resulting capture are one logical operation. Update
          // both refs synchronously so a snapshot delivered immediately after
          // this event can never be mistaken for a stale capture.
          updateGuardId(null)
          updateCaptureId(guardEvent.capture_id)
          if (guardEvent.session_id) sessionIdRef.current = guardEvent.session_id
          setDevice(guardEvent.device)
          setStarting(false)
          startInFlightRef.current = false
          if (cancelRequestedRef.current) {
            transitionTo('stopping')
            void call('stop_capture', { capture_id: guardEvent.capture_id }).catch(() => {})
          } else {
            // The guard can sit at `waiting` for minutes; the HUD the user set
            // during that wait, not the one frozen into create_temperature_guard,
            // is what this capture must run with.
            reconcileOverlayForCapture(guardEvent.capture_id)
            transitionTo('running')
          }
          return
        }

        if (guardEvent.event === 'temperature_guard_error') {
          updateGuardId(null)
          startInFlightRef.current = false
          setStarting(false)
          setStopping(false)
          setError(
            guardEvent.code
              ? `${guardEvent.code}: ${guardEvent.error}`
              : guardEvent.error
          )
          transitionTo('error')
          return
        }

        updateGuardId(null)
        startInFlightRef.current = false
        setStarting(false)
        setStopping(false)
        setGuardSnapshot(null)
        cancelRequestedRef.current = false
        if (phaseRef.current !== 'error') transitionTo('idle')
        return
      }

      // Keep evidence even if capture ends while paused, without a later sample.
      // Only acknowledged collector events mark the session; a failed click cannot.
      if (msg.capture_id === captureIdRef.current &&
          (msg.pause_used === true || (msg.event === 'capture_state' && msg.paused === true)) &&
          !completeSnapshotsRef.current[0]?.pause_used) {
        completeSnapshotsRef.current = completeSnapshotsRef.current.map(s => ({ ...s, pause_used: true }))
        snapshotsRef.current = snapshotsRef.current.map(s => ({ ...s, pause_used: true }))
        setSnapshots(snapshotsRef.current)
      }
      if (msg.event === 'capture_state' && msg.capture_id === captureIdRef.current) {
        const paused = msg.paused === true
        if (paused) setSampleRetrying(false)
        isPausedRef.current = paused
        setIsPaused(paused)
        const now = Date.now()
        if (typeof msg.elapsed_sec === 'number' && captureStartRef.current !== null) {
          const anchor = now - msg.elapsed_sec * 1000
          captureStartRef.current = anchor
          setCaptureStart(anchor)
          setNowTick(now)
        }
        if (phaseRef.current !== 'stopping') transitionTo(paused ? 'paused' : 'running')
        return
      }
      if (msg.event === 'snapshot' && msg.capture_id === captureIdRef.current) {
        // Anchor the elapsed clock to the first received sample so 0:00 lines up
        // with the first collected data point (and the chart's t=0), instead of
        // counting the pre-sample tunnel/DVT handshake time.
        const now = Date.now()
        if (captureStartRef.current === null) captureStartRef.current = now
        setCaptureStart((prev) => prev ?? now)
        setNowTick((prev) => (prev === 0 ? now : prev))
        const data = msg.data as MetricsSnapshot
        const snapshot = completeSnapshotsRef.current.at(-1)?.pause_used === true
          ? { ...data, pause_used: true } : data
        completeSnapshotsRef.current.push(snapshot)
        const next = [...snapshotsRef.current, snapshot]
        const intervalSec =
          capturePlatformRef.current === 'ios'
            ? IOS_SAMPLE_INTERVAL_SEC
            : ANDROID_SAMPLE_INTERVAL_SEC
        const cutoff = Math.ceil((ROLLING_WINDOW_MIN * 60) / intervalSec)
        const kept = next.length > cutoff ? next.slice(next.length - cutoff) : next
        // capture_ended can follow this event before React flushes state. Keep
        // the event-stream ref authoritative so a one-sample capture persists.
        snapshotsRef.current = kept
        setSnapshots(kept)
        setSampleRetrying(false)
        setStarting(false)
        if (!isPausedRef.current && phaseRef.current !== 'stopping' && phaseRef.current !== 'running') transitionTo('running')
      } else if (msg.event === 'capture_error' && msg.capture_id === captureIdRef.current) {
        if (msg.recoverable === true) {
          if (phaseRef.current === 'running') setSampleRetrying(true)
          return
        }
        setSampleRetrying(false)
        setError(`capture error: ${msg.error}`)
        setStarting(false)
        transitionTo('error')
      } else if (msg.event === 'capture_ended' && msg.capture_id === captureIdRef.current) {
        const endedCaptureId = msg.capture_id
        const targetAppTerminationError =
          typeof msg.target_app_termination_error === 'string'
            ? msg.target_app_termination_error
            : null
        const savedCuts = closeLiveCut(cutsRef.current, activeCutRef.current, completeSnapshotsRef.current)
        cutsRef.current = savedCuts
        setCuts(savedCuts)
        activeCutRef.current = null
        setActiveCut(null)
        isPausedRef.current = false
        setIsPaused(false)
        updateCaptureId(null)
        startInFlightRef.current = false
        setStopping(false)
        setGuardSnapshot(null)
        cancelRequestedRef.current = false
        if (targetAppTerminationError) {
          setError(`target-app-termination-failed: ${targetAppTerminationError}`)
          transitionTo('error')
        } else if (phaseRef.current !== 'error') {
          transitionTo('idle')
        }
        // Freeze the elapsed timer at the moment capture actually ended.
        const reportedElapsed =
          typeof msg.elapsed_sec === 'number' && Number.isFinite(msg.elapsed_sec)
            ? Math.max(0, msg.elapsed_sec)
            : null
        if (
          captureStartRef.current !== null &&
          reportedElapsed !== null
        ) {
          setNowTick(captureStartRef.current + reportedElapsed * 1000)
        } else {
          setNowTick(Date.now())
        }
        const snaps = completeSnapshotsRef.current
        if (msg.render_pipeline && snaps.length > 0) {
          const final = msg.render_pipeline as RenderPipeline
          const last = snaps[snaps.length - 1]
          const points = new Map((last.render_pipeline?.points ?? []).map(point => [point.bucket_id, point]))
          for (const point of final.points ?? []) {
            if ((points.get(point.bucket_id)?.revision ?? 0) < point.revision) points.set(point.bucket_id, point)
          }
          const render_pipeline = { ...final, points: [...points.values()].sort((a, b) => a.bucket_id - b.bucket_id).slice(-40) }
          snaps[snaps.length - 1] = { ...last, render_pipeline }
          snapshotsRef.current = snapshotsRef.current.map((sample, index, displayed) =>
            index === displayed.length - 1 ? { ...sample, render_pipeline } : sample)
          setSnapshots(snapshotsRef.current)
        }
        const shouldSend = getSendOnSave() && Boolean(getEndpointUrl())
        if (snaps.length > 0) {
          const sessionId = sessionIdRef.current || endedCaptureId
          const ownerId = sessionOwnerRef.current
          const name = buildDefaultSessionName(snaps)
          sessionNameRef.current = name
          // Auto-save the raw CSV (Documents/MPI/Raw) immediately — this
          // must never be lost if endpoint transfer fails. Best-effort / fire-and-forget.
          // Persist a default session name so the dashboard's session library shows
          // a meaningful label (the CSV's session.name), not just the file name.
          const rawSave = call('auto_save_raw_csv', {
            capture_id: endedCaptureId,
            session_id: sessionId,
            snapshots: snaps,
            session_name: name,
            cuts: savedCuts,
          }).catch(() => {})
          const cloudSave = saveSnapshotsToCloud(snaps, sessionId, ownerId, name, messagesRef.current, undefined, savedCuts)
            .then((failure) => { if (failure && mountedRef.current) setError(failure) })
          // Keep every pending session in the close barrier while allowing the
          // newer session's disk and cloud work to start independently.
          capturePersistenceRef.current = Promise.all([
            capturePersistenceRef.current, rawSave, cloudSave,
          ]).then(() => {})
          // Defer endpoint transfer behind a naming step only when an actual
          // enabled destination is configured.
          if (shouldSend) {
            setPendingSave({
              snapshots: snaps,
              defaultName: name,
              sessionId,
              ownerId,
              cuts: savedCuts,
            })
          }
        }
        closeCaptureEndedRef.current?.()
        closeCaptureEndedRef.current = null
      }
    })
    return () => unsubscribe()
    // Subscribe once on mount: the handler reads live state via refs
    // (captureIdRef/snapshotsRef) and only calls stable setState/module helpers,
    // so it never needs to re-subscribe (and the naming modal is opened via
    // setPendingSave, not read here).
  }, [
    reconcileOverlayForCapture,
    settleTemperaturePreviewEnded,
    transitionTo,
    updateCaptureId,
    updateGuardId,
  ])

  useEffect(() => {
    const eligible =
      guardSettings.enabled && phase === 'idle' && serial !== null && bundleId !== null
    if (!eligible) return

    const generation = temperaturePreviewGenerationRef.current
    const target = { platform, serial, package: bundleId }
    const previousLifecycle = temperaturePreviewLifecycleRef.current

    const isCurrent = () =>
      mountedRef.current && generation === temperaturePreviewGenerationRef.current

    const lifecycle = (async () => {
      let createdPreviewId: string | null = null
      try {
        // Serialize replacement previews. A device stays reserved until its
        // `ended` event, so creating the next preview any earlier races into
        // device_busy.
        await previousLifecycle
        if (!isCurrent()) return
        setTemperaturePreviewLoading(true)

        const lingeringPreviewId = temperaturePreviewIdRef.current
        if (lingeringPreviewId) await stopTemperaturePreviewId(lingeringPreviewId)
        if (!isCurrent()) return

        const result = await call<CreateTemperaturePreviewResult>(
          'create_temperature_preview',
          target,
          120_000
        )
        createdPreviewId = result.preview_id
        // Track the id immediately even when this response is already stale, so
        // a failed cleanup can be retried by the next lifecycle/start.
        temperaturePreviewIdRef.current = result.preview_id

        if (!isCurrent()) {
          await stopTemperaturePreviewId(result.preview_id)
          return
        }

        temperaturePreviewOwnerGenerationRef.current = generation
        setTemperaturePreviewId(result.preview_id)
        setDevice(result.device)
        await call('activate_temperature_preview', { preview_id: result.preview_id })

        if (!isCurrent()) await stopTemperaturePreviewId(result.preview_id)
      } catch (err) {
        if (
          createdPreviewId &&
          temperaturePreviewIdRef.current === createdPreviewId
        ) {
          await stopTemperaturePreviewId(createdPreviewId).catch(() => {})
        }
        if (isCurrent()) {
          setTemperaturePreviewLoading(false)
          setTemperaturePreviewError(err instanceof Error ? err.message : String(err))
        }
      }
    })()
    // `lifecycle` handles its own errors and therefore stays safe to await from
    // start(), cleanup, and the next serialized preview.
    temperaturePreviewLifecycleRef.current = lifecycle

    return () => {
      const cleanup = (async () => {
        await lifecycle
        const activePreviewId = temperaturePreviewIdRef.current
        if (
          activePreviewId &&
          temperaturePreviewOwnerGenerationRef.current === generation
        ) {
          await stopTemperaturePreviewId(activePreviewId)
        }
      })().catch(() => {})
      temperaturePreviewLifecycleRef.current = cleanup
    }
    // Deliberately exclude thresholdC: changing only the threshold must not
    // restart the read-only temperature preview.
  }, [
    bundleId,
    guardSettings.enabled,
    phase,
    platform,
    serial,
    stopTemperaturePreviewId,
    temperaturePreviewRetryNonce,
  ])

  const latest = snapshots.at(-1) ?? null
  const isRunning = captureId !== null

  // Drive the elapsed-time display from wall-clock time, not from the count of
  // samples received. iOS samples can arrive irregularly (or lag during the
  // one-off tunnel handshake), so a sum-of-durations clock could read 1 s after
  // 30 s of real capture. Tick only while running; the last value stays frozen
  // after the capture ends.
  useEffect(() => {
    if (!isRunning || isPaused) return
    const id = window.setInterval(() => setNowTick(Date.now()), 500)
    return () => window.clearInterval(id)
  }, [isRunning, isPaused])

  const elapsedSec = useMemo(() => {
    if (captureStart === null) return 0
    return Math.max(0, (nowTick - captureStart) / 1000)
  }, [captureStart, nowTick])

  const operationIsLocked = useCallback(() => {
    return (
      guardIdRef.current !== null ||
      captureIdRef.current !== null ||
      (phaseRef.current !== 'idle' && phaseRef.current !== 'error')
    )
  }, [])

  const setPlatform = useCallback((nextPlatform: Platform) => {
    if (operationIsLocked()) return
    invalidateTemperaturePreview()
    setPlatformState(nextPlatform)
    setSerial(null)
    setDevice(null)
    setBundleIdState(null)
    // The selection is gone, so no in-flight probe owns the card any more.
    overlayTargetSerialRef.current = null
    setOverlayStatus(null)
    setSnapshots([])
    cutsRef.current = []
    setCuts([])
    activeCutRef.current = null
    setActiveCut(null)
    isPausedRef.current = false
    setIsPaused(false)
    snapshotsRef.current = []
    completeSnapshotsRef.current = []
    sessionIdRef.current = crypto.randomUUID()
    sessionOwnerRef.current = null
    sessionNameRef.current = null
    captureStartRef.current = null
    setCaptureStart(null)
    setNowTick(0)
    setError(null)
    transitionTo('idle')
  }, [invalidateTemperaturePreview, operationIsLocked, transitionTo])

  // Selection checks presence and an already-running helper, without permission or package changes.
  const probeOverlaySupport = useCallback(async (probeSerial: string, connect = false) => {
    const revision = ++overlayProbeRevisionRef.current
    const current = () => mountedRef.current && overlayTargetSerialRef.current === probeSerial &&
      overlayProbeRevisionRef.current === revision
    try {
      const result = await call<CompanionOverlayStatusResult>(
        'companion_overlay_status',
        connect ? { serial: probeSerial, connect: true } : { serial: probeSerial },
        30_000
      )
      if (current()) setOverlayStatus(toDeviceOverlayStatus(result))
    } catch {
      if (current()) setOverlayStatus({ available: false, reason: 'device_unreachable' })
    }
  }, [])

  const checkPlayCompanion = useCallback((connect = false) => {
    const selected = overlayTargetSerialRef.current
    return selected ? probeOverlaySupport(selected, connect) : Promise.resolve()
  }, [probeOverlaySupport])

  const setDeviceSelection = useCallback((nextSerial: string | null, nextDevice: DeviceInfo | null) => {
    if (operationIsLocked()) return
    invalidateTemperaturePreview()
    setSerial(nextSerial)
    setDevice(nextDevice)
    setBundleIdState(null)
    // Auto-detect platform from whichever device was selected, so the user no
    // longer picks Android/iOS by hand — the unified device list drives it.
    if (nextDevice) setPlatformState(nextDevice.platform)

    // A status describes exactly one phone, so drop it on EVERY change —
    // Android→Android included. Otherwise phone A's red warning stays on screen
    // over phone B's card until B's probe answers, and forever if that probe
    // rejects (the probe swallows failures by design). Saying nothing is the
    // only honest state while the new answer is in flight.
    overlayTargetSerialRef.current = nextSerial
    setOverlayStatus(null)

    if (nextDevice?.platform === 'android' && nextSerial) void probeOverlaySupport(nextSerial)
  }, [invalidateTemperaturePreview, operationIsLocked, probeOverlaySupport])

  const setBundleId = useCallback((nextBundleId: string | null) => {
    if (operationIsLocked()) return
    invalidateTemperaturePreview()
    setBundleIdState(nextBundleId)
  }, [invalidateTemperaturePreview, operationIsLocked])

  const setAutoStopEnabled = useCallback(
    (enabled: boolean) => {
      if (operationIsLocked()) return
      setAutoStopSettings((prev) => ({ ...prev, enabled }))
    },
    [operationIsLocked]
  )

  const setAutoStopAfterSec = useCallback(
    (afterSec: number) => {
      if (operationIsLocked()) return
      setAutoStopSettings((prev) => ({
        ...prev,
        afterSec: normalizeAutoStopAfterSec(afterSec),
      }))
    },
    [operationIsLocked]
  )

  // The HUD is the one capture setting that stays editable while measuring, so
  // a change also has to reach the capture already holding the device. Fire and
  // forget: a warm connect to the companion can legitimately fail (it was never
  // started, or the user revoked the overlay permission) and must never surface
  // as a capture error.
  const pushOverlayToRunningCapture = useCallback((next: DeviceOverlaySettings) => {
    const activeCaptureId = captureIdRef.current
    if (!activeCaptureId) {
      // No capture to talk to yet — park the change instead of dropping it.
      // start_capture's round trip and the guard's threshold wait are both
      // windows where the card is live but the id does not exist.
      overlayPendingPushRef.current = next
      return
    }
    overlayPendingPushRef.current = null
    enqueueOverlayPush(activeCaptureId, next)
  }, [enqueueOverlayPush])

  const setDeviceOverlayEnabled = useCallback(
    (enabled: boolean) => {
      const next = { ...overlaySettingsRef.current, enabled }
      overlaySettingsRef.current = next
      setOverlaySettings(next)
      pushOverlayToRunningCapture(next)
      // Re-probe on the way ON: the companion may have finished installing, or
      // the user may have granted "Display over other apps" since the last answer.
      // The engine downgrades this to a warm-only check while a capture is
      // running, so it cannot foreground the companion over the measured app.
      if (enabled && serial && platform === 'android') probeOverlaySupport(serial)
    },
    [platform, probeOverlaySupport, pushOverlayToRunningCapture, serial]
  )

  const setDeviceOverlayMetric = useCallback(
    (key: DeviceOverlayMetricKey, enabled: boolean) => {
      const next = {
        ...overlaySettingsRef.current,
        metrics: { ...overlaySettingsRef.current.metrics, [key]: enabled },
      }
      overlaySettingsRef.current = next
      setOverlaySettings(next)
      pushOverlayToRunningCapture(next)
    },
    [pushOverlayToRunningCapture]
  )

  const setTemperatureGuardEnabled = useCallback(
    (enabled: boolean) => {
      if (operationIsLocked()) return
      invalidateTemperaturePreview()
      setGuardSettings((prev) => ({ ...prev, enabled }))
    },
    [invalidateTemperaturePreview, operationIsLocked]
  )

  const setTemperatureGuardThresholdC = useCallback(
    (thresholdC: number) => {
      if (operationIsLocked()) return
      setGuardSettings((prev) => ({
        ...prev,
        thresholdC: normalizeTemperatureThreshold(thresholdC),
      }))
    },
    [operationIsLocked]
  )

  const retryTemperaturePreview = useCallback(() => {
    if (operationIsLocked()) return
    invalidateTemperaturePreview()
    setTemperaturePreviewRetryNonce((prev) => prev + 1)
  }, [invalidateTemperaturePreview, operationIsLocked])

  const clearError = useCallback(() => {
    setError(null)
    if (!guardIdRef.current && !captureIdRef.current && phaseRef.current === 'error') {
      transitionTo('idle')
    }
  }, [transitionTo])

  const settleCancelledStart = useCallback(
    (operation: number): boolean => {
      if (operation === operationRef.current) return false
      if (
        cancelRequestedRef.current &&
        phaseRef.current === 'stopping' &&
        guardIdRef.current === null &&
        captureIdRef.current === null
      ) {
        startInFlightRef.current = false
        setStarting(false)
        setStopping(false)
        setGuardSnapshot(null)
        transitionTo('idle')
      }
      return true
    },
    [transitionTo]
  )

  const start = useCallback(async () => {
    if (closingRef.current || !serial || !bundleId || startInFlightRef.current || operationIsLocked()) return
    // Lock the platform for this operation. Snapshot payloads from older/test
    // producers may omit device metadata, while the selected capture platform
    // is already authoritative and cannot change until teardown completes.
    capturePlatformRef.current = platform
    const configuredAutoStopAfterSec = autoStopSettings.enabled
      ? normalizeAutoStopAfterSec(autoStopSettings.afterSec)
      : null
    // Android-only: the HUD lives in the companion app, which only exists on the
    // Android side. Send nothing at all for iOS rather than a disabled config.
    const configuredOverlay =
      platform === 'android'
        ? {
            enabled: overlaySettingsRef.current.enabled,
            metrics: deviceOverlayMetricList(overlaySettingsRef.current),
          }
        : null
    startInFlightRef.current = true
    cancelRequestedRef.current = false
    // configuredOverlay above is the current setting, so whatever was parked
    // earlier is now in flight with the start command. Only a toggle from here
    // on is outstanding and worth replaying once the capture id exists.
    overlayPendingPushRef.current = null
    const operation = ++operationRef.current
    invalidateTemperaturePreview()
    setError(null)
    snapshotsRef.current = []
    completeSnapshotsRef.current = []
    sessionIdRef.current = crypto.randomUUID()
    sessionOwnerRef.current = null
    sessionNameRef.current = null
    setSnapshots([])
    cutsRef.current = []
    setCuts([])
    activeCutRef.current = null
    setActiveCut(null)
    isPausedRef.current = false
    setIsPaused(false)
    captureStartRef.current = null
    setCaptureStart(null)
    setNowTick(0)
    setGuardSample(null)
    setStarting(true)
    setStopping(false)
    transitionTo('preparing')

    try {
      // Freeze the original account before capture/guard creation. Account
      // changes in another measurement window cannot reassign this session.
      const cloud = await cloudCommand('cloud-status').catch(() => null)
      sessionOwnerRef.current = cloud?.signedIn && cloud.enabled ? cloud.userId : null
      // The preview owns the same device reservation as the guard/capture.
      // Do not create the next backend resource until `preview_ended` confirms
      // that reservation has actually been released.
      await stopTemperaturePreviewAndWait()
    } catch (err) {
      if (settleCancelledStart(operation)) return
      setError(err instanceof Error ? err.message : String(err))
      setStarting(false)
      setStopping(false)
      startInFlightRef.current = false
      transitionTo('error')
      return
    }
    if (settleCancelledStart(operation)) return

    const durationSec = platform === 'ios' ? IOS_SAMPLE_INTERVAL_SEC : ANDROID_SAMPLE_INTERVAL_SEC

    if (guardSettings.enabled) {
      const lockedDevice = device ? Object.freeze({ ...device }) : null
      const lockedSnapshot: TemperatureGuardSnapshot = {
        platform,
        serial,
        device: lockedDevice,
        bundleId,
        thresholdC: guardSettings.thresholdC,
        durationSec,
      }
      Object.freeze(lockedSnapshot)
      setGuardSnapshot(lockedSnapshot)

      let createdGuardId: string | null = null
      try {
        const result = await call<CreateTemperatureGuardResult>(
          'create_temperature_guard',
          {
            platform: lockedSnapshot.platform,
            serial: lockedSnapshot.serial,
            package: lockedSnapshot.bundleId,
            threshold_c: lockedSnapshot.thresholdC,
            required_hits: TEMPERATURE_GUARD_REQUIRED_HITS,
            cold_start: true,
            duration_sec: lockedSnapshot.durationSec,
            ...(configuredAutoStopAfterSec !== null
              ? { auto_stop_after_sec: configuredAutoStopAfterSec }
              : {}),
            ...(configuredOverlay !== null ? { overlay: configuredOverlay } : {}),
          },
          120_000
        )
        createdGuardId = result.guard_id

        if (operation !== operationRef.current || cancelRequestedRef.current) {
          await call('stop_temperature_guard', { guard_id: result.guard_id }).catch(() => {})
          if (phaseRef.current === 'stopping') {
            setStarting(false)
            setStopping(false)
            startInFlightRef.current = false
            setGuardSnapshot(null)
            transitionTo('idle')
          }
          return
        }

        updateGuardId(result.guard_id)
        setDevice(result.device)
        await call('activate_temperature_guard', { guard_id: result.guard_id })
        // State/sample/started events own all subsequent transitions. In
        // particular, never start the elapsed timer from this command response.
      } catch (err) {
        // A start event can win the race with the activate response. Once a
        // capture id exists, its events are authoritative and a late response
        // failure must not tear down the running capture UI.
        if (settleCancelledStart(operation) || captureIdRef.current) return
        if (createdGuardId) {
          await call('stop_temperature_guard', { guard_id: createdGuardId }).catch(() => {})
        }
        updateGuardId(null)
        setError(err instanceof Error ? err.message : String(err))
        setStarting(false)
        setStopping(false)
        startInFlightRef.current = false
        transitionTo('error')
      }
      return
    }

    setGuardSnapshot(null)
    try {
      const result = await call<StartCaptureResult>('start_capture', {
        platform,
        serial,
        package: bundleId,
        duration_sec: durationSec,
        ...(configuredAutoStopAfterSec !== null
          ? { auto_stop_after_sec: configuredAutoStopAfterSec }
          : {}),
        ...(configuredOverlay !== null ? { overlay: configuredOverlay } : {}),
      })
      if (operation !== operationRef.current || cancelRequestedRef.current) {
        await call('stop_capture', { capture_id: result.capture_id }).catch(() => {})
        startInFlightRef.current = false
        setStarting(false)
        setStopping(false)
        transitionTo('idle')
        return
      }
      updateCaptureId(result.capture_id)
      if (result.session_id) sessionIdRef.current = result.session_id
      // A toggle during start_capture's round trip had no id to target; apply it
      // now so the capture does not run with the pre-press setting.
      reconcileOverlayForCapture(result.capture_id)
      setDevice(result.device)
      // Leave captureStart null until the first sample arrives — the elapsed clock
      // starts from the first collected data point, not the button press, so it
      // does not count the up-to-90 s iOS tunnel/DVT handshake before data flows.
      setStarting(false)
      startInFlightRef.current = false
      transitionTo('running')
    } catch (err) {
      if (settleCancelledStart(operation)) return
      setError(err instanceof Error ? err.message : String(err))
      setStarting(false)
      startInFlightRef.current = false
      transitionTo('error')
    }
  }, [
    bundleId,
    autoStopSettings.afterSec,
    autoStopSettings.enabled,
    device,
    guardSettings.enabled,
    guardSettings.thresholdC,
    invalidateTemperaturePreview,
    operationIsLocked,
    platform,
    reconcileOverlayForCapture,
    serial,
    settleCancelledStart,
    stopTemperaturePreviewAndWait,
    transitionTo,
    updateCaptureId,
    updateGuardId,
  ])

  const stop = useCallback(async () => {
    const activeGuardId = guardIdRef.current
    const activeCaptureId = captureIdRef.current
    if (!activeGuardId && !activeCaptureId && !startInFlightRef.current) return
    cancelRequestedRef.current = true
    operationRef.current += 1
    setStopping(true)
    setStarting(false)
    transitionTo('stopping')
    try {
      if (activeGuardId) {
        await call('stop_temperature_guard', { guard_id: activeGuardId })
      } else if (activeCaptureId) {
        await call('stop_capture', { capture_id: activeCaptureId })
      }
      // A pending create has no id to cancel yet. Its completion path observes
      // operationRef/cancelRequestedRef and immediately stops the returned guard.
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setStopping(false)
      startInFlightRef.current = false
      transitionTo('error')
    }
  }, [transitionTo])

  useEffect(() => {
    if (!window.api) return
    void window.api.invoke('account-view-ready').catch(() => {})
    const prepare = (accountChange: boolean) => {
      if (closingRef.current) return
      closingRef.current = true
      void (async () => {
        let timer: ReturnType<typeof setTimeout> | undefined
        try {
          const ended = new Promise<void>((resolve) => {
            closeCaptureEndedRef.current = resolve
            timer = setTimeout(resolve, 10_000)
          })
          // Request teardown while the renderer and its full capture data still
          // exist. Bounded fallback also covers a failed sidecar during close.
          if (accountChange) await stopTemperaturePreviewAndWait().catch(() => {})
          await Promise.race([stop().catch(() => {}), ended])
          if (captureIdRef.current) await ended
          if (timer) clearTimeout(timer)
          if (captureIdRef.current && completeSnapshotsRef.current.length > 0) {
            const fallbackCaptureId = captureIdRef.current
            const sessionId = sessionIdRef.current
            const full = completeSnapshotsRef.current.slice()
            const name = sessionNameRef.current || buildDefaultSessionName(full)
            // A delayed partial backup has its own recovery folder, so it cannot
            // overwrite or hold up a later complete CSV from capture_ended.
            const rawSave = call('auto_save_raw_csv', {
              capture_id: fallbackCaptureId, session_id: sessionId,
              snapshots: full, session_name: name, close_backup: true,
              cuts: closeLiveCut(cutsRef.current, activeCutRef.current, full),
            }).catch(() => {})
            await Promise.all([
              rawSave,
              saveSnapshotsToCloud(full, sessionId, sessionOwnerRef.current, name, messagesRef.current,
                // A final save may complete while this partial payload is built.
                () => captureIdRef.current === fallbackCaptureId,
                closeLiveCut(cutsRef.current, activeCutRef.current, full)),
            ])
          }
          await capturePersistenceRef.current
        } finally {
          if (timer) clearTimeout(timer)
          closeCaptureEndedRef.current = null
          if (accountChange) closingRef.current = false
          await window.api.invoke(accountChange ? 'account-change-ready' : 'window-close-ready').catch(() => {})
        }
      })()
    }
    const close = window.api.receive('prepare-window-close', () => prepare(false))
    const change = window.api.receive('prepare-account-change', () => prepare(true))
    return () => { close(); change() }
  }, [stop, stopTemperaturePreviewAndWait])

  const exportCsv = useCallback(async () => {
    if (snapshots.length === 0) return
    const completedBeforeExport = captureIdRef.current === null && !startInFlightRef.current
    const exportSnapshots = completeSnapshotsRef.current.slice()
    const exportCuts = closeLiveCut(cutsRef.current, activeCutRef.current, exportSnapshots)
    const sessionId = sessionIdRef.current
    const ownerId = sessionOwnerRef.current
    const name = sessionNameRef.current || buildDefaultSessionName(exportSnapshots)
    const target = await saveFile({
      defaultPath: `mpi_${device?.display_name || device?.model || 'live'}_${Date.now()}.csv`,
      filters: [{ name: 'CSV', extensions: ['csv'] }],
    })
    if (!target) return
    setExportingCsv(true)
    try {
      await call('export_live_csv', { save_path: target, snapshots: exportSnapshots, session_id: sessionId, session_name: name, cuts: exportCuts })
      const cloudFailure = completedBeforeExport
        ? await saveSnapshotsToCloud(exportSnapshots, sessionId, ownerId, name, t, undefined, exportCuts) : null
      if (cloudFailure) setError(cloudFailure)
      // Also POST once when endpoint transfer is enabled and configured.
      const res = await sendSnapshotsToEndpoint(exportSnapshots, t, name, sessionId, exportCuts)
      if (res && !res.ok) {
        setError(res.error ?? t.capture.dataSendFailed)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setExportingCsv(false)
    }
  }, [device?.display_name, device?.model, snapshots, t])

  const exportXlsx = useCallback(async () => {
    if (snapshots.length === 0) return
    const completedBeforeExport = captureIdRef.current === null && !startInFlightRef.current
    const exportSnapshots = completeSnapshotsRef.current.slice()
    const exportCuts = closeLiveCut(cutsRef.current, activeCutRef.current, exportSnapshots)
    const sessionId = sessionIdRef.current
    const ownerId = sessionOwnerRef.current
    const name = sessionNameRef.current || buildDefaultSessionName(exportSnapshots)
    const target = await saveFile({
      defaultPath: `mpi_${device?.display_name || device?.model || 'live'}_${Date.now()}_summary.xlsx`,
      filters: [{ name: 'Excel', extensions: ['xlsx'] }],
    })
    if (!target) return
    setExportingXlsx(true)
    try {
      await call('export_live_xlsx', { save_path: target, snapshots: exportSnapshots, session_id: sessionId, session_name: name, cuts: exportCuts })
      const cloudFailure = completedBeforeExport
        ? await saveSnapshotsToCloud(exportSnapshots, sessionId, ownerId, name, t, undefined, exportCuts) : null
      if (cloudFailure) setError(cloudFailure)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setExportingXlsx(false)
    }
  }, [device?.display_name, device?.model, snapshots, t])

  // Transfer the deferred session under the chosen name (blank → the default).
  const confirmSessionSave = useCallback(
    async (name: string) => {
      if (!pendingSave) return
      setSavingSession(true)
      try {
        const savedName = name.trim() || pendingSave.defaultName
        sessionNameRef.current = savedName
        const cloudFailure = await saveSnapshotsToCloud(
          pendingSave.snapshots, pendingSave.sessionId, pendingSave.ownerId, savedName, t, undefined, pendingSave.cuts,
        )
        if (cloudFailure) setError(cloudFailure)
        const res = await sendSnapshotsToEndpoint(
          pendingSave.snapshots,
          t,
          savedName,
          pendingSave.sessionId,
          pendingSave.cuts,
        )
        if (res && !res.ok) setError(res.error ?? t.capture.dataSendFailed)
      } finally {
        setSavingSession(false)
        setPendingSave(null)
      }
    },
    [pendingSave, t]
  )

  // Skip naming edits and transfer with the prefilled default.
  const skipSessionSave = useCallback(() => {
    if (pendingSave) void confirmSessionSave(pendingSave.defaultName)
  }, [pendingSave, confirmSessionSave])

  const togglePause = useCallback(async () => {
    const id = captureIdRef.current
    if (!id || pausePendingRef.current || phaseRef.current === 'stopping') return
    pausePendingRef.current = true
    setPausePending(true)
    try {
      await call(isPausedRef.current ? 'resume_capture' : 'pause_capture', { capture_id: id })
    } catch (err) {
      if (captureIdRef.current === id) setError(err instanceof Error ? err.message : String(err))
    } finally {
      pausePendingRef.current = false
      setPausePending(false)
    }
  }, [])

  const toggleCut = useCallback(() => {
    if (!captureIdRef.current || isPausedRef.current || phaseRef.current !== 'running') return
    const full = completeSnapshotsRef.current
    if (!full.length) return
    if (activeCutRef.current) {
      const next = closeLiveCut(cutsRef.current, activeCutRef.current, full)
      if (next === cutsRef.current) return
      cutsRef.current = next
      setCuts(next)
      activeCutRef.current = null
      setActiveCut(null)
    } else if (cutsRef.current.length < 200) {
      const cut = { id: crypto.randomUUID(), name: `${t.capture.cutDefault} ${cutsRef.current.length + 1}`, start_sec: snapshotElapsed(full, full.length - 1) }
      activeCutRef.current = cut
      setActiveCut(cut)
    }
  }, [t.capture.cutDefault])

  const selectionLocked =
    guardId !== null ||
    captureId !== null ||
    (phase !== 'idle' && phase !== 'error')

  const value = useMemo<LiveCaptureState>(
    () => ({
      phase,
      platform,
      serial,
      device,
      bundleId,
      captureId,
      snapshots,
      latest,
      elapsedSec,
      error,
      sampleRetrying,
      exportingCsv,
      exportingXlsx,
      starting,
      stopping,
      isRunning,
      isPaused,
      pausePending,
      cuts,
      activeCut,
      togglePause,
      toggleCut,
      selectionLocked,
      autoStopEnabled: autoStopSettings.enabled,
      autoStopAfterSec: autoStopSettings.afterSec,
      deviceOverlayEnabled: overlaySettings.enabled,
      deviceOverlayMetrics: overlaySettings.metrics,
      deviceOverlayStatus: overlayStatus,
      checkPlayCompanion,
      temperatureGuardEnabled: guardSettings.enabled,
      temperatureGuardThresholdC: guardSettings.thresholdC,
      guardId,
      guardSample,
      guardSnapshot,
      temperaturePreviewId,
      temperaturePreviewSample,
      temperaturePreviewLoading,
      temperaturePreviewError,
      pendingSave: pendingSave ? { defaultName: pendingSave.defaultName } : null,
      savingSession,
      confirmSessionSave,
      skipSessionSave,
      setPlatform,
      setDeviceSelection,
      setBundleId,
      setAutoStopEnabled,
      setAutoStopAfterSec,
      setDeviceOverlayEnabled,
      setDeviceOverlayMetric,
      setTemperatureGuardEnabled,
      setTemperatureGuardThresholdC,
      retryTemperaturePreview,
      clearError,
      start,
      stop,
      exportCsv,
      exportXlsx,
    }),
    [
      phase,
      platform,
      serial,
      device,
      bundleId,
      captureId,
      snapshots,
      latest,
      elapsedSec,
      error,
      sampleRetrying,
      exportingCsv,
      exportingXlsx,
      starting,
      stopping,
      isRunning,
      isPaused,
      pausePending,
      cuts,
      activeCut,
      togglePause,
      toggleCut,
      selectionLocked,
      autoStopSettings.enabled,
      autoStopSettings.afterSec,
      overlaySettings.enabled,
      overlaySettings.metrics,
      overlayStatus,
      checkPlayCompanion,
      guardSettings.enabled,
      guardSettings.thresholdC,
      guardId,
      guardSample,
      guardSnapshot,
      temperaturePreviewId,
      temperaturePreviewSample,
      temperaturePreviewLoading,
      temperaturePreviewError,
      pendingSave,
      savingSession,
      confirmSessionSave,
      skipSessionSave,
      setPlatform,
      setDeviceSelection,
      setBundleId,
      setAutoStopEnabled,
      setAutoStopAfterSec,
      setDeviceOverlayEnabled,
      setDeviceOverlayMetric,
      setTemperatureGuardEnabled,
      setTemperatureGuardThresholdC,
      retryTemperaturePreview,
      clearError,
      start,
      stop,
      exportCsv,
      exportXlsx,
    ]
  )

  return <LiveCaptureContext.Provider value={value}>{children}</LiveCaptureContext.Provider>
}
