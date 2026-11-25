import { describe, expect, it } from 'vitest'
import { metricStatusLabel } from './metricProvenance'
import type { MetricsSnapshot } from './types'

describe('metric provenance labels', () => {
  it('requires both voltage and current for power availability', () => {
    const snapshot = { metric_status: {
      battery_current: { source: 'battery', scope: 'system_device', status: 'fresh', age_sec: 0 },
      battery_voltage: { source: 'battery', scope: 'system_device', status: 'unavailable', age_sec: 16 },
    } } as unknown as MetricsSnapshot
    expect(metricStatusLabel(snapshot, 'power')).toBe('unavailable · 16.0 s')
  })

  it('keeps FPS collection details internal while retaining availability', () => {
    const snapshot = { frames: { source: 'ios_kdebug_display_experimental', scope: 'system_display', experimental: true },
      metric_status: { fps: { source: 'ios_kdebug_display_experimental', scope: 'system_display', status: 'held', age_sec: 2 } },
    } as unknown as MetricsSnapshot
    expect(metricStatusLabel(snapshot, 'fps')).toBe('held · 2.0 s')
  })

  it('displays partial core coverage while keeping a measured idle zero', () => {
    const snapshot = { cpu: { total_source: 'proc_stat_per_core', total_status: 'partial', per_core_percent: [0, null, 50] } } as MetricsSnapshot
    expect(metricStatusLabel(snapshot, 'cpu')).toContain('partial · 2/3 cores')
  })
})
