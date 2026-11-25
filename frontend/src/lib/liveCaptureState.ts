import { createContext, useContext } from 'react'
import type { DeviceInfo, MetricsSnapshot, Platform, SessionCut } from './types'
import type {
  DeviceOverlayMetricKey,
  DeviceOverlayMetrics,
  DeviceOverlayStatus,
} from './deviceOverlay'
import type {
  CapturePhase,
  TemperatureGuardSample,
  TemperatureGuardSnapshot,
  TemperaturePreviewSample,
} from './temperatureGuard'

// Per-sample capture window, in seconds, per platform. iOS streams one frame per
// second from a persistent DVT connection (mpi/ios/pmd3_stream.py), so
// it samples at 1 s; Android samples at 0.5 s. Shared here so the capture loop
// and any UI that displays the interval stay in sync.
export const ANDROID_SAMPLE_INTERVAL_SEC = 0.5
export const IOS_SAMPLE_INTERVAL_SEC = 1.0

export function sampleIntervalSec(platform: Platform): number {
  return platform === 'ios' ? IOS_SAMPLE_INTERVAL_SEC : ANDROID_SAMPLE_INTERVAL_SEC
}

export interface LiveCaptureState {
  phase: CapturePhase
  platform: Platform
  serial: string | null
  device: DeviceInfo | null
  bundleId: string | null
  captureId: string | null
  snapshots: MetricsSnapshot[]
  latest: MetricsSnapshot | null
  elapsedSec: number
  error: string | null
  exportingCsv: boolean
  exportingXlsx: boolean
  starting: boolean
  stopping: boolean
  isRunning: boolean
  isPaused: boolean
  pausePending: boolean
  cuts: SessionCut[]
  activeCut: Omit<SessionCut, 'end_sec'> | null
  togglePause(): Promise<void>
  toggleCut(): void
  selectionLocked: boolean
  autoStopEnabled: boolean
  autoStopAfterSec: number
  deviceOverlayEnabled: boolean
  deviceOverlayMetrics: DeviceOverlayMetrics
  /** Whether the selected device can actually show the HUD. `null` while the
   *  probe has not answered (or for a non-Android device) — every failure mode
   *  is silent on the device, so the UI needs this to explain itself. */
  deviceOverlayStatus: DeviceOverlayStatus | null
  checkPlayCompanion: (connect?: boolean) => Promise<void>
  temperatureGuardEnabled: boolean
  temperatureGuardThresholdC: number
  guardId: string | null
  guardSample: TemperatureGuardSample | null
  guardSnapshot: TemperatureGuardSnapshot | null
  temperaturePreviewId: string | null
  temperaturePreviewSample: TemperaturePreviewSample | null
  temperaturePreviewLoading: boolean
  temperaturePreviewError: string | null
  /** When a capture ends with endpoint transfer enabled and configured, the
   *  transfer is deferred behind a naming step. */
  pendingSave: { defaultName: string } | null
  /** True while the deferred endpoint transfer is in flight. */
  savingSession: boolean
  /** Transfer the pending session under `name` (blank → the default). */
  confirmSessionSave(name: string): Promise<void>
  /** Transfer the pending session under its default name without editing. */
  skipSessionSave(): void
  setPlatform(platform: Platform): void
  setDeviceSelection(serial: string | null, device: DeviceInfo | null): void
  setBundleId(bundleId: string | null): void
  setAutoStopEnabled(enabled: boolean): void
  setAutoStopAfterSec(afterSec: number): void
  /** Unlike the other capture settings this one is live: while a capture runs it
   *  also pushes the new HUD config to that capture. */
  setDeviceOverlayEnabled(enabled: boolean): void
  setDeviceOverlayMetric(key: DeviceOverlayMetricKey, enabled: boolean): void
  setTemperatureGuardEnabled(enabled: boolean): void
  setTemperatureGuardThresholdC(thresholdC: number): void
  retryTemperaturePreview(): void
  clearError(): void
  start(): Promise<void>
  stop(): Promise<void>
  exportCsv(): Promise<void>
  exportXlsx(): Promise<void>
}

export const LiveCaptureContext = createContext<LiveCaptureState | null>(null)

export function useLiveCapture(): LiveCaptureState {
  const ctx = useContext(LiveCaptureContext)
  if (!ctx) {
    throw new Error('useLiveCapture must be used inside LiveCaptureProvider')
  }
  return ctx
}
