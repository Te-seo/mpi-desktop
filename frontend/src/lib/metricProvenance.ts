import type { MetricsSnapshot } from './types'
import type { LiveMetricKey } from './liveMetrics'

const keys: Record<LiveMetricKey, string> = {
  fps: 'fps', cpu: 'cpu_total', cpu_norm: 'cpu_norm', gpu: 'gpu', mem: 'mem_bytes',
  temp: 'temperature', power: 'battery_current', batt: 'battery_level',
}

export function metricStatusLabel(snapshot: MetricsSnapshot, metric: LiveMetricKey): string | undefined {
  let status = snapshot.metric_status?.[keys[metric]]
  if (metric === 'power') {
    const voltage = snapshot.metric_status?.battery_voltage
    if (status && voltage) {
      status = { ...status,
        status: status.status === 'unavailable' || voltage.status === 'unavailable' ? 'unavailable'
          : status.status === 'held' || voltage.status === 'held' ? 'held' : 'fresh',
        age_sec: status.age_sec !== null && voltage.age_sec !== null ? Math.max(status.age_sec, voltage.age_sec) : null,
      }
    }
  }
  const parts: string[] = []
  if (status) {
    parts.push(status.status)
    if (status.age_sec !== null) parts.push(`${status.age_sec.toFixed(1)} s`)
  }
  if (metric === 'cpu' && snapshot.cpu.total_source) {
    parts.push(snapshot.cpu.total_source, snapshot.cpu.total_status ?? '')
    if (snapshot.cpu.per_core_percent?.length) {
      parts.push(`${snapshot.cpu.per_core_percent.filter((v) => v !== null).length}/${snapshot.cpu.per_core_percent.length} cores`)
    }
  }
  return parts.filter(Boolean).join(' · ') || undefined
}
