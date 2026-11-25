import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { formatMmss, formatValue } from '../lib/format'
import {
  localizedLiveMetrics,
  metricPrimaryKeys,
  type LiveMetricDef,
  type LiveMetricKey,
} from '../lib/liveMetrics'
import { useCrosshair } from './liveCrosshair'
import { useMessages } from '../lib/i18n'
import type { TemperatureZone } from '../lib/thermalMetrics'

interface Props {
  /** Full-resolution chart rows (RowDatum). `t` is seconds from capture start
   *  and every metric's plotted key is present, so the overlay reads exactly
   *  what the cards plot. */
  data: Record<string, number | null>[]
  temperatureZone?: TemperatureZone
  anonymousMemory?: boolean
  legacyMemoryLabel?: string
}

/** Magnetic radius: when the cursor is within this many seconds of a pin, the
 *  overlay snaps to that pin's data (~1px at typical scale, per spec). */
const PIN_SNAP_SEC = 1

/** Index of the value in a sorted ascending array nearest to `target`. O(log n). */
function nearestIndex(sorted: number[], target: number): number {
  let lo = 0
  let hi = sorted.length - 1
  if (hi < 0) return -1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (sorted[mid] < target) lo = mid + 1
    else hi = mid
  }
  if (lo > 0 && Math.abs(sorted[lo - 1] - target) <= Math.abs(sorted[lo] - target)) return lo - 1
  return lo
}

