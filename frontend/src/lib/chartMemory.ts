import rules from '../../../mpi/play-memory-rules.json'
import type { MetricsSnapshot, SessionAnalysis } from './types'

export const MEMORY_SOURCE = 'android_proc_anon_rss_swap'
export type MemoryCategory = 'app' | 'game'
export const isAndroidAnonymousMemory = (s?: MetricsSnapshot) =>
  s?.device.platform === 'android' && s.memory.memory_source === MEMORY_SOURCE

export const memoryChartLabel = 'Anonymous RSS + Swap'

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
