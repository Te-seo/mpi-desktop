import rules from '../../../mpi/play-memory-rules.json'
import type { MetricsSnapshot, SessionAnalysis } from './types'

export const MEMORY_SOURCE = 'android_proc_anon_rss_swap'
export type MemoryCategory = 'app' | 'game'
export const isAndroidAnonymousMemory = (s?: MetricsSnapshot) =>
  s?.device.platform === 'android' && s.memory.memory_source === MEMORY_SOURCE

export const memoryChartLabel = 'Anonymous RSS + Swap'

const anonymousSeries = { key: 'mem_anon_mib', label: memoryChartLabel, unit: 'MiB' } as const
const iosSeries = { key: 'mem_ios_mib', label: 'iOS app memory', unit: 'MiB' } as const
const legacySeries = { key: 'mem_ratio', label: 'Legacy PSS / RSS', unit: '%' } as const

export function liveMemorySeries(s?: MetricsSnapshot) {
  if (s?.device.platform === 'ios') return iosSeries
  return isAndroidAnonymousMemory(s) ? anonymousSeries : legacySeries
}

export function liveMemoryValues(s: MetricsSnapshot) {
  const { pss_total_kb, rss_total_kb, sys_total_kb, sys_used_kb } = s.memory
  const appKiB = [pss_total_kb, rss_total_kb].find(
    (value): value is number => typeof value === 'number' && Number.isFinite(value) && value > 0,
  )
  const legacyRatio = sys_total_kb > 0 && appKiB !== undefined
    ? Math.round((appKiB / sys_total_kb) * 1000) / 10
    : s.cpu.mem_percent || null
  const series = liveMemorySeries(s)
  return {
    mem_anon_mib: anonymousMemoryMib(s),
    mem_ios_mib: series === iosSeries && appKiB !== undefined ? appKiB / 1024 : null,
    mem_total_mb: sys_used_kb ? Math.round(sys_used_kb / 1024) : null,
    mem_ratio: series === legacySeries ? legacyRatio : null,
  }
}

export function anonymousMemoryMib(s: MetricsSnapshot): number | null {
  if (!isAndroidAnonymousMemory(s)) return null
  const value = s.memory.anon_rss_plus_swap_kib
  return Number.isSafeInteger(value) && value! >= 0 ? value! / 1024 : null
}

export function observedCategory(s?: MetricsSnapshot): MemoryCategory | null {
  const value = s?.memory.play_memory_category
  return value === 'app' || value === 'game' ? value : null
}

export function currentMemoryReference(s: MetricsSnapshot | undefined, category: MemoryCategory | null) {
  if (!s || !isAndroidAnonymousMemory(s) || !category) return null
  return playReference(s.memory, category)
}

export function playReference(m: NonNullable<SessionAnalysis['memory_reference_context']>, category: MemoryCategory) {
  const ram = m.play_memory_ram_kib
  const sdk = m.play_memory_sdk_int
  const state = m.play_memory_state
  if (m.play_memory_state_source !== 'measured_oom_score_adj' || !Number.isSafeInteger(sdk) || sdk! < rules.android_min_sdk ||
      m.play_memory_form_factor !== 'phone_tablet' || !Number.isSafeInteger(ram) || ram! <= 0 ||
      ram! / 1024 > rules.max_mem_total_inclusive_mib || !state) return null
  const col = rules.state_order.indexOf(state)
  const tier = rules.tier_lower_inclusive_mib.reduce((found, low, i) => ram! >= low * 1024 ? i : found, -1)
  if (col < 0 || tier < 1) return null
  const mib = rules.anonymous_rss_plus_swap_limits_mib[category][tier][col]
  return mib == null ? null : { mib, tier: rules.tier_labels[tier].replace('_gb', ' GB'), state }
}
