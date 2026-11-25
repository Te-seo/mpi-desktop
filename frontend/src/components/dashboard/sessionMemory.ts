import { MEMORY_SOURCE, memoryChartLabel } from '../../lib/chartMemory'
import type { SessionAnalysis } from '../../lib/types'

export const sessionMemoryLabels = {
  anonymous: memoryChartLabel,
  ios: 'iOS app memory',
  legacy: 'Legacy PSS / RSS',
}

export function sessionMemoryType(analysis: SessionAnalysis): keyof typeof sessionMemoryLabels {
  if (analysis.platform === 'ios') return 'ios'
  if (analysis.platform === 'android' && analysis.memory_source === MEMORY_SOURCE) return 'anonymous'
  return 'legacy'
}

export function sessionMemoryUnit(type: keyof typeof sessionMemoryLabels) {
  return type === 'legacy' ? 'MB' : 'MiB'
}

export function sessionPeakMemory(analysis: SessionAnalysis): number | null {
  return sessionMemoryType(analysis) === 'anonymous'
    ? analysis.metrics.anon_rss_plus_swap_mib?.max ?? null
    : analysis.peak_app_memory_mb
}
