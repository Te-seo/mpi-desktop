import { beforeEach, describe, expect, it } from 'vitest'
import {
  DEVICE_OVERLAY_STORAGE_KEY,
  loadDeviceOverlaySettings,
  deviceOverlayMetricList,
} from './deviceOverlay'

describe('device overlay preference migration', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('replaces a field-damaged current value with valid legacy settings', () => {
    localStorage.setItem(
      DEVICE_OVERLAY_STORAGE_KEY,
      JSON.stringify({
        enabled: 'yes',
        metrics: { fps: 'yes' },
      })
    )
    localStorage.setItem(
      'perfinspect.deviceOverlay.v1',
      JSON.stringify({
        enabled: true,
        metrics: {
          fps: false,
          cpu: true,
          gpu: false,
          temp: true,
          net: false,
        },
      })
    )

    expect(loadDeviceOverlaySettings()).toEqual({
      enabled: true,
      metrics: {
        elapsed: true,
        fps: false,
        cpu: true,
        gpu: false,
        skin: true,
        net: false,
      },
    })
    expect(localStorage.getItem('perfinspect.deviceOverlay.v1')).toBeNull()
  })

  it('migrates the legacy visibility choice but sends Skin as a distinct metric', () => {
    localStorage.setItem(DEVICE_OVERLAY_STORAGE_KEY, JSON.stringify({ enabled: true, metrics: { temp: false } }))
    const migrated = loadDeviceOverlaySettings()
    expect(migrated.metrics.skin).toBe(false)
    expect(deviceOverlayMetricList(migrated)).not.toContain('temp')
    expect(deviceOverlayMetricList(migrated)).not.toContain('skin')
    migrated.metrics.skin = true
    expect(deviceOverlayMetricList(migrated)).toContain('skin')
    expect(deviceOverlayMetricList(migrated)).not.toContain('temp')
  })

  it('prefers an explicit Skin preference over the old temperature preference', () => {
    localStorage.setItem(DEVICE_OVERLAY_STORAGE_KEY, JSON.stringify({ enabled: true, metrics: { temp: false, skin: true } }))
    expect(loadDeviceOverlaySettings().metrics.skin).toBe(true)
  })
})
