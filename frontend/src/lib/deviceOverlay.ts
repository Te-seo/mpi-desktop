// On-device metrics HUD, drawn by the Android companion app while a capture
// runs. Android-only: nothing is installed on an iOS device and iOS has no
// cross-app draw-over API, so the sidecar ignores this config for iOS targets.
import { migrateLegacyStorageKey } from './storageMigration'
/** Why the on-device HUD is unavailable, as reported by
 *  `companion.overlay_support`. English sentinels, localized in the UI —
 *  the same contract the sidecar uses for error codes. */
export type DeviceOverlayReason =
  | 'companion_not_installed'
  | 'companion_too_old'
  | 'usb_unauthorized'
  | 'device_unreachable'
  | 'companion_sign_in_required'
  | 'companion_unreachable'
  | 'overlay_unsupported_os'
  | 'overlay_permission_denied'

/** Answer to the `companion_overlay_status` probe. `null` while unknown — the
 *  probe is best-effort, so "not yet answered" and "unavailable" are different
 *  states and only the latter warrants a warning. */
export interface DeviceOverlayStatus {
  available: boolean
  reason: DeviceOverlayReason | null
}

export interface CompanionOverlayStatusResult {
  installed?: boolean
  supported?: boolean
  can_draw_overlays?: boolean
  reason?: string | null
}

const OVERLAY_REASONS: readonly DeviceOverlayReason[] = [
  'companion_not_installed',
  'companion_too_old',
  'usb_unauthorized',
  'device_unreachable',
  'companion_sign_in_required',
  'companion_unreachable',
  'overlay_unsupported_os',
  'overlay_permission_denied',
]

export function toDeviceOverlayStatus(
  result: CompanionOverlayStatusResult
): DeviceOverlayStatus {
  const available =
    result.installed === true &&
    result.supported === true &&
    result.can_draw_overlays === true
  const raw = result.reason
  const reason = OVERLAY_REASONS.find((candidate) => candidate === raw) ?? null
  // An unavailable device with an unrecognized (or missing) reason still has to
  // say something, so fall back to the vaguest accurate one rather than claiming
  // the HUD works.
  return { available, reason: available ? null : reason ?? 'companion_unreachable' }
}

export const DEVICE_OVERLAY_STORAGE_KEY = 'mpi.deviceOverlay.v1'
const LEGACY_DEVICE_OVERLAY_STORAGE_KEY = 'perfinspect.deviceOverlay.v1'

export type DeviceOverlayMetricKey =
  | 'elapsed'
  | 'fps'
  | 'cpu'
  | 'gpu'
  | 'skin'
  | 'net'

/** HUD row order. The companion renders the metrics in this order, so the
 *  sidebar checkboxes must iterate the same list. */
export const DEVICE_OVERLAY_METRIC_KEYS: readonly DeviceOverlayMetricKey[] = [
  'elapsed',
  'fps',
  'cpu',
  'gpu',
  'skin',
  'net',
]

export type DeviceOverlayMetrics = Record<DeviceOverlayMetricKey, boolean>

export interface DeviceOverlaySettings {
  enabled: boolean
  metrics: DeviceOverlayMetrics
}

export const DEFAULT_DEVICE_OVERLAY_SETTINGS: DeviceOverlaySettings = {
  enabled: false,
  metrics: {
    elapsed: true,
    fps: true,
    cpu: true,
    gpu: true,
    skin: true,
    net: true,
  },
}

function normalizeDeviceOverlayMetrics(value: unknown): DeviceOverlayMetrics {
  const source = (value ?? {}) as Partial<Record<DeviceOverlayMetricKey | 'temp', unknown>>
  const metrics = {} as DeviceOverlayMetrics
  for (const key of DEVICE_OVERLAY_METRIC_KEYS) {
    // A metric added after this preference was written has no stored entry.
    // Fall back to its default (on) instead of silently hiding a new HUD row.
    // Retain an existing temperature-row visibility choice, not its old sensor
    // semantics. New captures send the distinct Skin wire key, never temp.
    const stored = key === 'skin' && source.skin === undefined ? source.temp : source[key]
    metrics[key] =
      typeof stored === 'boolean'
        ? stored
        : DEFAULT_DEVICE_OVERLAY_SETTINGS.metrics[key]
  }
  return metrics
}

function isStoredDeviceOverlaySettings(value: string): boolean {
  try {
    const parsed = JSON.parse(value) as {
      enabled?: unknown
      metrics?: unknown
    }
    if (
      !parsed ||
      typeof parsed !== 'object' ||
      Array.isArray(parsed) ||
      typeof parsed.enabled !== 'boolean' ||
      !parsed.metrics ||
      typeof parsed.metrics !== 'object' ||
      Array.isArray(parsed.metrics)
    ) {
      return false
    }
    const metrics = parsed.metrics as Record<string, unknown>
    return [...DEVICE_OVERLAY_METRIC_KEYS, 'temp'].every(
      (key) => metrics[key] === undefined || typeof metrics[key] === 'boolean'
    )
  } catch {
    return false
  }
}

export function loadDeviceOverlaySettings(): DeviceOverlaySettings {
  try {
    migrateLegacyStorageKey(
      DEVICE_OVERLAY_STORAGE_KEY,
      LEGACY_DEVICE_OVERLAY_STORAGE_KEY,
      isStoredDeviceOverlaySettings
    )
    const raw = localStorage.getItem(DEVICE_OVERLAY_STORAGE_KEY)
    if (!raw) return { ...DEFAULT_DEVICE_OVERLAY_SETTINGS }
    const parsed = JSON.parse(raw) as { enabled?: unknown; metrics?: unknown }
    return {
      enabled: parsed.enabled === true,
      metrics: normalizeDeviceOverlayMetrics(parsed.metrics),
    }
  } catch {
    return { ...DEFAULT_DEVICE_OVERLAY_SETTINGS }
  }
}

export function saveDeviceOverlaySettings(settings: DeviceOverlaySettings): void {
  try {
    localStorage.setItem(
      DEVICE_OVERLAY_STORAGE_KEY,
      JSON.stringify({
        enabled: settings.enabled,
        metrics: normalizeDeviceOverlayMetrics(settings.metrics),
      })
    )
  } catch {
    // Storage can be unavailable in hardened/private renderer contexts. The
    // in-memory setting still applies to the current window.
  }
}

/** The metric list the sidecar/companion wire contract expects, e.g. ["fps","cpu"]. */
export function deviceOverlayMetricList(
  settings: DeviceOverlaySettings
): DeviceOverlayMetricKey[] {
  return DEVICE_OVERLAY_METRIC_KEYS.filter((key) => settings.metrics[key])
}
