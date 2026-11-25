export type ChartRow = Record<string, number | null>

export const MAX_PLOT_POINTS = 700

function finiteValue(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** The displayed "current" value is the actual final sample, never an older value. */
export function currentRowValue(rows: readonly ChartRow[], key: string): number | null {
  if (rows.length === 0) return null
  return finiteValue(rows[rows.length - 1][key])
}

/**
 * Thin chart rows while preserving extrema for every series sharing the primary
 * axis. A null representative is also retained per series/bucket so a missing
 * CPU interval remains a visible break when Recharts uses connectNulls=false.
 */
export function decimatePlotData(
  rows: ChartRow[],
  seriesKeys: readonly string[]
): ChartRow[] {
  const n = rows.length
  if (n <= MAX_PLOT_POINTS) return rows

  const keys = [...new Set(seriesKeys.filter(Boolean))]
  if (keys.length === 0) return [rows[0], rows[n - 1]]

  // Per key and bucket: minimum, maximum, and one null-gap representative.
  // Reserving the first/last rows keeps the returned set within the paint cap.
  const maxPerBucket = keys.length * 3
  const buckets = Math.max(1, Math.floor((MAX_PLOT_POINTS - 2) / maxPerBucket))
  const step = n / buckets
  const indices = new Set<number>([0, n - 1])

  for (let bucket = 0; bucket < buckets; bucket++) {
    const start = Math.floor(bucket * step)
    const end = Math.min(n, Math.floor((bucket + 1) * step))

    for (const key of keys) {
      let minIndex = -1
      let maxIndex = -1
      let nullIndex = -1
      let minValue = Infinity
      let maxValue = -Infinity

      for (let i = start; i < end; i++) {
        const value = finiteValue(rows[i][key])
        if (value === null) {
          if (nullIndex === -1) nullIndex = i
          continue
        }
        if (value < minValue) {
          minValue = value
          minIndex = i
        }
        if (value > maxValue) {
          maxValue = value
          maxIndex = i
        }
      }

      if (minIndex >= 0) indices.add(minIndex)
      if (maxIndex >= 0) indices.add(maxIndex)
      if (nullIndex >= 0) indices.add(nullIndex)
    }
  }

  return [...indices]
    .sort((a, b) => a - b)
    .map((index) => rows[index])
}
