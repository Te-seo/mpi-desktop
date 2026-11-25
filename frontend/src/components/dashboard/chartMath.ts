// Pure chart math, extracted for unit testing (no DOM / recharts).

export interface Pt {
  t: number
  v: number | null
}

/** Max points drawn per line; beyond this each redraw only gets slower. */
export const MAX_POINTS = 600

/** Min/max of every finite value across all series, or null when there are none.
 *  Module scope keeps the loop accumulators out of React's render scope. */
export function extentOf(series: Pt[][]): { min: number; max: number } | null {
  let min = Infinity
  let max = -Infinity
  let seen = false
  for (const rows of series) {
    for (const p of rows) {
      if (p.v == null || !Number.isFinite(p.v)) continue
      seen = true
      if (p.v < min) min = p.v
      if (p.v > max) max = p.v
    }
  }
  return seen ? { min, max } : null
}

/** Nearest sampled value to a target time (no interpolation). */
export function nearestValue(
  times: (number | null)[],
  vals: (number | null)[],
  target: number
): number | null {
  let best = -1
  let bestDist = Infinity
  for (let i = 0; i < times.length; i++) {
    const tv = times[i]
    if (tv == null || !Number.isFinite(tv)) continue
    const dist = Math.abs(tv - target)
    if (dist < bestDist) {
      bestDist = dist
      best = i
    }
  }
  if (best < 0) return null
  const v = vals[best]
  return v != null && Number.isFinite(v) ? v : null
}

/** Last finite timestamp in a series (0 when none). */
export function lastFiniteT(times: (number | null)[]): number {
  for (let i = times.length - 1; i >= 0; i--) {
    const v = times[i]
    if (v != null && Number.isFinite(v)) return v
  }
  return 0
}

/** First finite timestamp in a series (null when none). */
export function firstFiniteT(times: (number | null)[]): number | null {
  for (let i = 0; i < times.length; i++) {
    const v = times[i]
    if (v != null && Number.isFinite(v)) return v
  }
  return null
}

/** Nearest sampled value ONLY within the series' own time span. Returns null when
 *  the target is before the first or after the last sample, so a shorter session
 *  shows "—" (not a stale extrapolated value) where it has no data. */
export function sampledValueAt(
  times: (number | null)[],
  vals: (number | null)[],
  target: number
): number | null {
  const first = firstFiniteT(times)
  if (first === null) return null
  if (target < first || target > lastFiniteT(times)) return null
  return nearestValue(times, vals, target)
}

/** Shared X max across sessions: the longest session's end by default; the
 *  shortest session's end when only the common range is shown. Sessions with no
 *  finite timestamps are ignored so the common-range toggle isn't a no-op. */
export function resolveXMax(tArrays: (number | null)[][], commonRange: boolean): number {
  const lasts = tArrays.map(lastFiniteT).filter((x) => x > 0)
  if (lasts.length === 0) return 0
  return commonRange ? Math.min(...lasts) : Math.max(...lasts)
}

/** Min/max-preserving downsample: keep, per bucket, the rows where the value hit
 *  its min and max so spikes survive. Nulls are preserved as gaps. */
export function downsample(rows: Pt[], maxPoints = MAX_POINTS): Pt[] {
  const n = rows.length
  if (n <= maxPoints) return rows
  const buckets = Math.max(1, Math.floor(maxPoints / 2))
  const step = n / buckets
  const out: Pt[] = []
  let lastIdx = -1
  const push = (i: number) => {
    if (i !== lastIdx) {
      out.push(rows[i])
      lastIdx = i
    }
  }
  push(0)
  for (let b = 0; b < buckets; b++) {
    const start = Math.floor(b * step)
    const end = Math.min(n, Math.floor((b + 1) * step))
    let minI = -1
    let maxI = -1
    let minV = Infinity
    let maxV = -Infinity
    for (let i = start; i < end; i++) {
      const v = rows[i].v
      if (v == null || !Number.isFinite(v)) continue
      if (v < minV) {
        minV = v
        minI = i
      }
      if (v > maxV) {
        maxV = v
        maxI = i
      }
    }
    if (minI === -1) continue
    const a = Math.min(minI, maxI)
    const c = Math.max(minI, maxI)
    push(a)
    if (c !== a) push(c)
  }
  push(n - 1)
  return out
}
