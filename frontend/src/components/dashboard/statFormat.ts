// Formatting helpers for the Analysis Dashboard. A null / non-finite statistic
// always renders as an em dash ("—"), matching the backend's "no valid sample"
// contract.

import type { MetricStats } from '../../lib/types'

const DASH = '—'

/** Round to `digits` decimals and drop trailing zeros. Null / NaN -> "—". */
export function fmt(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  return String(Number(v.toFixed(digits)))
}

/** Integer with thousands separators. Null / NaN -> "—". */
export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  return Math.round(v).toLocaleString('en-US')
}

/** Whole seconds → H:MM:SS. */
export function fmtDuration(totalSec: number): string {
  const s = Math.max(0, Math.round(totalSec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const mm = String(m).padStart(2, '0')
  const ss = String(sec).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`
}

/** ISO timestamp → local "MM-DD HH:MM:SS" (best effort; raw string on failure). */
export function fmtTimestamp(iso: string | null | undefined): string {
  if (!iso) return DASH
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** ISO timestamp → local date "YYYY-MM-DD" for the library list. */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return DASH
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** True when a metric was recorded on fewer than 80% of samples (and at least one
 *  sample exists) — triggers the "partial data" warning. */
export function coverageLow(s: MetricStats | undefined): boolean {
  return !!s && s.total_count > 0 && s.coverage_percent < 80
}

export { DASH }
