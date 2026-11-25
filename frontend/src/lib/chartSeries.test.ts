import { describe, expect, it } from 'vitest'
import { currentRowValue, decimatePlotData, MAX_PLOT_POINTS, type ChartRow } from './chartSeries'

describe('chart series mechanics', () => {
  it('uses the actual last row and does not backfill a null current value', () => {
    const rows: ChartRow[] = [
      { t: 0, cpu_total: 33.3 },
      { t: 1, cpu_total: null },
    ]

    expect(currentRowValue(rows, 'cpu_total')).toBeNull()
  })

  it('preserves an app-only spike beyond the plot cap', () => {
    const rows: ChartRow[] = Array.from({ length: 1200 }, (_, t) => ({
      t,
      cpu_total: 20,
      cpu_app: t === 817 ? 96 : 10,
      'comparison:cpu_total': 18,
      'comparison:cpu_app': 9,
    }))

    const sampled = decimatePlotData(rows, [
      'cpu_total',
      'cpu_app',
      'comparison:cpu_total',
      'comparison:cpu_app',
    ])

    expect(sampled.length).toBeLessThanOrEqual(MAX_PLOT_POINTS)
    expect(sampled.some((row) => row.cpu_app === 96)).toBe(true)
  })

  it('preserves comparison extrema and a null row that breaks a CPU gap', () => {
    const rows: ChartRow[] = Array.from({ length: 1200 }, (_, t) => ({
      t,
      cpu_total: 20,
      cpu_app: t === 601 ? null : 10,
      'comparison:cpu_total': 18,
      'comparison:cpu_app': t === 913 ? 88 : 9,
    }))

    const sampled = decimatePlotData(rows, [
      'cpu_total',
      'cpu_app',
      'comparison:cpu_total',
      'comparison:cpu_app',
    ])

    expect(sampled.some((row) => row['comparison:cpu_app'] === 88)).toBe(true)
    expect(sampled.some((row) => row.cpu_app === null)).toBe(true)
  })
})
