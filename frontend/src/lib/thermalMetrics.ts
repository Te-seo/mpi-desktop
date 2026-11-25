import type { MetricsSnapshot, Platform } from './types'

export const ANDROID_SKIN_SOURCE = 'android_thermalservice_current_type_skin_max_celsius'
export type TemperatureZone = 'skin' | 'battery'

/** The status marker also identifies new captures when no Skin sample exists. */
export function hasSkinTemperatureContract(snapshot: MetricsSnapshot): boolean {
  return snapshot.device.platform === 'android' && (
    snapshot.thermal?.sources?.skin === ANDROID_SKIN_SOURCE ||
    snapshot.thermal?.status?.skin === 'measured' ||
    snapshot.thermal?.status?.skin === 'unavailable'
  )
}

/** Old Android recordings and iOS retain their battery headline. */
export function temperatureZoneForSnapshots(
  snapshots: MetricsSnapshot[],
  platform?: Platform,
): TemperatureZone {
  if (!snapshots.length) return platform === 'android' ? 'skin' : 'battery'
  return snapshots.some(hasSkinTemperatureContract) ? 'skin' : 'battery'
}

/** Never label a legacy sysfs value or a stale/unavailable sample as HAL Skin. */
export function measuredSkinTemperature(snapshot: MetricsSnapshot): number | null {
  if (snapshot.device.platform !== 'android' ||
    snapshot.thermal?.sources?.skin !== ANDROID_SKIN_SOURCE ||
    snapshot.thermal?.status?.skin === 'unavailable') return null
  const value = normalizeThermalZones(snapshot.thermal.zones_c).skin
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Older captures sometimes stored the chart prefix inside the sensor name. */
export function normalizeThermalZones(zones: Record<string, number> = {}): Record<string, number> {
  const normalized: Record<string, number> = {}
  for (const [key, value] of Object.entries(zones)) {
    if (!Number.isFinite(value)) continue
    const zone = key.replace(/^(thermal:)+/, '')
    if (zone && (normalized[zone] === undefined || key === zone)) normalized[zone] = value
  }
  return normalized
}

/** Resolve canonical and legacy-prefixed analysis series without substituting another sensor. */
export function thermalSeriesForZone(
  timeseries: Record<string, (number | null)[]>,
  zone: string | null | undefined
): (number | null)[] {
  if (!zone) return []
  const name = zone.replace(/^(thermal:)+/, '')
  return timeseries['thermal:' + name] ?? timeseries['thermal:thermal:' + name] ?? []
}
