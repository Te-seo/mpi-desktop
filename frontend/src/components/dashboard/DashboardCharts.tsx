import { memoryReferenceLabel } from '../MemoryReferenceLabel'
import { useEffect, useMemo, useState } from 'react'
import { thermalSeriesForZone } from '../../lib/thermalMetrics'
import { CircleHelp } from 'lucide-react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { useLocale, useMessages } from '../../lib/i18n'
import { MEMORY_SOURCE, memoryChartLabel, playReference, type MemoryCategory } from '../../lib/chartMemory'
import type { SessionAnalysis } from '../../lib/types'
import { fmt } from './statFormat'
import {
  METRIC_COLORS,
  SESSION_COLORS,
  SESSION_DASH,
  SESSION_LABELS,
  type DashboardMetricKey,
} from './sessionColors'
import { downsample, extentOf, resolveXMax, sampledValueAt, type Pt } from './chartMath'

interface ChartSeriesDef {
  key: DashboardMetricKey
  label: string
  color: string
  /** Time-series key in SessionAnalysis.timeseries, or a resolver for temp. */
  values: (a: SessionAnalysis) => (number | null)[]
}

interface ChartDef {
  key: DashboardMetricKey
  label: string
  unit: string
  yMin?: number
  yMax?: number
  series: ChartSeriesDef[]
  digits: number
  note?: string
  horizontalReference?: { value: number; label: string }
  memoryType?: 'anonymous' | 'legacy' | 'ios'
}

/** The 8 full-width dashboard charts, shared by single-session and comparison.
 *  Each metric has a fixed colour; sessions use stable A-D labels and line
 *  patterns, so comparisons never rely on colour alone. Series are
 *  0-aligned (SessionAnalysis timeseries `t` already starts at 0) and never
 *  interpolated. Y range is shared across sessions per chart. */
