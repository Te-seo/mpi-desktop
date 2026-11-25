import { describe, expect, it } from 'vitest'
import { anonymousMemoryMib, currentMemoryReference, MEMORY_SOURCE, observedCategory } from '../lib/chartMemory'
import { formatMmss } from '../lib/format'
import type { MetricsSnapshot, Platform } from '../lib/types'
import { buildComparisonRows, mergeComparisonRows } from './LiveCharts'

type Row = Record<string, number | null>

function cpuSnapshot({
  raw,
  total,
  normalized,
  cores,
  platform = 'android',
  second = 0,
}: {
  raw: number
  total: number | null
  normalized: number | null
  cores: number
  platform?: Platform
  second?: number
}): MetricsSnapshot {
  return {
    captured_at: `2026-07-21T00:00:${String(second).padStart(2, '0')}Z`,
    device: { serial: 'test', platform, model: 'test', os_version: '1', connection_type: 'usb' },
    package: { bundle_id: 'app', label: null, version_name: null, version_code: null, apk_path: null, uid: null },
    duration_sec: 1,
    frames: { total_frames: 0, janky_frames: 0, small_jank: 0, big_jank: 0, jank_percent: 0, p50_ms: 0, p90_ms: 0, p95_ms: 0, p99_ms: 0, missed_vsync: 0, slow_ui_thread: 0, frame_deadline_missed: 0, fps_estimate: 60 },
    memory: { pss_total_kb: 0, rss_total_kb: 0, java_heap_kb: 0, native_heap_kb: 0, graphics_kb: 0, code_kb: 0, stack_kb: 0, sys_total_kb: 0, sys_used_kb: 0 },
    cpu: { pid: 1, cpu_percent: raw, mem_percent: 0, total_percent: total, cpu_norm: normalized, per_core_freq_mhz: Array.from({ length: cores }, () => 1000) },
    gpu: { busy_percent: null, clock_mhz: null, temp_c: null },
    thermal: { zones_c: {} },
    battery: { level_percent: null, current_ma: null, status: null, voltage_mv: null },
    raw: {},
  }
}

describe('mergeComparisonRows', () => {
  it('keeps half-second samples distinct in the chart, time labels and comparison', () => {
    const sample = cpuSnapshot({ raw: 0, total: 0, normalized: 0, cores: 4 })
    const snapshots = [0, 0.5, 1].map((elapsed_sec, index) => ({
      ...sample, elapsed_sec, frames: { ...sample.frames, fps_estimate: 50 + index },
    }))
    const comparison = buildComparisonRows(snapshots)
    expect(comparison.map(row => row.t)).toEqual([0, 0.5, 1])
    expect(comparison.map(row => formatMmss(row.t))).toEqual(['00:00', '00:00.5', '00:01'])
    expect(formatMmss(0.25)).toBe('00:00.25')
    const live: Row[] = [0.25, 0.5, 0.9, 1.5].map(t => ({ t }))
    const merged = mergeComparisonRows<Row>(live, comparison)
    expect(merged.map(row => row['comparison:fps'])).toEqual([50, 51, 52, undefined])
  })

  it('leaves live data unchanged when comparison is empty', () => {
    const live: Row[] = [{ t: 0, fps: 60 }]
    expect(mergeComparisonRows(live, [])).toBe(live)
  })

  it('uses exact and nearest elapsed-duration samples', () => {
    const live: Row[] = [{ t: 0, fps: 1 }, { t: 4, fps: 2 }, { t: 10, fps: 3 }]
    const comparison: Row[] = [{ t: 0, fps: 50 }, { t: 5, fps: 55 }, { t: 10, fps: 60 }]
    const merged = mergeComparisonRows(live, comparison)
    expect(merged.map((row) => row['comparison:fps'])).toEqual([50, 55, 60])
  })

  it('does not hold the final comparison value past the saved duration', () => {
    const live: Row[] = [{ t: 0, fps: 1 }, { t: 5, fps: 2 }, { t: 6, fps: 3 }]
    const comparison: Row[] = [{ t: 0, fps: 50 }, { t: 5, fps: 55 }]
    const merged = mergeComparisonRows(live, comparison)
    expect(merged[1]['comparison:fps']).toBe(55)
    expect(merged[2]['comparison:fps']).toBeUndefined()
  })
})