export function HoverOverlay({ data, temperatureZone = 'battery', anonymousMemory = false, legacyMemoryLabel }: Props) {
  const msg = useMessages()
  const metricByKey = useMemo<Record<string, LiveMetricDef>>(
    () => Object.fromEntries(localizedLiveMetrics(msg.charts, temperatureZone).map((m) => [m.key, m] as const)),
    [msg.charts, temperatureZone]
  )
  const primaryKeys = { ...metricPrimaryKeys(temperatureZone), ...(anonymousMemory ? { mem: 'mem_anon_mib' } : {}) }
  const { hoverT, hoverKey, pins } = useCrosshair()
  const [pos, setPos] = useState<{ x: number; y: number }>({ x: 0, y: 0 })

  // The row `t` values are already sorted ascending (samples append in arrival
  // order), so we can binary-search them. Memoize so this only rebuilds when the
  // data array changes, not on every mouse move.
  const times = useMemo(() => data.map((d) => d.t as number), [data])

  // Coalesce high-frequency mousemove events into at most one position update
  // per animation frame so the overlay (and its re-render) can't outpace paint.
  useEffect(() => {
    let raf: number | null = null
    let next = { x: 0, y: 0 }
    function flush() {
      raf = null
      setPos(next)
    }
    function onMove(e: MouseEvent) {
      next = { x: e.clientX, y: e.clientY }
      if (raf === null) raf = requestAnimationFrame(flush)
    }
    window.addEventListener('mousemove', onMove)
    return () => {
      window.removeEventListener('mousemove', onMove)
      if (raf !== null) cancelAnimationFrame(raf)
    }
  }, [])

  // The overlay only appears while actively hovering — a pin no longer keeps it
  // open. When the cursor drifts within PIN_SNAP_SEC of a pin, it snaps to that
  // pin's time + headline (magnetic), so a pinned reading is easy to re-inspect.
  if (hoverT === null || data.length === 0) return null

  let nearestPinT: number | null = null
  let nearestDist = Infinity
  let nearestPinKey: LiveMetricKey | null = null
  for (const p of pins) {
    const d = Math.abs(p.t - hoverT)
    if (d < nearestDist) {
      nearestDist = d
      nearestPinT = p.t
      nearestPinKey = p.key
    }
  }
  const snapped = nearestPinT !== null && nearestDist <= PIN_SNAP_SEC
  const t = snapped ? (nearestPinT as number) : hoverT
  const activeKey = snapped ? nearestPinKey : hoverKey

  // Closest row to the crosshair time via binary search — O(log n).
  const idx = nearestIndex(times, t)
  const row = data[idx < 0 ? 0 : idx]
  const valueOf = (key: LiveMetricKey): number | null => {
    const v = row[primaryKeys[key]]
    return v === undefined ? null : v
  }
  const summaryRows = [
    { key: 'fps', label: metricByKey.fps.label, value: valueOf('fps'), digits: metricByKey.fps.digits, unit: metricByKey.fps.unit },
    { key: 'cpu-system', label: msg.charts.systemCpu, value: row.cpu_total, digits: 0, unit: '%' },
    { key: 'cpu-app', label: msg.charts.appCpuWholeDevice, value: row.cpu_app, digits: 0, unit: '%' },
    { key: 'gpu', label: metricByKey.gpu.label, value: valueOf('gpu'), digits: metricByKey.gpu.digits, unit: metricByKey.gpu.unit },
    { key: 'temp', label: metricByKey.temp.label, value: valueOf('temp'), digits: metricByKey.temp.digits, unit: metricByKey.temp.unit },
    { key: 'power', label: metricByKey.power.label, value: valueOf('power'), digits: metricByKey.power.digits, unit: metricByKey.power.unit },
  ]

  const def = activeKey ? metricByKey[activeKey] : undefined
  const headlineDef = activeKey === 'mem' && def
    ? { ...def, label: anonymousMemory ? 'Anonymous RSS + Swap' : legacyMemoryLabel ?? def.label,
        unit: anonymousMemory ? 'MiB' : def.unit }
    : def

  // Position: offset from cursor, but clamp to viewport so it never goes off-screen.
  const PADDING = 14
  const VIEWPORT_GUTTER = 8
  const WIDTH = Math.min(280, Math.max(1, window.innerWidth - VIEWPORT_GUTTER * 2))
  const EST_HEIGHT = 240
  let left = pos.x + PADDING
  let top = pos.y + PADDING
  if (left + WIDTH > window.innerWidth) left = pos.x - PADDING - WIDTH
  const maxLeft = Math.max(VIEWPORT_GUTTER, window.innerWidth - WIDTH - VIEWPORT_GUTTER)
  left = Math.min(Math.max(left, VIEWPORT_GUTTER), maxLeft)
  if (top + EST_HEIGHT > window.innerHeight) top = Math.max(0, window.innerHeight - EST_HEIGHT)

  return createPortal(
    <div
      role="tooltip"
      className="pointer-events-none fixed z-50 rounded-md border border-neutral-200 bg-white/95 p-2 text-xs shadow-lg backdrop-blur dark:border-neutral-700 dark:bg-neutral-900/95"
      style={{ left, top, width: WIDTH }}
    >
      {/* 대상 시간 (target time) */}
      <div className="mb-1.5 flex items-baseline justify-between gap-2 border-b border-neutral-200 pb-1 dark:border-neutral-700">
        <span className="font-mono text-neutral-700 dark:text-neutral-200">{formatMmss(t)}</span>
        {snapped && (
          <span className="text-[10px] uppercase tracking-wide text-clay-600 dark:text-clay-400">
            pinned
          </span>
        )}
      </div>

      {/* 최상단 대상 항목 값 (headline: the value of the hovered/pinned card) */}
      {headlineDef && (
        <div className="mb-2 flex items-baseline justify-between gap-2">
          <span
            className="text-[11px] font-semibold uppercase tracking-[0.06em]"
            style={{ color: headlineDef.accent }}
          >
            {headlineDef.key === 'cpu' ? msg.charts.systemCpu : headlineDef.label}
          </span>
          <span className="font-mono text-lg font-semibold leading-none text-neutral-900 tabular-nums dark:text-neutral-50">
            {formatValue(valueOf(headlineDef.key), headlineDef.digits)}
            <span className="ml-0.5 text-[11px] font-medium text-neutral-400">
              {headlineDef.unit}
            </span>
          </span>
        </div>
      )}

      {/* CPU rows are deliberately separate so system and app scope stay explicit. */}
      <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-0.5">
        {summaryRows.map((summary) => (
          <Row
            key={summary.key}
            label={summary.label}
            value={summary.value}
            digits={summary.digits}
            unit={summary.unit}
          />
        ))}
      </dl>

      {snapped && (
        <div className="mt-1 truncate text-[10px] text-neutral-500">{msg.hoverOverlay.pinned}</div>
      )}
    </div>,
    document.body
  )
}

function Row({
  label,
  value,
  digits,
  unit,
}: {
  label: string
  value: number | null | undefined
  digits: number
  unit?: string
}) {
  return (
    <>
      <dt className="text-neutral-500">{label}</dt>
      <dd className="text-right font-mono text-neutral-900 tabular-nums dark:text-neutral-100">
        {formatValue(value ?? null, digits)}
        {unit ? <span className="ml-0.5 text-neutral-400">{unit}</span> : null}
      </dd>
    </>
  )
}
