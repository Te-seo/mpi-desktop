import { describe, expect, it } from 'vitest'
import { coverageLow, fmt, fmtInt } from './statFormat'
import { makeStats } from '../../test/fixtures'

describe('fmt', () => {
  it('renders an em dash for null / undefined / non-finite', () => {
    expect(fmt(null)).toBe('—')
    expect(fmt(undefined)).toBe('—')
    expect(fmt(NaN)).toBe('—')
    expect(fmt(Infinity)).toBe('—')
  })

  it('rounds to the requested decimals and drops trailing zeros', () => {
    expect(fmt(60.04, 1)).toBe('60')
    expect(fmt(59.64, 1)).toBe('59.6')
    expect(fmt(2000, 0)).toBe('2000')
    expect(fmt(2.5, 2)).toBe('2.5')
  })
})

describe('fmtInt', () => {
  it('formats integers with separators; dash for missing', () => {
    expect(fmtInt(12345)).toBe('12,345')
    expect(fmtInt(null)).toBe('—')
  })
})

describe('coverageLow', () => {
  it('is true only when coverage < 80 with samples present', () => {
    expect(coverageLow(makeStats({ coverage_percent: 79.9, total_count: 10 }))).toBe(true)
    expect(coverageLow(makeStats({ coverage_percent: 80, total_count: 10 }))).toBe(false)
    expect(coverageLow(makeStats({ coverage_percent: 0, total_count: 0 }))).toBe(false)
    expect(coverageLow(undefined)).toBe(false)
  })
})