export function DashboardCharts({
  analyses,
  selectedPaths,
}: {
  analyses: SessionAnalysis[]
  /** Selection order; keeps each session's A–D color/label stable even when the
   *  analyses array was compacted by a failed load. */
  selectedPaths?: string[]
}) {
  const t = useMessages()
  const c = t.dashboard.charts
  const { locale } = useLocale()
  const ko = locale === 'ko'
  const memoryType = analyses.some(a => a.platform === 'android' && a.memory_source === MEMORY_SOURCE)
    ? 'anonymous' : analyses.length && analyses.every(a => a.platform === 'ios') ? 'ios' : 'legacy'
  const single = analyses.length === 1 ? analyses[0] : undefined
  const context = single?.memory_reference_context
  const observed = context?.play_memory_category
  const [manual, setManual] = useState<{ file: string; type?: MemoryCategory }>()
  const chosen = observed === 'app' || observed === 'game' ? observed : manual?.file === single?.file_path ? manual?.type : undefined
  const ref = memoryType === 'anonymous' && context && chosen ? playReference(context, chosen) : null
  const memoryControl = single && memoryType === 'anonymous' && observed !== 'app' && observed !== 'game' ? (
    <select aria-label={ko ? '앱 유형' : 'App category'} value={chosen ?? ''}
      onChange={e => {
        const value = e.currentTarget.value
        setManual({ file: single.file_path, type: value === 'app' || value === 'game' ? value : undefined })
      }}
      className="rounded border border-line bg-bg px-1 text-xs text-ink">
      <option value="">{ko ? '유형 선택' : 'Choose type'}</option>
      <option value="app">{ko ? '앱' : 'App'}</option>
      <option value="game">{ko ? '게임' : 'Game'}</option>
    </select>
  ) : undefined
  const cpuNormalizationHelp = t.dashboard.detail.cpuNormalizationHelp
  const multi = (selectedPaths?.length ?? analyses.length) > 1
  const [commonRange, setCommonRange] = useState(false)
  const [hidden, setHidden] = useState<Set<string>>(new Set())

  // Stable A–D color/label slot for each analysis by its position in the
  // selection (falling back to array position when selectedPaths is absent).
  const slots = useMemo(
    () =>
      analyses.map((a, i) => {
        const s = selectedPaths ? selectedPaths.indexOf(a.file_path) : -1
        return s >= 0 ? s : i
      }),
    [analyses, selectedPaths]
  )

  const charts: ChartDef[] = useMemo(() => {
    const ts = (key: string) => (a: SessionAnalysis) => a.timeseries[key] ?? []
    const metricSeries = (
      key: DashboardMetricKey,
      label: string,
      values: ChartSeriesDef['values']
    ): ChartSeriesDef => ({ key, label, color: METRIC_COLORS[key], values })
    const battzone = (a: SessionAnalysis): (number | null)[] => {
      const zone = a.battery_zone ?? 'battery'
      return thermalSeriesForZone(a.timeseries, zone)
    }
    return [
      { key: 'fps', label: c.fps, unit: t.dashboard.units.fps, yMin: 0, series: [metricSeries('fps', c.fps, ts('fps'))], digits: 1 },
      { key: 'temp', label: c.temperature, unit: t.dashboard.units.celsius, series: [metricSeries('temp', c.temperature, battzone)], digits: 1 },
      {
        key: 'cpu',
        label: c.cpuUsage,
        unit: t.dashboard.units.percent,
        yMin: 0,
        yMax: 100,
        series: [
          metricSeries('cpu', c.cpu, ts('cpu_system')),
          metricSeries('cpu_app', c.cpuApp, ts('cpu_app')),
        ],
        digits: 1,
      },
      {
        key: 'cpu_norm',
        label: c.cpuNorm,
        unit: t.dashboard.units.percent,
        yMin: 0,
        yMax: 100,
        series: [metricSeries('cpu_norm', c.cpuNorm, ts('cpu_norm'))],
        digits: 1,
        note: cpuNormalizationHelp,
      },
      { key: 'gpu', label: c.gpu, unit: t.dashboard.units.percent, yMin: 0, series: [metricSeries('gpu', c.gpu, ts('gpu_busy'))], digits: 1 },
      {
        key: 'mem', memoryType,
        label: memoryType === 'anonymous' ? memoryChartLabel : memoryType === 'ios' ? 'iOS app memory' : 'Legacy PSS / RSS',
        unit: memoryType === 'anonymous' ? 'MiB' : t.dashboard.units.mb,
        yMin: 0,
        series: [metricSeries('mem', memoryType === 'anonymous' ? memoryChartLabel : c.memory,
          memoryType === 'anonymous' ? ts('anon_rss_plus_swap_mib') : ts('app_memory_mb'))], digits: 0,
        horizontalReference: ref ? {
          value: ref.mib,
          label: `${ko ? 'Play 로컬 참고' : 'Play local reference'} · ${ref.tier} · ${ref.state} · ${ref.mib} MiB`,
        } : undefined,
      },
      { key: 'power', label: c.power, unit: t.dashboard.units.mw, yMin: 0, series: [metricSeries('power', c.power, ts('power_mw'))], digits: 0 },
      { key: 'battery', label: c.battery, unit: t.dashboard.units.percent, yMin: 0, series: [metricSeries('battery', c.battery, ts('battery_level'))], digits: 0 },
    ]
  }, [c, cpuNormalizationHelp, t.dashboard.units, memoryType, ref, ko])

  // Common range = shortest session's last timestamp; full = longest. Sessions
  // with no samples are ignored so the toggle isn't collapsed to a no-op.
  const xMax = useMemo(
    () => resolveXMax(analyses.map((a) => a.timeseries.t ?? []), commonRange),
    [analyses, commonRange]
  )

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="text-[13px] font-semibold text-ink">{c.title}</span>
        <div className="flex items-center gap-3">
          {multi && (
            <label className="flex items-center gap-1.5 text-[12px] text-muted">
              <input
                type="checkbox"
                checked={commonRange}
                onChange={(e) => setCommonRange(e.target.checked)}
                className="accent-[var(--color-accent)]"
              />
              {c.commonRangeOnly}
            </label>
          )}
          {multi && (
            <div className="flex items-center gap-1">
              {analyses.map((a, i) => (
                <button
                  key={a.file_path + i}
                  type="button"
                  onClick={() =>
                    setHidden((prev) => {
                      const next = new Set(prev)
                      if (next.has(a.file_path)) next.delete(a.file_path)
                      else next.add(a.file_path)
                      return next
                    })
                  }
                  className="flex items-center gap-1 rounded-[6px] border px-1.5 py-0.5 text-[11px]"
                  style={{
                    borderColor: 'var(--color-line)',
                    color: hidden.has(a.file_path)
                      ? 'var(--color-faint)'
                      : SESSION_COLORS[slots[i]],
                    opacity: hidden.has(a.file_path) ? 0.5 : 1,
                  }}
                >
                  <span className="font-bold">{SESSION_LABELS[slots[i]]}</span>
                  <svg width="18" height="5" aria-hidden="true">
                    <line
                      x1="0"
                      y1="2.5"
                      x2="18"
                      y2="2.5"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeDasharray={SESSION_DASH[slots[i]]}
                    />
                  </svg>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {charts.map((def) => (
        <ChartCard
          key={def.key}
          def={def}
          analyses={analyses}
          slots={slots}
          hidden={hidden}
          multi={multi}
          xMax={xMax}
          secondsLabel={c.seconds}
          headerControl={def.key === 'mem' ? memoryControl : undefined}
        />
      ))}
    </div>
  )
}

function ChartCard({
  def,
  analyses,
  slots,
  hidden,
  multi,
  xMax,
  secondsLabel,
  headerControl,
}: {
  def: ChartDef
  analyses: SessionAnalysis[]
  slots: number[]
  hidden: Set<string>
  multi: boolean
  xMax: number
  secondsLabel: (s: number) => string
  headerControl?: React.ReactNode
}) {
  const [hovering, setHovering] = useState(false)
  useEffect(() => {
    const clear = () => setHovering(false)
    window.addEventListener('blur', clear)
    window.addEventListener('scroll', clear, true)
    document.addEventListener('visibilitychange', clear)
    return () => {
      window.removeEventListener('blur', clear)
      window.removeEventListener('scroll', clear, true)
      document.removeEventListener('visibilitychange', clear)
    }
  }, [])

  // One downsampled {t,v}[] per session, plus the shared Y domain. Rows past
  // xMax are dropped so the "common range only" toggle actually crops the lines
  // (Recharts would otherwise expand the numeric X domain back out to fit the
  // longer session's overflowing points).
  const { lines, yDomain } = useMemo(() => {
    const full = analyses.flatMap((a, i) =>
      def.series.map((series) => {
        const times = a.timeseries.t ?? []
        const vals = matchesMemorySource(def.memoryType, a) ? series.values(a) : []
        const pts: Pt[] = []
        for (let k = 0; k < times.length; k++) {
          const tv = times[k]
          if (tv == null || !Number.isFinite(tv)) continue
          if (xMax > 0 && tv > xMax) continue
          const raw = vals[k]
          pts.push({ t: tv, v: raw != null && Number.isFinite(raw) ? raw : null })
        }
        return { idx: i, series, pts }
      })
    )
    const ext = extentOf(full.map((f) => f.pts))
    const loBase = def.yMin ?? (ext ? ext.min : 0)
    const lo = ext ? Math.min(loBase, ext.min) : loBase
    const hiRaw = Math.max(def.yMax ?? -Infinity, ext ? ext.max : lo + 1, def.horizontalReference ? def.horizontalReference.value * 1.32 : -Infinity)
    const hi = hiRaw > lo ? hiRaw : lo + 1
    const lines = full.map((f) => ({ idx: f.idx, series: f.series, rows: downsample(f.pts) }))
    return { lines, yDomain: [Math.floor(lo), Math.ceil(hi)] as [number, number] }
  }, [analyses, def, xMax])

  const visible = lines.filter(
    (line) => !hidden.has(analyses[line.idx].file_path)
  )
  const hasData = visible.some((l) => l.rows.some((p) => p.v != null))

  return (
    <div className="mpi-panel min-w-0 p-4">
      <div className="mb-1 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className="text-sm font-semibold text-ink">{def.label}</span>
          {def.series.length > 1 && (
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              {def.series.map((series) => (
                <span key={series.key} className="inline-flex items-center gap-1">
                  {multi && def.key === 'cpu' ? (
                    <svg width="16" height="5" aria-hidden="true">
                      <line
                        x1="0"
                        y1="2.5"
                        x2="16"
                        y2="2.5"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeDasharray={
                          series.key === 'cpu_app' ? '5 3' : undefined
                        }
                      />
                    </svg>
                  ) : (
                    <span
                      className="h-1.5 w-1.5 rounded-full"
                      style={{ backgroundColor: series.color }}
                      aria-hidden="true"
                    />
                  )}
                  {series.label}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">{headerControl}<span className="text-xs text-muted">{def.unit}</span></div>
      </div>
      {hasData || def.horizontalReference ? (
        <div
          data-testid={`analysis-plot-${def.key}`}
          onMouseLeave={() => setHovering(false)}
          onPointerLeave={() => setHovering(false)}
          onPointerCancel={() => setHovering(false)}
        >
        <ResponsiveContainer width="100%" height={150}>
          <LineChart
            margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
            onMouseMove={(state) => setHovering(typeof state.activeLabel === 'number' && Number.isFinite(state.activeLabel))}
            onMouseLeave={() => setHovering(false)}
          >
            <CartesianGrid stroke="var(--color-grid)" strokeDasharray="2 3" vertical={false} />
            <XAxis
              type="number"
              dataKey="t"
              domain={[0, xMax > 0 ? xMax : 'dataMax']}
              tickFormatter={(s: number) => secondsLabel(Math.round(s))}
              tick={{ fontSize: 10, fill: 'var(--color-faint)' }}
              allowDecimals={false}
              stroke="var(--color-line)"
            />
            <YAxis
              domain={yDomain}
              width={38}
              tick={{ fontSize: 10, fill: 'var(--color-faint)' }}
              stroke="var(--color-line)"
            />
            {def.horizontalReference && <ReferenceLine y={def.horizontalReference.value} stroke="#d97706" strokeDasharray="5 3" label={memoryReferenceLabel(def.horizontalReference.label)} />}
            <Tooltip
              active={hovering ? undefined : false}
              wrapperStyle={{ pointerEvents: 'none' }}
              cursor={{ stroke: 'var(--color-faint)', strokeDasharray: '3 3' }}
              content={(props) => {
                const payload = props.payload as unknown as
                  | ReadonlyArray<{ payload?: { t?: number } }>
                  | undefined
                if (!hovering || !props.active || !payload || payload.length === 0) return null
                const first = payload[0]?.payload
                const tSec = first && typeof first.t === 'number' ? first.t : 0
                return (
                  <div className="rounded-md border border-line bg-surface/95 p-2 text-[11px] shadow-lg">
                    <div className="mb-1 font-mono text-muted">{secondsLabel(Math.round(tSec))}</div>
                    <div className="flex flex-col gap-0.5">
                      {analyses.map((a, i) => {
                        if (hidden.has(a.file_path) || !matchesMemorySource(def.memoryType, a)) return null
                        return def.series.map((series) => {
                          const v = sampledValueAt(
                            a.timeseries.t ?? [],
                            series.values(a),
                            tSec
                          )
                          return (
                            <div key={`${a.file_path}-${i}-${series.key}`} className="flex items-center gap-2">
                              <svg width="20" height="6" className="flex-none" aria-hidden="true">
                                <line
                                  x1="0"
                                  y1="3"
                                  x2="20"
                                  y2="3"
                                  stroke={resolveLineColor(series, slots[i], multi)}
                                  strokeWidth="2"
                                  strokeDasharray={resolveLineDash(
                                    def,
                                    series,
                                    slots[i],
                                    multi
                                  )}
                                />
                              </svg>
                              <span className="text-faint">
                                {SESSION_LABELS[slots[i]]} · {series.label}
                              </span>
                              <span className="ml-auto font-mono tabular-nums text-ink">
                                {fmt(v, def.digits)} {def.unit}
                              </span>
                            </div>
                          )
                        })
                      })}
                    </div>
                  </div>
                )
              }}
            />
            {visible.map((l) => (
              <Line
                key={`${l.idx}-${l.series.key}`}
                data={l.rows}
                dataKey="v"
                name={`${SESSION_LABELS[slots[l.idx]]} · ${l.series.label}`}
                stroke={resolveLineColor(l.series, slots[l.idx], multi)}
                strokeDasharray={resolveLineDash(
                  def,
                  l.series,
                  slots[l.idx],
                  multi
                )}
                strokeWidth={1.6}
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
        </div>
      ) : (
        <div className="flex h-[120px] items-center justify-center text-[12px] text-faint">—</div>
      )}
      {def.note && (
        <div className="mt-1.5 flex items-start gap-1 text-[10px] leading-relaxed text-faint">
          <CircleHelp size={11} className="mt-0.5 flex-none" aria-hidden="true" />
          <span>{def.note}</span>
        </div>
      )}
    </div>
  )
}

function resolveLineColor(
  series: ChartSeriesDef,
  slot: number,
  multi: boolean
): string {
  return multi ? SESSION_COLORS[slot] ?? SESSION_COLORS[0] : series.color
}

function resolveLineDash(
  def: ChartDef,
  series: ChartSeriesDef,
  slot: number,
  multi: boolean
): string | undefined {
  if (!multi) return undefined
  if (def.key === 'cpu') return series.key === 'cpu_app' ? '5 3' : undefined
  return SESSION_DASH[slot]
}

function matchesMemorySource(type: ChartDef['memoryType'], analysis: SessionAnalysis): boolean {
  if (type === 'anonymous') return analysis.platform === 'android' && analysis.memory_source === MEMORY_SOURCE
  if (type === 'ios') return analysis.platform === 'ios'
  if (type === 'legacy') return analysis.platform !== 'ios' && analysis.memory_source !== MEMORY_SOURCE
  return true
}
