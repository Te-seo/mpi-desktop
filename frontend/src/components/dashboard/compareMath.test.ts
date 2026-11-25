import { describe, expect, it } from 'vitest'
import { deltaParts, mismatchDims } from './compareMath'
import { makeAnalysis } from '../../test/fixtures'

describe('deltaParts', () => {
  it('computes absolute and percent delta', () => {
    expect(deltaParts(50, 60)).toEqual({ abs: 10, pct: 20 })
    expect(deltaParts(60, 45)).toEqual({ abs: -15, pct: -25 })
  })

  it('uses |baseline| for the percent so sign comes from the delta', () => {
    expect(deltaParts(-40, -30)).toEqual({ abs: 10, pct: 25 })
  })

  it('returns null percent when the baseline is 0', () => {
    expect(deltaParts(0, 5)).toEqual({ abs: 5, pct: null })
  })

  it('returns nulls when either value is unavailable', () => {
    expect(deltaParts(null, 5)).toEqual({ abs: null, pct: null })
    expect(deltaParts(5, null)).toEqual({ abs: null, pct: null })
    expect(deltaParts(NaN, 5)).toEqual({ abs: null, pct: null })
  })
})

describe('mismatchDims', () => {
  it('is empty for fewer than two sessions', () => {
    expect(mismatchDims([])).toEqual([])
    expect(mismatchDims([makeAnalysis()])).toEqual([])
  })

  it('is empty when all sessions match', () => {
    expect(mismatchDims([makeAnalysis(), makeAnalysis()])).toEqual([])
  })

  it('flags each differing dimension', () => {
    const a = makeAnalysis()
    const b = makeAnalysis({
      platform: 'ios',
      device_model: 'iPhone 15',
      bundle_id: 'com.other',
      app_version: '2.0',
    })
    expect(mismatchDims([a, b]).sort()).toEqual(['app', 'device', 'platform', 'version'])
  })

  it('flags only the platform when only platform differs', () => {
    const a = makeAnalysis()
    const b = makeAnalysis({ platform: 'ios' })
    expect(mismatchDims([a, b])).toEqual(['platform'])
  })
})