describe('buildComparisonRows CPU semantics', () => {
  it('uses whole-device app CPU instead of the raw core sum', () => {
    const [row] = buildComparisonRows([
      cpuSnapshot({ raw: 168.1, total: 33.3, normalized: 6.2, cores: 8 }),
    ])

    expect(row.cpu_app).toBeCloseTo(21.0125)
    expect(row.cpu_total).toBe(33.3)
    expect(row.cpu_norm).toBe(6.2)
    expect(row).not.toHaveProperty('cpu_pct')
  })

  it('keeps Android app share unavailable without stable cores and uses the iOS normalized fallback', () => {
    const [android] = buildComparisonRows([
      cpuSnapshot({ raw: 168.1, total: 33.3, normalized: 6.2, cores: 0 }),
    ])
    const [ios] = buildComparisonRows([
      cpuSnapshot({ raw: 120, total: 55, normalized: 15, cores: 0, platform: 'ios' }),
    ])

    expect(android.cpu_app).toBeNull()
    expect(ios.cpu_app).toBe(15)
  })

  it('merges the derived comparison value without reintroducing raw CPU', () => {
    const comparison = buildComparisonRows([
      cpuSnapshot({ raw: 168.1, total: 33.3, normalized: 6.2, cores: 8 }),
    ]) as unknown as Row[]
    const merged = mergeComparisonRows([{ t: 0, cpu_app: 10 }], comparison)

    expect(merged[0]['comparison:cpu_app']).toBeCloseTo(21.0125)
  })
})

describe('buildComparisonRows measured hardware values', () => {
  it('keeps iOS process sizes in MiB and missing footprints as gaps during replay', () => {
    const sample = cpuSnapshot({ platform: 'ios', raw: 0, total: 0, normalized: 0, cores: 1 })
    const rows = buildComparisonRows([2048, null, 0].map(rss_total_kb => ({
      ...sample, memory: { ...sample.memory, rss_total_kb },
    })))
    expect(rows.map(row => row.mem_ios_mib)).toEqual([2, null, null])
    expect(rows.every(row => row.mem_ratio === null && row.mem_anon_mib === null)).toBe(true)
  })

  it('resolves a legacy battery name and preserves measured idle CPU/GPU zero', () => {
    const snapshot = cpuSnapshot({ raw: 0, total: 0, normalized: 0, cores: 8 })
    snapshot.cpu.total_source = 'dumpsys_cpuinfo'
    snapshot.thermal.zones_c = { 'thermal:battery': 32.5 }
    snapshot.gpu = { busy_percent: 0, clock_mhz: 200, temp_c: null }
    const [row] = buildComparisonRows([snapshot])
    expect(row['thermal:battery']).toBe(32.5)
    expect(row).not.toHaveProperty('thermal:thermal:battery')
    expect(row.cpu_total).toBe(0)
    expect(row.gpu_busy).toBe(0)
    expect(row.gpu_clock).toBe(200)
  })
})

describe('Android Anonymous RSS + Swap plot and local threshold', () => {
  const sample = cpuSnapshot({ raw: 0, total: null, normalized: null, cores: 1 })
  const metrics = { ...sample.memory, memory_source: MEMORY_SOURCE, anon_rss_plus_swap_kib: 2048,
    play_memory_state: 'foreground', play_memory_state_source: 'measured_oom_score_adj', play_memory_ram_kib: 7 * 1024 * 1024,
    play_memory_sdk_int: 33, play_memory_form_factor: 'phone_tablet' }

  it('separates units and leaves a gap instead of substituting legacy PSS/RSS', () => {
    const missing = { ...metrics, pss_total_kb: 400, rss_total_kb: 900, anon_rss_plus_swap_kib: null }
    expect(buildComparisonRows([{ ...sample, memory: metrics }, { ...sample, memory: missing }, sample])
      .map(row => [row.mem_anon_mib, row.mem_ratio])).toEqual([[2, null], [null, null], [null, null]])
    expect(anonymousMemoryMib({ ...sample, memory: { ...metrics, anon_rss_plus_swap_kib: 0 } })).toBe(0)
  })
  it('shows only the latest supported local threshold and does not infer category', () => {
    expect(observedCategory({ ...sample, memory: metrics })).toBeNull()
    expect(currentMemoryReference({ ...sample, memory: metrics }, null)).toBeNull()
    expect(currentMemoryReference({ ...sample, memory: metrics }, 'game')).toMatchObject({ mib: 3584, tier: '8 GB' })
    expect(currentMemoryReference({ ...sample, memory: { ...metrics, play_memory_state: 'background' } }, 'app'))
      .toMatchObject({ mib: 1536 })
    expect(currentMemoryReference({ ...sample, memory: { ...metrics, play_memory_state: null } }, 'app')).toBeNull()
    expect(currentMemoryReference({ ...sample, memory: { ...metrics, play_memory_form_factor: 'other' } }, 'app')).toBeNull()
    expect(currentMemoryReference({ ...sample, memory: { ...metrics, play_memory_sdk_int: 32 } }, 'app')).toBeNull()
  })
})
