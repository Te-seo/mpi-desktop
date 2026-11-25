import { migrateLegacyStorageKey } from './storageMigration'

export const AUTO_STOP_STORAGE_KEY = 'mpi.autoStop.v1'
const LEGACY_AUTO_STOP_STORAGE_KEY = 'perfinspect.autoStop.v1'
export const DEFAULT_AUTO_STOP_AFTER_SEC = 40 * 60
export const MIN_AUTO_STOP_AFTER_SEC = 1
// LiveCaptureProvider intentionally keeps at most one hour of samples in
// memory. Keep the configurable limit within that lossless capture window.
export const MAX_AUTO_STOP_AFTER_SEC = 60 * 60

export interface AutoStopSettings {
  enabled: boolean
  afterSec: number
}

export interface AutoStopTimeParts {
  hours: string
  minutes: string
  seconds: string
}

export const DEFAULT_AUTO_STOP_SETTINGS: AutoStopSettings = {
  enabled: false,
  afterSec: DEFAULT_AUTO_STOP_AFTER_SEC,
}

export function normalizeAutoStopAfterSec(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return DEFAULT_AUTO_STOP_AFTER_SEC
  }
  return Math.min(
    MAX_AUTO_STOP_AFTER_SEC,
    Math.max(MIN_AUTO_STOP_AFTER_SEC, Math.round(value))
  )
}

export function loadAutoStopSettings(): AutoStopSettings {
  try {
    migrateLegacyStorageKey(
      AUTO_STOP_STORAGE_KEY,
      LEGACY_AUTO_STOP_STORAGE_KEY,
      (value) => {
        try {
          const parsed = JSON.parse(value) as {
            enabled?: unknown
            afterSec?: unknown
          }
          return (
            typeof parsed.enabled === 'boolean' &&
            typeof parsed.afterSec === 'number' &&
            Number.isFinite(parsed.afterSec) &&
            parsed.afterSec >= MIN_AUTO_STOP_AFTER_SEC &&
            parsed.afterSec <= MAX_AUTO_STOP_AFTER_SEC
          )
        } catch {
          return false
        }
      }
    )
    const raw = localStorage.getItem(AUTO_STOP_STORAGE_KEY)
    if (!raw) return DEFAULT_AUTO_STOP_SETTINGS
    const parsed = JSON.parse(raw) as { enabled?: unknown; afterSec?: unknown }
    const persistedAfterSec =
      typeof parsed.afterSec === 'number' &&
      Number.isFinite(parsed.afterSec) &&
      parsed.afterSec >= MIN_AUTO_STOP_AFTER_SEC &&
      parsed.afterSec <= MAX_AUTO_STOP_AFTER_SEC
        ? Math.round(parsed.afterSec)
        : DEFAULT_AUTO_STOP_AFTER_SEC
    return {
      enabled: parsed.enabled === true,
      afterSec: persistedAfterSec,
    }
  } catch {
    return DEFAULT_AUTO_STOP_SETTINGS
  }
}

export function saveAutoStopSettings(settings: AutoStopSettings): void {
  try {
    localStorage.setItem(
      AUTO_STOP_STORAGE_KEY,
      JSON.stringify({
        enabled: settings.enabled,
        afterSec: normalizeAutoStopAfterSec(settings.afterSec),
      })
    )
  } catch {
    // Storage can be unavailable in hardened/private renderer contexts. The
    // in-memory setting still applies to the current window.
  }
}

export function formatAutoStopTime(afterSec: number): string {
  const total = normalizeAutoStopAfterSec(afterSec)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
}

export function formatAutoStopTimeParts(afterSec: number): AutoStopTimeParts {
  const [hours, minutes, seconds] = formatAutoStopTime(afterSec).split(':')
  return { hours, minutes, seconds }
}

export function normalizeAutoStopTimePart(value: string): string {
  const trimmed = value.trim()
  if (!/^\d{1,2}$/.test(trimmed)) return trimmed
  return trimmed.padStart(2, '0')
}

export function parseAutoStopTimeParts(parts: AutoStopTimeParts): number | null {
  const values = [parts.hours, parts.minutes, parts.seconds]
  if (values.some((value) => !/^\d{1,2}$/.test(value.trim()))) return null
  return parseAutoStopTime(
    values.map((value) => value.trim().padStart(2, '0')).join(':')
  )
}

export function parseAutoStopTime(value: string): number | null {
  const match = /^(\d{2}):([0-5]\d):([0-5]\d)$/.exec(value.trim())
  if (!match) return null
  const hours = Number(match[1])
  const minutes = Number(match[2])
  const seconds = Number(match[3])
  const total = hours * 3600 + minutes * 60 + seconds
  if (total < MIN_AUTO_STOP_AFTER_SEC || total > MAX_AUTO_STOP_AFTER_SEC) return null
  return total
}
