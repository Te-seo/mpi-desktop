import { beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_TEMPERATURE_THRESHOLD_C,
  MAX_TEMPERATURE_THRESHOLD_C,
  MIN_TEMPERATURE_THRESHOLD_C,
  TEMPERATURE_GUARD_STORAGE_KEY,
  loadTemperatureGuardSettings,
  normalizeTemperatureThreshold,
  saveTemperatureGuardSettings,
} from './temperatureGuard'

describe('temperature guard preferences', () => {
  beforeEach(() => localStorage.clear())

  it('defaults to OFF at 25.0 C', () => {
    expect(DEFAULT_TEMPERATURE_THRESHOLD_C).toBe(25)
    expect(loadTemperatureGuardSettings()).toEqual({
      enabled: false,
      thresholdC: DEFAULT_TEMPERATURE_THRESHOLD_C,
    })
  })

  it('migrates the persisted v1 default from 40.0 C to 25.0 C', () => {
    localStorage.setItem(
      'mpi.temperatureGuard.v1',
      JSON.stringify({ enabled: true, thresholdC: 40 })
    )

    expect(loadTemperatureGuardSettings()).toEqual({ enabled: true, thresholdC: 25 })
    expect(JSON.parse(localStorage.getItem(TEMPERATURE_GUARD_STORAGE_KEY) ?? '{}')).toEqual({
      enabled: true,
      thresholdC: 25,
    })
    expect(localStorage.getItem('mpi.temperatureGuard.v1')).toBeNull()
  })

  it('preserves a custom v1 threshold during migration', () => {
    localStorage.setItem(
      'mpi.temperatureGuard.v1',
      JSON.stringify({ enabled: false, thresholdC: 37.5 })
    )

    expect(loadTemperatureGuardSettings()).toEqual({ enabled: false, thresholdC: 37.5 })
  })

  it('restores valid pre-MPI settings over a field-damaged current value', () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: 'yes', thresholdC: 'hot' })
    )
    localStorage.setItem(
      'perfinspect.temperatureGuard.v2',
      JSON.stringify({ enabled: true, thresholdC: 31.5 })
    )

    expect(loadTemperatureGuardSettings()).toEqual({
      enabled: true,
      thresholdC: 31.5,
    })
    expect(localStorage.getItem('perfinspect.temperatureGuard.v2')).toBeNull()
  })

  it('persists preferences without any runtime waiting state', () => {
    saveTemperatureGuardSettings({ enabled: true, thresholdC: 39.94 })

    expect(loadTemperatureGuardSettings()).toEqual({ enabled: true, thresholdC: 39.9 })
    expect(JSON.parse(localStorage.getItem(TEMPERATURE_GUARD_STORAGE_KEY) ?? '{}')).toEqual({
      enabled: true,
      thresholdC: 39.9,
    })
  })

  it('normalizes invalid and out-of-range thresholds', () => {
    expect(normalizeTemperatureThreshold(Number.NaN)).toBe(DEFAULT_TEMPERATURE_THRESHOLD_C)
    expect(normalizeTemperatureThreshold(Number.POSITIVE_INFINITY)).toBe(
      DEFAULT_TEMPERATURE_THRESHOLD_C
    )
    expect(normalizeTemperatureThreshold(0)).toBe(MIN_TEMPERATURE_THRESHOLD_C)
    expect(normalizeTemperatureThreshold(100)).toBe(MAX_TEMPERATURE_THRESHOLD_C)
  })
})
