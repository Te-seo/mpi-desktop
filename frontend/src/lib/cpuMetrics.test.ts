import { describe, expect, it } from 'vitest'
import type { CpuStats, MetricsSnapshot } from './types'
import {
  clampPercent,
  deriveCpuDisplayMetrics,
  stableLogicalCpuCount,
} from './cpuMetrics'

function cpu(over: Partial<CpuStats> = {}): CpuStats {
  return {
    pid: 1,
    cpu_percent: 0,
    mem_percent: 0,
    total_percent: null,
    per_core_freq_mhz: [],
    cpu_norm: null,
    ...over,
  }
}

function snapshotWithWidth(width: number): Pick<MetricsSnapshot, 'cpu'> {
  return { cpu: cpu({ per_core_freq_mhz: Array.from({ length: width }, () => 1000) }) }
}

describe('CPU display derivation', () => {
  it('uses the declared CPU count even without readable clocks and never fills missing readings with zero', () => {
    expect(stableLogicalCpuCount([{ cpu: cpu({ logical_core_count: 8 }) }])).toBe(8)
    const missing = deriveCpuDisplayMetrics(cpu({ cpu_percent: null, cpu_norm: null }), 'android', 8)
    expect(missing.appRawPercent).toBeNull()
    expect(missing.appWholeDevicePercent).toBeNull()
    expect(missing.systemPercent).toBeNull()
  })

  it('averages measured cores while retaining missing core positions', () => {
    const partial = cpu({ per_core_percent: [0, null, 80], total_percent: 90, total_source: 'proc_stat_per_core' })
    expect(deriveCpuDisplayMetrics(partial, 'android', 3).systemPercent).toBe(40)
    expect(deriveCpuDisplayMetrics(cpu({ per_core_percent: [null, null], total_source: 'proc_stat_per_core' }), 'android', 2).systemPercent).toBeNull()
  })
  it('converts core-summed app CPU to the same whole-device scale as system CPU', () => {
    const display = deriveCpuDisplayMetrics(
      cpu({ cpu_percent: 168.1, total_percent: 33.3, cpu_norm: 6.2 }),
      'android',
      8
    )

    expect(display.appRawPercent).toBe(168.1)
    expect(display.appWholeDevicePercent).toBeCloseTo(21.0125)
    expect(display.systemPercent).toBe(33.3)
    expect(display.appNormalizedPercent).toBe(6.2)
  })

  it('rejects missing or contradictory core widths instead of guessing a divisor', () => {
    expect(stableLogicalCpuCount([])).toBeNull()
    expect(stableLogicalCpuCount([snapshotWithWidth(0), snapshotWithWidth(8), snapshotWithWidth(0)])).toBe(8)
    expect(stableLogicalCpuCount([snapshotWithWidth(8), snapshotWithWidth(4)])).toBeNull()

    expect(
      deriveCpuDisplayMetrics(cpu({ cpu_percent: 168.1, cpu_norm: 6.2 }), 'android', null)
        .appWholeDevicePercent
    ).toBeNull()
  })

  it('clamps bounded display percentages while retaining raw diagnostic scale', () => {
    const display = deriveCpuDisplayMetrics(
      cpu({ cpu_percent: 900, total_percent: 140, cpu_norm: -2 }),
      'android',
      4
    )

    expect(display.appRawPercent).toBe(900)
    expect(display.appWholeDevicePercent).toBe(100)
    expect(display.systemPercent).toBe(100)
    expect(display.appNormalizedPercent).toBe(0)
    expect(clampPercent(Number.NaN)).toBeNull()
    expect(clampPercent(Number.POSITIVE_INFINITY)).toBeNull()
  })

  it('uses iOS core-normalized data as the safe fallback but not Android frequency normalization', () => {
    const missingCores = cpu({ cpu_percent: 120, cpu_norm: 15 })

    expect(deriveCpuDisplayMetrics(missingCores, 'ios', null).appWholeDevicePercent).toBe(15)
    expect(deriveCpuDisplayMetrics(missingCores, 'android', null).appWholeDevicePercent).toBeNull()
  })
})
