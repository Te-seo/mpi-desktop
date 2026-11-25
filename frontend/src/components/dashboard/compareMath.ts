// Pure comparison math, extracted so it can be unit-tested without a DOM.

import type { SessionAnalysis } from '../../lib/types'

export interface DeltaParts {
  /** cmp - base, or null when either value is unavailable. */
  abs: number | null
  /** (cmp - base) / |base| * 100, or null when the baseline is 0 or either
   *  value is unavailable. */
  pct: number | null
}

export function deltaParts(base: number | null, cmp: number | null): DeltaParts {
  if (base == null || cmp == null || !Number.isFinite(base) || !Number.isFinite(cmp)) {
    return { abs: null, pct: null }
  }
  const abs = cmp - base
  const pct = base === 0 ? null : (abs / Math.abs(base)) * 100
  return { abs, pct }
}

export type MismatchDim = 'platform' | 'device' | 'app' | 'version'

/** Which comparison dimensions differ across the selected sessions (empty when
 *  fewer than two sessions or all match). Comparison is never blocked; this only
 *  surfaces a neutral warning. */
export function mismatchDims(analyses: SessionAnalysis[]): MismatchDim[] {
  if (analyses.length < 2) return []
  const out: MismatchDim[] = []
  const same = (get: (a: SessionAnalysis) => string) =>
    analyses.every((a) => get(a) === get(analyses[0]))
  if (!same((a) => a.platform)) out.push('platform')
  if (!same((a) => a.device_model)) out.push('device')
  if (!same((a) => a.bundle_id)) out.push('app')
  if (!same((a) => a.app_version ?? '')) out.push('version')
  return out
}
