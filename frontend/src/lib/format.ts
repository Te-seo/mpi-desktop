// Small numeric / time formatters shared by the live dashboard.

/** Seconds → HH:MM:SS (zero-padded). */
export function formatHms(totalSec: number): string {
  const s = Math.max(0, Math.floor(totalSec))
  const h = String(Math.floor(s / 3600)).padStart(2, '0')
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0')
  const sec = String(s % 60).padStart(2, '0')
  return `${h}:${m}:${sec}`
}

/** Chart times in MM:SS with fractional seconds for sub-second ticks and samples. */
export function formatMmss(totalSec: number): string {
  const milliseconds = Math.max(0, Math.round(totalSec * 1000))
  const s = Math.floor(milliseconds / 1000)
  const m = String(Math.floor(s / 60)).padStart(2, '0')
  const sec = String(s % 60).padStart(2, '0')
  const fraction = String(milliseconds % 1000).padStart(3, '0').replace(/0+$/, '')
  return fraction ? `${m}:${sec}.${fraction}` : `${m}:${sec}`
}

/** Compact axis label: 6000 → "6k", 3200 → "3.2k", small values pass through. */
export function shortNum(x: number): string {
  if (!Number.isFinite(x)) return '—'
  const abs = Math.abs(x)
  if (abs >= 1000) {
    const k = x / 1000
    return (Number.isInteger(k) ? k : Math.round(k * 10) / 10) + 'k'
  }
  return Number.isInteger(x) ? String(x) : String(Math.round(x * 10) / 10)
}

/** Format a metric value with a fixed number of decimals (integers stay bare). */
export function formatValue(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (digits === 0) return Math.round(v).toLocaleString('en-US')
  return v.toFixed(digits)
}
