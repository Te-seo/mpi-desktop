import { describe, expect, it } from 'vitest'
import {
  downsample,
  extentOf,
  lastFiniteT,
  nearestValue,
  resolveXMax,
  sampledValueAt,
  type Pt,
} from './chartMath'

describe('extentOf', () => {
  it('spans every finite value across series, skipping nulls', () => {
    const a: Pt[] = [{ t: 0, v: 10 }, { t: 1, v: null }, { t: 2, v: 30 }]
    const b: Pt[] = [{ t: 0, v: 5 }, { t: 1, v: 40 }]
    expect(extentOf([a, b])).toEqual({ min: 5, max: 40 })
  })

  it('returns null when no finite values exist', () => {
    expect(extentOf([[{ t: 0, v: null }]])).toBeNull()
    expect(extentOf([])).toBeNull()
  })
})

describe('nearestValue', () => {
  const times = [0, 1, 2, 10]
  const vals = [100, 200, 300, 400]

  it('returns the nearest sample without interpolating', () => {
    expect(nearestValue(times, vals, 1.4)).toBe(200)
    expect(nearestValue(times, vals, 6)).toBe(300) // closer to t=2 than t=10
    expect(nearestValue(times, vals, 9)).toBe(400)
  })

  it('returns null when the nearest sample is missing or there are no samples', () => {
    expect(nearestValue([0, 1], [null, null], 0.5)).toBeNull()
    expect(nearestValue([], [], 3)).toBeNull()
  })
})

describe('lastFiniteT', () => {
  it('returns the last finite timestamp', () => {
    expect(lastFiniteT([0, 1, 2])).toBe(2)
    expect(lastFiniteT([0, 1, null])).toBe(1)
    expect(lastFiniteT([])).toBe(0)
  })
})

describe('sampledValueAt (no extrapolation past a session end)', () => {
  const times = [0, 1, 2, 10]
  const vals = [100, 200, 300, 400]

  it('returns the nearest value within the session span', () => {
    expect(sampledValueAt(times, vals, 1.4)).toBe(200)
    expect(sampledValueAt(times, vals, 10)).toBe(400)
  })

  it('returns null past the last sample or before the first', () => {
    expect(sampledValueAt(times, vals, 11)).toBeNull()
    expect(sampledValueAt([1, 2], [10, 20], 0.5)).toBeNull()
  })

  it('returns null when there are no samples', () => {
    expect(sampledValueAt([], [], 3)).toBeNull()
  })
})

describe('resolveXMax (unequal-duration series)', () => {
  const short = [0, 1, 2]
  const long = [0, 1, 2, 3, 4, 5]

  it('uses the longest session by default', () => {
    expect(resolveXMax([short, long], false)).toBe(5)
  })

  it('truncates to the shortest session for the common range', () => {
    expect(resolveXMax([short, long], true)).toBe(2)
  })

  it('ignores zero-length sessions so common-range does not collapse to 0', () => {
    expect(resolveXMax([long, []], true)).toBe(5)
    expect(resolveXMax([[], []], true)).toBe(0)
  })
})

describe('downsample', () => {
  it('returns the input unchanged when under the cap', () => {
    const rows: Pt[] = [{ t: 0, v: 1 }, { t: 1, v: 2 }]
    expect(downsample(rows, 10)).toBe(rows)
  })

  it('caps the point count while preserving the min and max spikes', () => {
    const rows: Pt[] = Array.from({ length: 1000 }, (_, i) => ({ t: i, v: i }))
    // Inject an extreme spike that must survive downsampling.
    rows[500] = { t: 500, v: 99999 }
    const out = downsample(rows, 100)
    expect(out.length).toBeLessThanOrEqual(100 + 2)
    expect(out.some((p) => p.v === 99999)).toBe(true)
    // Endpoints are always kept.
    expect(out[0].t).toBe(0)
    expect(out[out.length - 1].t).toBe(999)
  })
})
