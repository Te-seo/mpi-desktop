import { describe, expect, it } from 'vitest'
import { normalizeThermalZones, thermalSeriesForZone } from './thermalMetrics'

describe('thermal sensor naming compatibility', () => {
  it('maps old battery aliases to the primary sensor without replacing a canonical reading', () => {
    expect(normalizeThermalZones({ 'thermal:battery': 34, battery: 35, cpu_max: 50 })).toEqual({ battery: 35, cpu_max: 50 })
    expect(normalizeThermalZones({ battery: 35, 'thermal:battery': 34 })).toEqual({ battery: 35 })
    expect(normalizeThermalZones({ 'thermal:thermal:battery': 0, invalid: NaN })).toEqual({ battery: 0 })
  })
  it('loads old and current analysis battery series without using CPU temperature', () => {
    expect(thermalSeriesForZone({ 'thermal:thermal:battery': [30, null, 32] }, 'thermal:battery')).toEqual([30, null, 32])
    expect(thermalSeriesForZone({ 'thermal:battery': [0] }, 'battery')).toEqual([0])
    expect(thermalSeriesForZone({ 'thermal:cpu_max': [70] }, 'battery')).toEqual([])
  })
})
