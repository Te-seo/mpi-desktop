import type { CpuStats, MetricsSnapshot, Platform } from './types'

const MAX_REASONABLE_LOGICAL_CPUS = 1024

export interface CpuDisplayMetrics {
  /** Process CPU in the collector's native convention: one fully busy core = 100%. */
  appRawPercent: number | null
  /** App CPU converted to a whole-device 0-100 share by logical CPU count. */
  appWholeDevicePercent: number | null
  /** Whole-device system CPU on its native 0-100 scale. */
  systemPercent: number | null
  /** Collector-provided app normalization (frequency-adjusted on Android, core-only on iOS). */
  appNormalizedPercent: number | null
}

function finiteNumber(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function clampPercent(value: number | null | undefined): number | null {
  const finite = finiteNumber(value)
  return finite === null ? null : Math.max(0, Math.min(100, finite))
}

function validLogicalCpuCount(value: number | null | undefined): number | null {
  return typeof value === 'number' &&
    Number.isInteger(value) &&
    value > 0 &&
    value <= MAX_REASONABLE_LOGICAL_CPUS
    ? value
    : null
}

/**
 * Infer one stable logical-CPU width for a capture. Empty per-core arrays are
 * ignored because a platform/sample may not expose frequencies; contradictory
 * non-empty widths are rejected rather than changing the divisor mid-session.
 */
export function stableLogicalCpuCount(
  snapshots: readonly Pick<MetricsSnapshot, 'cpu'>[]
): number | null {
  const widths = new Set<number>()
  for (const snapshot of snapshots) {
    const declared = snapshot.cpu?.logical_core_count
    const width = declared ?? snapshot.cpu?.per_core_freq_mhz?.length ?? 0
    if (width === 0) continue
    const valid = validLogicalCpuCount(width)
    if (valid === null) return null
    widths.add(valid)
    if (widths.size > 1) return null
  }
  return widths.size === 1 ? [...widths][0] : null
}

/**
 * Convert collector CPU fields into explicitly-scoped display values.
 *
 * `cpu_percent` is intentionally retained as raw diagnostic data because its
 * 100% means one core. The UI comparison value divides it by the capture's
 * stable logical-CPU count. iOS does not expose per-core frequencies in the
 * current stream, but its backend-provided `cpu_norm` is already raw/cores, so
 * that value is the safe whole-device fallback. Android `cpu_norm` is frequency
 * adjusted and therefore must not be substituted for plain app utilization.
 */
export function deriveCpuDisplayMetrics(
  cpu: CpuStats,
  platform: Platform,
  logicalCpuCount: number | null
): CpuDisplayMetrics {
  const raw = finiteNumber(cpu.cpu_percent)
  const appRawPercent = raw === null ? null : Math.max(0, raw)
  const cores = validLogicalCpuCount(logicalCpuCount)
  const normalized = clampPercent(cpu.cpu_norm)
  const measuredCores = (cpu.per_core_percent ?? []).filter(
    (value): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100
  )
  const systemPercent = cpu.total_source === 'proc_stat_per_core'
    ? measuredCores.length ? measuredCores.reduce((sum, value) => sum + value, 0) / measuredCores.length : null
    : clampPercent(cpu.total_percent)

  const appWholeDevicePercent =
    appRawPercent !== null && cores !== null
      ? clampPercent(appRawPercent / cores)
      : platform === 'ios'
        ? normalized
        : null

  return {
    appRawPercent,
    appWholeDevicePercent,
    systemPercent,
    appNormalizedPercent: normalized,
  }
}
