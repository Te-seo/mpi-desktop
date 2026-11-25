import { beforeEach, describe, expect, it } from 'vitest'
import {
  AUTO_STOP_STORAGE_KEY,
  DEFAULT_AUTO_STOP_AFTER_SEC,
  MAX_AUTO_STOP_AFTER_SEC,
  formatAutoStopTime,
  formatAutoStopTimeParts,
  loadAutoStopSettings,
  normalizeAutoStopAfterSec,
  normalizeAutoStopTimePart,
  parseAutoStopTime,
  parseAutoStopTimeParts,
  saveAutoStopSettings,
} from './autoStop'

describe('auto-stop settings', () => {
  beforeEach(() => localStorage.clear())

  it('defaults to OFF with a 00:40:00 target', () => {
    expect(loadAutoStopSettings()).toEqual({
      enabled: false,
      afterSec: DEFAULT_AUTO_STOP_AFTER_SEC,
    })
    expect(formatAutoStopTime(DEFAULT_AUTO_STOP_AFTER_SEC)).toBe('00:40:00')
  })

  it('round-trips a valid persisted setting', () => {
    saveAutoStopSettings({ enabled: true, afterSec: 3599 })
    expect(loadAutoStopSettings()).toEqual({ enabled: true, afterSec: 3599 })
    expect(localStorage.getItem(AUTO_STOP_STORAGE_KEY)).toBe(
      JSON.stringify({ enabled: true, afterSec: 3599 })
    )
  })

  it('recovers safely from corrupt and out-of-range persisted data', () => {
    localStorage.setItem(AUTO_STOP_STORAGE_KEY, '{broken')
    expect(loadAutoStopSettings().afterSec).toBe(DEFAULT_AUTO_STOP_AFTER_SEC)

    localStorage.setItem(
      AUTO_STOP_STORAGE_KEY,
      JSON.stringify({ enabled: 'true', afterSec: Number.NaN })
    )
    expect(loadAutoStopSettings()).toEqual({
      enabled: false,
      afterSec: DEFAULT_AUTO_STOP_AFTER_SEC,
    })
    localStorage.setItem(
      AUTO_STOP_STORAGE_KEY,
      JSON.stringify({ enabled: true, afterSec: 0 })
    )
    expect(loadAutoStopSettings()).toEqual({
      enabled: true,
      afterSec: DEFAULT_AUTO_STOP_AFTER_SEC,
    })
    localStorage.setItem(
      AUTO_STOP_STORAGE_KEY,
      JSON.stringify({ enabled: true, afterSec: MAX_AUTO_STOP_AFTER_SEC + 1 })
    )
    expect(loadAutoStopSettings()).toEqual({
      enabled: true,
      afterSec: DEFAULT_AUTO_STOP_AFTER_SEC,
    })
    expect(normalizeAutoStopAfterSec(0)).toBe(1)
    expect(normalizeAutoStopAfterSec(MAX_AUTO_STOP_AFTER_SEC + 1)).toBe(
      MAX_AUTO_STOP_AFTER_SEC
    )
  })

  it('parses and formats strict HH:MM:SS duration values', () => {
    expect(parseAutoStopTime('00:00:01')).toBe(1)
    expect(parseAutoStopTime('00:59:59')).toBe(3599)
    expect(parseAutoStopTime('01:00:00')).toBe(3600)
    expect(parseAutoStopTime('01:00:00')).toBe(MAX_AUTO_STOP_AFTER_SEC)
    expect(formatAutoStopTime(366)).toBe('00:06:06')

    expect(parseAutoStopTime('00:00:00')).toBeNull()
    expect(parseAutoStopTime('1:00:00')).toBeNull()
    expect(parseAutoStopTime('00:60:00')).toBeNull()
    expect(parseAutoStopTime('01:00:01')).toBeNull()
    expect(parseAutoStopTime('99:59:59')).toBeNull()
    expect(parseAutoStopTime('not-a-time')).toBeNull()
  })

  it('accepts intermediate parts, pads on blur, and enforces the full range', () => {
    expect(parseAutoStopTimeParts({ hours: '0', minutes: '4', seconds: '2' })).toBe(242)
    expect(parseAutoStopTimeParts({ hours: '', minutes: '40', seconds: '00' })).toBeNull()
    expect(parseAutoStopTimeParts({ hours: '00', minutes: '60', seconds: '00' })).toBeNull()
    expect(parseAutoStopTimeParts({ hours: '01', minutes: '00', seconds: '00' })).toBe(3600)
    expect(parseAutoStopTimeParts({ hours: '01', minutes: '00', seconds: '01' })).toBeNull()
    expect(normalizeAutoStopTimePart('7')).toBe('07')
    expect(normalizeAutoStopTimePart('')).toBe('')
    expect(formatAutoStopTimeParts(2400)).toEqual({
      hours: '00',
      minutes: '40',
      seconds: '00',
    })
  })

  it('migrates a valid legacy setting exactly once and removes the old key', () => {
    localStorage.setItem(
      'perfinspect.autoStop.v1',
      JSON.stringify({ enabled: true, afterSec: 3599 })
    )

    expect(loadAutoStopSettings()).toEqual({ enabled: true, afterSec: 3599 })
    expect(localStorage.getItem(AUTO_STOP_STORAGE_KEY)).toBe(
      JSON.stringify({ enabled: true, afterSec: 3599 })
    )
    expect(localStorage.getItem('perfinspect.autoStop.v1')).toBeNull()
  })
})
