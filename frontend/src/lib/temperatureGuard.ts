import type { DeviceInfo, PackageInfo, Platform } from './types'
import { migrateLegacyStorageKey } from './storageMigration'

export const TEMPERATURE_GUARD_STORAGE_KEY = 'mpi.temperatureGuard.v2'
const PRE_MPI_TEMPERATURE_GUARD_STORAGE_KEY = 'perfinspect.temperatureGuard.v2'
const LEGACY_TEMPERATURE_GUARD_STORAGE_KEY = 'perfinspect.temperatureGuard.v1'
const MPI_LEGACY_TEMPERATURE_GUARD_STORAGE_KEY = 'mpi.temperatureGuard.v1'
const LEGACY_DEFAULT_TEMPERATURE_THRESHOLD_C = 40.0
export const DEFAULT_TEMPERATURE_THRESHOLD_C = 25.0
export const MIN_TEMPERATURE_THRESHOLD_C = 5.0
export const MAX_TEMPERATURE_THRESHOLD_C = 60.0
export const TEMPERATURE_THRESHOLD_STEP_C = 0.1
export const TEMPERATURE_GUARD_REQUIRED_HITS = 2

export type CapturePhase =
  | 'idle'
  | 'preparing'
  | 'waiting'
  | 'triggering'
  | 'running'
  | 'paused'
  | 'stopping'
  | 'error'

export interface TemperatureGuardSettings {
  enabled: boolean
  thresholdC: number
}

export const DEFAULT_TEMPERATURE_GUARD_SETTINGS: Readonly<TemperatureGuardSettings> = {
  enabled: false,
  thresholdC: DEFAULT_TEMPERATURE_THRESHOLD_C,
}

export interface TemperatureGuardSnapshot {
  platform: Platform
  serial: string
  device: DeviceInfo | null
  bundleId: string
  thresholdC: number
  durationSec: number
}

export interface TemperatureGuardSample {
  temperatureC: number
  sampledAt: string
  source: string
  thresholdC: number
  consecutiveHits: number
  requiredHits: number
}

export interface TemperaturePreviewSample {
  temperatureC: number
  sampledAt: string
  source: string
}

export interface CreateTemperaturePreviewResult {
  preview_id: string
  state: 'preparing'
  device: DeviceInfo
  package: PackageInfo
}

export interface CreateTemperatureGuardResult {
  guard_id: string
  device: DeviceInfo
  package: PackageInfo
  duration_sec?: number
}

export type TemperatureGuardEvent =
  | {
      event: 'temperature_guard_state'
      guard_id: string
      state: Exclude<CapturePhase, 'idle'>
    }
  | {
      event: 'temperature_guard_sample'
      guard_id: string
      temperature_c: number
      sampled_at: string
      source: string
      threshold_c: number
      consecutive_hits: number
      required_hits: number
    }
  | {
      event: 'temperature_guard_started'
      guard_id: string
      capture_id: string
      session_id?: string
      trigger_temperature_c: number
      device: DeviceInfo
      package: PackageInfo
      duration_sec: number
    }
  | {
      event: 'temperature_guard_error'
      guard_id: string
      code?: string
      error: string
    }
  | {
      event: 'temperature_guard_ended'
      guard_id: string
      reason?: string
    }

export type TemperaturePreviewEvent =
  | {
      event: 'temperature_preview_state'
      preview_id: string
      state: 'preparing' | 'running' | 'stopping' | 'error'
    }
  | {
      event: 'temperature_preview_sample'
      preview_id: string
      temperature_c: number
      sampled_at: string
      source: string
    }
  | {
      event: 'temperature_preview_error'
      preview_id: string
      code?: string
      error: string
    }
  | {
      event: 'temperature_preview_ended'
      preview_id: string
      reason?: string
    }

export function normalizeTemperatureThreshold(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_TEMPERATURE_THRESHOLD_C
  const clamped = Math.min(
    MAX_TEMPERATURE_THRESHOLD_C,
    Math.max(MIN_TEMPERATURE_THRESHOLD_C, value)
  )
  return Number(
    (Math.round(clamped / TEMPERATURE_THRESHOLD_STEP_C) * TEMPERATURE_THRESHOLD_STEP_C).toFixed(1)
  )
}

