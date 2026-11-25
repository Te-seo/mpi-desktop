import { memoryReferenceLabel } from './MemoryReferenceLabel'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from 'recharts'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useId,
  useState,
  type MouseEvent,
  type ReactNode,
  type WheelEvent,
} from 'react'
import { createPortal } from 'react-dom'
import { CircleHelp } from 'lucide-react'
import { PIN_COLORS, useCrosshair } from './liveCrosshair'
import { formatMmss, formatValue, shortNum } from '../lib/format'
import { useMessages } from '../lib/i18n'
import type { LiveMetricKey, MetricSeriesDef } from '../lib/liveMetrics'
import { currentRowValue, decimatePlotData } from '../lib/chartSeries'

/** Subset of the props recharts injects into a custom XAxis tick renderer.
 *  Coordinates arrive as string|number, so callers coerce with Number(). */
interface TimeTickProps {
  x?: number | string
  y?: number | string
  payload?: { value?: number | string }
  index?: number
  visibleTicksCount?: number
}

interface Props {
  /** Which metric this card plots — reported to the crosshair so the hover
   *  overlay can headline the card the cursor is actually over. */
  metricKey: LiveMetricKey
  label: string
  accent: string
  unit: string
  digits: number
  helpText?: string
  statusText?: string
  data: Record<string, number | null>[]
  /** Series rendered as the headline value + sparkline. */
  primaryKey: string
  /** Legend label for the headline series (so it's named in the legend row). */
  primaryLabel?: string
  comparisonKey?: string
  comparisonLabel?: string
  /** Dashed comparison counterparts for same-axis secondary series. */
  comparisonSecondary?: MetricSeriesDef[]
  /** Extra series surfaced as a compact current-value legend (not plotted). */
  secondary?: MetricSeriesDef[]
  yMin?: number
  yMax?: number
  /** Drag handle (grip) injected by the sortable wrapper; renders at the card's
   *  left edge. Dragging it reorders the cards (separate from chart mouse-drag). */
  dragHandle?: ReactNode
  headerControl?: ReactNode
  horizontalReference?: { value: number; label: string }
}

interface HelpPopupPosition {
  left: number
  width: number
  top?: number
  bottom?: number
}

// Chart geometry shared by the mini y-axis and the recharts plot. The XAxis is
// given a fixed height, so the actual plotting rectangle is
// CHART_H - PLOT_TOP - X_AXIS_H. The hi/mid/lo labels are pinned to that rect
// (not the full container) so they line up with the top/middle/bottom of the
// chart instead of drifting down into the time-axis row.
const CHART_H = 82
const PLOT_TOP = 4
const X_AXIS_H = 18
const PLOT_H = CHART_H - PLOT_TOP - X_AXIS_H

export function MetricCard({
  metricKey,
  label,
  accent,
  unit,
  digits,
  helpText,
  statusText,
  data,
  primaryKey,
  primaryLabel,
  comparisonKey,
  comparisonLabel,
  comparisonSecondary = [],
  secondary = [],
  yMin,
  yMax,
  dragHandle,
  headerControl,
  horizontalReference,
}: Props) {
  const t = useMessages()
  const helpId = useId()
  const helpButtonRef = useRef<HTMLButtonElement>(null)
  const [helpPopup, setHelpPopup] = useState<HelpPopupPosition | null>(null)
  const {
    hoverT,
    pins,
    selection,
    setHover,
    togglePin,
    addPin,
    setSelection,
    openMenu,
    xDomain,
    setXDomain,
    resetZoom,
  } = useCrosshair()

  const showHelp = useCallback(() => {
    const anchor = helpButtonRef.current
    if (!anchor || typeof window === 'undefined') return
    const rect = anchor.getBoundingClientRect()
    const viewportPadding = 8
    const width = Math.min(320, Math.max(0, window.innerWidth - viewportPadding * 2))
    const left = Math.max(
      viewportPadding,
      Math.min(rect.left, window.innerWidth - width - viewportPadding)
    )
    const estimatedHeight = 120
    if (rect.bottom + 6 + estimatedHeight <= window.innerHeight) {
      setHelpPopup({ left, top: rect.bottom + 6, width })
    } else {
      setHelpPopup({ left, bottom: window.innerHeight - rect.top + 6, width })
    }
  }, [])

  const isCpuMetric = metricKey === 'cpu' || metricKey === 'cpu_norm'
  const connectNulls = false
  const displayedValue = (key: string) => currentRowValue(data, key)
  const current = displayedValue(primaryKey)

  // Frame the primary y-axis around EVERY series drawn on it: the headline
  // series plus any secondary plotted with axis:'primary' (e.g. the CPU card's
  // whole-device app share or the FPS card's jank lines). Scanning only primaryKey would let those
  // overlays be clipped flat against the top. The signature string keeps this
  // memo stable across hovers (the `secondary` array is rebuilt each render).
  const primaryAxisSig = [
    primaryKey,
    ...secondary.filter((s) => s.axis === 'primary').map((s) => s.key),
    ...(comparisonKey ? [comparisonKey] : []),
    ...comparisonSecondary.filter((s) => s.axis === 'primary').map((s) => s.key),
  ].join(',')

  const { lo, hi } = useMemo(() => {
    const keys = primaryAxisSig.split(',')
    let dataMin = Infinity
    let dataMax = -Infinity
    for (const row of data) {
      for (const key of keys) {
        const v = row[key]
        if (v !== null && v !== undefined && !Number.isNaN(v)) {
          dataMin = Math.min(dataMin, v as number)
          dataMax = Math.max(dataMax, v as number)
        }
      }
    }
    if (!Number.isFinite(dataMin)) {
      dataMin = yMin ?? 0
      dataMax = yMax ?? 1
    }
    const loV = yMin ?? Math.floor(dataMin)
    // yMax (when given) is a SOFT default ceiling for framing, not a hard clip:
    // the axis grows past it so a true reading above the nominal range (e.g.
    // a momentary GPU busy value above its nominal range) renders at full
    // height instead of being flattened against the top by allowDataOverflow.
    let hiV = Math.max(yMax ?? -Infinity, Math.ceil(dataMax), horizontalReference ? Math.ceil(horizontalReference.value * 1.32) : -Infinity)
    if (loV >= hiV) hiV = loV + 1
    return { lo: loV, hi: hiV }
  }, [data, primaryAxisSig, yMin, yMax, horizontalReference?.value])

  // Thin every same-axis series together so an app/comparison-only spike or a
  // null gap cannot disappear merely because the system headline stayed flat.
  const plotData = useMemo(
    () => decimatePlotData(data, primaryAxisSig.split(',')),
    [data, primaryAxisSig]
  )

  const axisVals = [shortNum(hi), shortNum((lo + hi) / 2), shortNum(lo)]
  const gradId = `mc-grad-${primaryKey.replace(/[^a-z0-9]/gi, '')}`

  // Anchor the first/last time ticks to the inside of the plot so the edge
  // labels (e.g. "0:00" and the final "m:ss") are not half-clipped by the
  // chart bounds. Middle ticks stay centred as usual.
  const renderTimeTick = useCallback((props: TimeTickProps) => {
    const { x = 0, y = 0, payload, index = 0, visibleTicksCount = 0 } = props
    const anchor = index === 0 ? 'start' : index === visibleTicksCount - 1 ? 'end' : 'middle'
    return (
      <text
        x={Number(x)}
        y={Number(y)}
        dy="0.71em"
        textAnchor={anchor}
        fontSize={10}
        fontFamily="var(--font-mono)"
        fill="var(--color-faint)"
      >
        {formatMmss(Number(payload?.value ?? 0))}
      </text>
    )
  }, [])

  // Pointer model on the chart, all keyed off recharts' activeLabel (= the time
  // under the cursor). A press that doesn't move is a click; a press that moves
  // is a drag:
  //   click            → toggle a single pin
  //   Ctrl/Cmd+click   → add/remove one pin (multiple pins)
  //   drag             → set a summary selection (range)
  //   Shift+drag       → zoom all charts to the dragged range
  //   right-click on a selection → "save range" menu
  const dragRef = useRef<{
    startT: number
    mode: 'summary' | 'zoom'
    ctrl: boolean
    moved: boolean
  } | null>(null)
  const lastTRef = useRef<number | null>(null)

  const finishDrag = useCallback(() => {
    const d = dragRef.current
    dragRef.current = null
    if (!d) return
    const endT = lastTRef.current
    if (!d.moved || endT === null) {
      // No movement → treat as a click (pin).
      if (d.ctrl) addPin(d.startT, metricKey)
      else togglePin(d.startT, metricKey)
      setSelection(null)
      return
    }
    const lo = Math.min(d.startT, endT)
    const hi = Math.max(d.startT, endT)
    if (d.mode === 'zoom') {
      if (hi > lo) setXDomain([lo, hi])
      setSelection(null)
    }
    // summary: the selection was set live during the drag — keep it.
  }, [addPin, togglePin, setSelection, setXDomain, metricKey])

  // A drag can end with the cursor off the chart (recharts won't fire its own
  // mouseup then), so finalize on a window-level mouseup as a fallback.
  useEffect(() => {
    const onUp = () => {
      if (dragRef.current) finishDrag()
    }
    window.addEventListener('mouseup', onUp)
    return () => window.removeEventListener('mouseup', onUp)
  }, [finishDrag])

  const onMouseDown = useCallback(
    (state: { activeLabel?: string | number }, e: MouseEvent) => {
      // Left button only — a right-click must not clear an existing selection
      // (the "save range" menu needs it to survive the contextmenu press).
      if (e && typeof e.button === 'number' && e.button !== 0) return
      const t = typeof state?.activeLabel === 'number' ? state.activeLabel : null
      if (t === null || !Number.isFinite(t)) return
      dragRef.current = {
        startT: t,
        mode: e?.shiftKey ? 'zoom' : 'summary',
        ctrl: !!(e?.ctrlKey || e?.metaKey),
        moved: false,
      }
      lastTRef.current = t
      setSelection(null)
    },
    [setSelection]
  )

  const onMouseMove = useCallback(
    (state: { activeLabel?: string | number }) => {
      const t = typeof state?.activeLabel === 'number' ? state.activeLabel : null
      if (t === null || !Number.isFinite(t)) {
        setHover(null, null)
        return
      }
      lastTRef.current = t
      setHover(t, metricKey)
      const d = dragRef.current
      if (d) {
        if (Math.abs(t - d.startT) > 1e-6) d.moved = true
        if (d.moved) {
          setSelection({
            startT: Math.min(d.startT, t),
            endT: Math.max(d.startT, t),
            mode: d.mode,
          })
        }
      }
    },
    [setHover, metricKey, setSelection]
  )
  const onMouseUp = useCallback(() => {
    if (dragRef.current) finishDrag()
  }, [finishDrag])
  const onMouseLeave = useCallback(() => setHover(null, null), [setHover])

  const onContextMenu = useCallback(
    (e: MouseEvent) => {
      // Right-click over a summary selection opens the save menu.
      if (selection && selection.mode === 'summary') {
        e.preventDefault()
        openMenu({ x: e.clientX, y: e.clientY })
      }
    },
    [selection, openMenu]
  )

  const onWheel = useCallback(
    (e: WheelEvent<HTMLDivElement>) => {
      const modifier = e.ctrlKey || e.shiftKey || e.altKey || e.metaKey
      if (!modifier) return
      e.preventDefault()
      e.stopPropagation()
      let domain = xDomain
      if (domain === null && data.length > 0) {
        const ts = data.map((d) => d.t as number).filter((t) => Number.isFinite(t))
        if (ts.length === 0) return
        domain = [Math.min(...ts), Math.max(...ts)]
      }
      if (!domain) return
      const [dlo, dhi] = domain
      const range = dhi - dlo
      if (range <= 0) return
      const factor = e.deltaY > 0 ? 1.25 : 0.8
      const newRange = range * factor
      const pivot = hoverT ?? (dlo + dhi) / 2
      const leftFrac = (pivot - dlo) / range
      const newLo = pivot - leftFrac * newRange
      setXDomain([newLo, newLo + newRange])
    },
    [xDomain, data, hoverT, setXDomain]
  )

  const onDoubleClick = useCallback(() => resetZoom(), [resetZoom])

  // Legend row: the headline (primary) series first — so e.g. battery temp and
  // system CPU% are named alongside their overlays — then the secondaries.
  const legendRows = [
    { key: primaryKey, label: primaryLabel ?? label, color: accent, value: current },
    ...secondary.map((s) => ({ key: s.key, label: s.label, color: s.color, value: displayedValue(s.key) })),
  ].filter((series) =>
    series.value !== null || series.key === primaryKey || (isCpuMetric && series.key === 'cpu_app')
  )
  const comparisonValue = comparisonKey ? displayedValue(comparisonKey) : null
  const comparisonSecondaryRows = comparisonSecondary.map((series) => ({
    ...series,
    value: displayedValue(series.key),
  }))

  return (
    <>
      <div className="mpi-panel mpi-metric-card px-5 py-4">
      <div className="mpi-metric-row flex items-center gap-5">
        {dragHandle}
        {/* value block */}
        <div className="mpi-metric-value w-[144px] flex-none">
          <div className="mb-1 flex items-center gap-2">
            <span
              className="h-[9px] w-[9px] flex-none rounded-[3px]"
              style={{ backgroundColor: accent }}
            />
            <span className="text-[12px] font-semibold uppercase tracking-[0.06em] text-muted">
              {label}
            </span>
            {helpText && (
              <span className="flex-none">
                <button
                  ref={helpButtonRef}
                  type="button"
                  className="flex h-6 w-6 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-ink"
                  aria-label={`${label}: ${helpText}`}
                  aria-describedby={helpId}
                  onMouseEnter={showHelp}
                  onMouseLeave={() => setHelpPopup(null)}
                  onFocus={showHelp}
                  onBlur={() => setHelpPopup(null)}
                >
                  <CircleHelp size={13} aria-hidden="true" />
                </button>
              </span>
            )}
          </div>
          {headerControl}
          <div
            data-testid={`metric-current-${metricKey}`}
            className="font-mono text-[30px] font-semibold leading-none tracking-tight text-ink tabular-nums"
          >
            {formatValue(current, digits)}
            <span className="ml-1 text-[13px] font-medium text-muted">{unit}</span>
          </div>
          <div className="mt-1 font-mono text-[11px] text-faint">
            range {shortNum(lo)}–{shortNum(hi)}
          </div>
          {statusText && <p className="mt-2 break-words text-[12px] leading-relaxed text-muted" title={statusText}>{statusText}</p>}
        </div>

        {/* mini y-axis — labels pinned to the plot rect so hi/mid/lo line up
            with the chart's top/middle/bottom gridlines (not the time-axis row). */}
        <div
          className="relative w-7 flex-none font-mono text-[10px] font-medium text-faint"
          style={{ height: CHART_H }}
        >
          {axisVals.map((v, i) => (
            <span
              key={i}
              className="absolute right-0 leading-none"
              style={{
                top: PLOT_TOP + (i * PLOT_H) / 2,
                transform:
                  i === 0
                    ? 'none'
                    : i === axisVals.length - 1
                      ? 'translateY(-100%)'
                      : 'translateY(-50%)',
              }}
            >
              {v}
            </span>
          ))}
        </div>

        {/* chart */}
        <div
          className="min-w-0 flex-1 select-none outline-none focus:outline-none focus-visible:outline-none"
          tabIndex={-1}
          style={{ height: CHART_H }}
          data-live-chart-plot
          onMouseLeave={onMouseLeave}
          onPointerLeave={onMouseLeave}
          onPointerCancel={onMouseLeave}
          onWheel={onWheel}
          onDoubleClick={onDoubleClick}
          onContextMenu={onContextMenu}
          title={t.metricCard.chartHelp}
        >
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart
              data={plotData}
              margin={{ top: PLOT_TOP, right: 4, bottom: 0, left: 0 }}
              onMouseDown={onMouseDown}
              onMouseMove={onMouseMove}
              onMouseUp={onMouseUp}
              onMouseLeave={onMouseLeave}
            >
              <defs>
                <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={accent} stopOpacity={0.28} />
                  <stop offset="100%" stopColor={accent} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid vertical={false} stroke="var(--color-grid)" />
              {/* 시간별 수직 점선 라인: dashed vertical guides at each time tick so
                  readings can be lined up across cards. */}
              <CartesianGrid
                horizontal={false}
                vertical
                stroke="var(--color-grid)"
                strokeDasharray="3 3"
              />
              <XAxis
                dataKey="t"
                type="number"
                height={X_AXIS_H}
                domain={xDomain ?? ['dataMin', 'dataMax']}
                tickFormatter={(t) => formatMmss(t as number)}
                tick={renderTimeTick}
                tickLine={false}
                axisLine={{ stroke: 'var(--color-line)' }}
                minTickGap={28}
                allowDataOverflow
              />
              <YAxis yAxisId="primary" domain={[lo, hi]} hide allowDataOverflow />
              {/* Secondary series keep their own (independent) scale so e.g.
                  per-core MHz don't get squashed against a 0–100% primary axis. */}
              {secondary.some((series) => series.axis !== 'primary') && (
                <YAxis yAxisId="secondary" domain={['dataMin', 'dataMax']} hide allowDataOverflow />
              )}
              {secondary.map((s) => (
                <Line
                  key={s.key}
                  yAxisId={s.axis === 'primary' ? 'primary' : 'secondary'}
                  type="linear"
                  dataKey={s.key}
                  stroke={s.color}
                  strokeWidth={1}
                  strokeOpacity={0.5}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls={connectNulls}
                />
              ))}
              <Area
                yAxisId="primary"
                type="linear"
                dataKey={primaryKey}
                stroke={accent}
                strokeWidth={2}
                fill={`url(#${gradId})`}
                dot={false}
                isAnimationActive={false}
                connectNulls={connectNulls}
              />
              {comparisonKey && (
                <Line
                  yAxisId="primary"
                  type="linear"
                  dataKey={comparisonKey}
                  stroke={accent}
                  strokeWidth={2}
                  strokeDasharray="6 4"
                  strokeOpacity={0.85}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls={connectNulls}
                />
              )}
              {comparisonSecondary.map((series) => (
                <Line
                  key={series.key}
                  yAxisId={series.axis === 'secondary' ? 'secondary' : 'primary'}
                  type="linear"
                  dataKey={series.key}
                  stroke={series.color}
                  strokeWidth={1.5}
                  strokeDasharray="6 4"
                  strokeOpacity={0.85}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls={connectNulls}
                />
              ))}
              {/* drag region: summary (neutral) or zoom preview (accent) */}
              {selection && (
                <ReferenceArea
                  yAxisId="primary"
                  x1={selection.startT}
                  x2={selection.endT}
                  stroke="none"
                  fill={selection.mode === 'zoom' ? 'var(--color-accent)' : '#94a3b8'}
                  fillOpacity={0.16}
                />
              )}
              {horizontalReference && (
                <ReferenceLine yAxisId="primary" y={horizontalReference.value} stroke="#d97706" strokeDasharray="5 3" label={memoryReferenceLabel(horizontalReference.label)} />
              )}
              {hoverT !== null && (
                <ReferenceLine yAxisId="primary" x={hoverT} stroke={accent} strokeDasharray="3 3" />
              )}
              {/* persistent pins — cycle a small palette so multiple pins read distinctly */}
              {pins.map((p, i) => (
                <ReferenceLine
                  key={`pin-${p.t}`}
                  yAxisId="primary"
                  x={p.t}
                  stroke={PIN_COLORS[i % PIN_COLORS.length]}
                  strokeWidth={1.5}
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      {legendRows.length > 0 && (
        <div className="mpi-metric-legend mt-2 flex flex-wrap gap-x-4 gap-y-1 pl-[148px] font-mono text-[11px] text-muted">
          {legendRows.map((s) => (
            <span key={s.key} className="inline-flex min-w-0 max-w-full items-center gap-1.5">
              <span
                className="inline-block h-1.5 w-1.5 flex-none rounded-full"
                style={{ backgroundColor: s.color }}
              />
              <span className="min-w-0 break-words">{s.label}</span>
              <span className="flex-none text-ink tabular-nums">
                {s.value === null
                  ? '—'
                  : Number.isInteger(s.value)
                    ? s.value
                    : (s.value as number).toFixed(1)}
              </span>
            </span>
          ))}
          {comparisonKey && (
            <span className="inline-flex min-w-0 max-w-full items-center gap-1.5">
              <span className="inline-block w-4 flex-none border-t-2 border-dashed" style={{ borderColor: accent }} />
              <span className="min-w-0 break-words">{comparisonLabel ?? 'Comparison'}</span>
              <span className="flex-none text-ink tabular-nums">
                {comparisonValue === null
                  ? '—'
                  : Number.isInteger(comparisonValue)
                    ? comparisonValue
                    : comparisonValue.toFixed(1)}
              </span>
            </span>
          )}
          {comparisonSecondaryRows.map((series) => (
            <span key={series.key} className="inline-flex min-w-0 max-w-full items-center gap-1.5">
              <span className="inline-block w-4 flex-none border-t-2 border-dashed" style={{ borderColor: series.color }} />
              <span className="min-w-0 break-words">{comparisonLabel ?? 'Comparison'} · {series.label}</span>
              <span className="flex-none text-ink tabular-nums">
                {series.value === null
                  ? '—'
                  : Number.isInteger(series.value)
                    ? series.value
                    : series.value.toFixed(1)}
              </span>
            </span>
          ))}
        </div>
      )}
      </div>
      {helpText && helpPopup && createPortal(
        <span
          id={helpId}
          role="tooltip"
          className="pointer-events-none fixed z-[100] rounded-lg border border-line bg-surface-hi p-2 text-left text-[11px] font-normal normal-case leading-relaxed tracking-normal text-ink shadow-xl"
          style={helpPopup}
        >
          {helpText}
        </span>,
        document.body
      )}
    </>
  )
}