function parseStoredSettings(
  raw: string,
  fallbackThresholdC: number
): TemperatureGuardSettings {
  const parsed = JSON.parse(raw) as { enabled?: unknown; thresholdC?: unknown }
  return {
    enabled: parsed.enabled === true,
    thresholdC:
      typeof parsed.thresholdC === 'number'
        ? normalizeTemperatureThreshold(parsed.thresholdC)
        : fallbackThresholdC,
  }
}

function isStoredTemperatureGuardSettings(value: string): boolean {
  try {
    const parsed = JSON.parse(value) as {
      enabled?: unknown
      thresholdC?: unknown
    }
    return (
      !!parsed &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed) &&
      typeof parsed.enabled === 'boolean' &&
      typeof parsed.thresholdC === 'number' &&
      Number.isFinite(parsed.thresholdC) &&
      parsed.thresholdC >= MIN_TEMPERATURE_THRESHOLD_C &&
      parsed.thresholdC <= MAX_TEMPERATURE_THRESHOLD_C
    )
  } catch {
    return false
  }
}

/** Read preferences only. Runtime guard/capture state is intentionally never
 * persisted, so relaunching the app cannot silently resume waiting. */
export function loadTemperatureGuardSettings(): TemperatureGuardSettings {
  try {
    migrateLegacyStorageKey(
      TEMPERATURE_GUARD_STORAGE_KEY,
      PRE_MPI_TEMPERATURE_GUARD_STORAGE_KEY,
      isStoredTemperatureGuardSettings
    )
    migrateLegacyStorageKey(
      MPI_LEGACY_TEMPERATURE_GUARD_STORAGE_KEY,
      LEGACY_TEMPERATURE_GUARD_STORAGE_KEY,
      isStoredTemperatureGuardSettings
    )
    const raw = localStorage.getItem(TEMPERATURE_GUARD_STORAGE_KEY)
    if (raw) {
      return parseStoredSettings(raw, DEFAULT_TEMPERATURE_THRESHOLD_C)
    }
  } catch {
    return { ...DEFAULT_TEMPERATURE_GUARD_SETTINGS }
  }

  try {
    const legacyRaw = localStorage.getItem(
      MPI_LEGACY_TEMPERATURE_GUARD_STORAGE_KEY
    )
    if (!legacyRaw) return { ...DEFAULT_TEMPERATURE_GUARD_SETTINGS }
    const legacy = parseStoredSettings(
      legacyRaw,
      LEGACY_DEFAULT_TEMPERATURE_THRESHOLD_C
    )
    const migrated = {
      ...legacy,
      // Existing installations persisted the old default on first mount. Move
      // that value to the new default while preserving genuinely custom values.
      thresholdC:
        legacy.thresholdC === LEGACY_DEFAULT_TEMPERATURE_THRESHOLD_C
          ? DEFAULT_TEMPERATURE_THRESHOLD_C
          : legacy.thresholdC,
    }
    try {
      const serialized = JSON.stringify(migrated)
      localStorage.setItem(TEMPERATURE_GUARD_STORAGE_KEY, serialized)
      if (
        localStorage.getItem(TEMPERATURE_GUARD_STORAGE_KEY) === serialized &&
        localStorage.getItem(MPI_LEGACY_TEMPERATURE_GUARD_STORAGE_KEY) ===
          legacyRaw
      ) {
        localStorage.removeItem(MPI_LEGACY_TEMPERATURE_GUARD_STORAGE_KEY)
      }
    } catch {
      /* quota/private mode; the provider retries through its normal save effect */
    }
    return migrated
  } catch {
    return { ...DEFAULT_TEMPERATURE_GUARD_SETTINGS }
  }
}

export function saveTemperatureGuardSettings(settings: TemperatureGuardSettings): void {
  try {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({
        enabled: settings.enabled,
        thresholdC: normalizeTemperatureThreshold(settings.thresholdC),
      })
    )
  } catch {
    /* quota/private mode */
  }
}
