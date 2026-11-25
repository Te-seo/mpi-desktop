import { Fragment } from 'react'
import { AlertTriangle } from 'lucide-react'
import { useMessages } from '../../lib/i18n'
import type { MetricStats, SessionAnalysis } from '../../lib/types'
import { coverageLow, fmt, fmtDuration, fmtInt, fmtTimestamp } from './statFormat'
import { METRIC_COLORS } from './sessionColors'
import { sessionMemoryLabels, sessionMemoryType, sessionMemoryUnit, sessionPeakMemory } from './sessionMemory'

/** Single-session analysis: slim identity strip, two-tier KPI grid, and a
 *  unified diagnosis grid. Every card shares one grammar: a metric-colored
 *  tick, an 11px uppercase title, and an optional mono unit chip. The chart
 *  stack is rendered separately below by the parent. */
export function SessionAnalysisView({ analysis }: { analysis: SessionAnalysis }) {
  const t = useMessages()
  const d = t.dashboard
  const m = analysis.metrics
  const memoryType = sessionMemoryType(analysis)
  const anonymousMemory = memoryType === 'anonymous'
  const peakMemory = sessionPeakMemory(analysis)
  const memoryUnit = sessionMemoryUnit(memoryType)
  const memoryStats = anonymousMemory ? m.anon_rss_plus_swap_mib : m.app_memory_mb
  const memoryRatio = anonymousMemory ? null : analysis.memory_ratio_percent
  const appLabel = analysis.app_label?.trim()
  const appName = appLabel || analysis.bundle_id?.trim() || d.unknownApp

  const fps = m.fps
  // Representative coverage for the header chip: FPS drives the primary KPIs.
  const showPartial = coverageLow(fps)

  // Tier 1 = the verdict band (frame health + thermal ceiling); tier 2 = detail.
  const kpisLg: KpiProps[] = [
    { label: d.kpi.avgFps, value: fmt(fps?.average ?? null, 1), unit: d.units.fps, tick: METRIC_COLORS.fps },
    { label: d.kpi.p05Fps, value: fmt(fps?.p05 ?? null, 1), unit: d.units.fps, tick: METRIC_COLORS.fps },
    {
      label: d.kpi.weightedJank,
      value: fmt(analysis.frames.weighted_jank_percent, 2),
      unit: d.units.percent,
      tick: METRIC_COLORS.fps,
    },
    { label: d.kpi.maxTempBattery, value: fmt(analysis.max_temp_battery, 1), unit: d.units.celsius, tick: METRIC_COLORS.temp },
  ]
  const kpisSm: KpiProps[] = [
    { label: d.kpi.medianFps, value: fmt(fps?.median ?? null, 1), unit: d.units.fps, tick: METRIC_COLORS.fps },
    {
      label: d.kpi.hottestSensor,
      value: fmt(analysis.hottest_sensor_max, 1),
      unit: d.units.celsius,
      sub: analysis.hottest_sensor_zone ?? undefined,
      tick: METRIC_COLORS.temp,
    },
    {
      label: anonymousMemory ? `${d.detail.peak} ${sessionMemoryLabels.anonymous}` : d.kpi.peakAppMemory,
      value: fmt(peakMemory, 0),
      unit: memoryUnit,
      sub:
        memoryRatio != null
          ? `${fmt(memoryRatio, 1)}% ${d.detail.ratioOfRam}`
          : undefined,
      tick: METRIC_COLORS.mem,
    },
    { label: d.kpi.avgPower, value: fmt(m.power_mw?.average ?? null, 0), unit: d.units.mw, tick: METRIC_COLORS.power },
  ]

  return (
    <div className="flex flex-col gap-4">
      {/* identity strip */}
      <div className="mpi-panel px-5 py-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="min-w-0 max-w-full break-words text-lg font-semibold tracking-tight text-ink">
            {appName}
          </h2>
          {!appLabel && analysis.bundle_id?.trim() && <span className="text-[10px] text-faint">{d.library.packageId}</span>}
          {analysis.app_version && (
            <span className="text-[12px] text-muted">v{analysis.app_version}</span>
          )}
          {analysis.session_name && (
            <span className="rounded-full border border-line px-2 py-0.5 text-[11px] text-muted">
              {analysis.session_name}
            </span>
          )}
          {analysis.is_legacy && (
            <span className="rounded-full border border-line px-2 py-0.5 text-[10px] uppercase tracking-wide text-faint">
              {d.meta.legacy}
            </span>
          )}
          {showPartial && fps ? (
            <span className="ml-auto inline-flex items-center gap-1.5 rounded-[6px] border border-warn-line bg-warn-soft px-2 py-0.5 text-[11px] text-warn">
              <AlertTriangle size={12} className="flex-none" />
              {d.states.partialData(fps.valid_count, fps.total_count)}
            </span>
          ) : (
            <span className="ml-auto font-mono text-[11px] tabular-nums text-muted">
              {d.meta.coverage} {fps ? `${fmt(fps.coverage_percent, 0)}%` : '—'}
            </span>
          )}
        </div>
        <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 border-t border-line pt-3 text-xs">
          <Meta label={d.meta.platform} value={analysis.platform} />
          <Meta
            label={d.meta.device}
            value={analysis.device_display_name || analysis.device_model}
          />
          {analysis.platform === 'ios' && <>
            <Meta label={t.deviceInfo.labelModel} value={analysis.device_model} />
            <Meta label={t.deviceInfo.labelModelNumber} value={analysis.device_model_number || '—'} mono />
            <Meta label={t.deviceInfo.labelModelIdentifier} value={analysis.device_model_identifier || '—'} mono />
          </>}
          <Meta label={d.meta.os} value={analysis.os_version} />
          <Meta label={t.deviceInfo.labelBuildVersion} value={analysis.device_build_version || '—'} mono />
          <Meta label={t.deviceInfo.labelBuildName} value={analysis.device_build_name || '—'} />
          <Meta label={d.meta.duration} value={fmtDuration(analysis.duration_sec)} mono />
          <Meta label={d.meta.samples} value={fmtInt(analysis.sample_count)} mono />
          <Meta
            label={`${d.meta.started} → ${d.meta.ended}`}
            value={`${fmtTimestamp(analysis.started_at)} → ${fmtTimestamp(analysis.ended_at)}`}
            mono
          />
        </div>
      </div>

      {/* KPI two-tier 4x2 */}
      <div className="grid grid-cols-1 gap-3 min-[480px]:grid-cols-2 xl:grid-cols-4">
        {kpisLg.map((k) => (
          <KpiCard key={k.label} {...k} size="lg" />
        ))}
        {kpisSm.map((k) => (
          <KpiCard key={k.label} {...k} size="sm" />
        ))}
      </div>

      {/* diagnosis grid, ordered to mirror the KPI clusters */}
      <div className="grid grid-cols-1 gap-4 min-[800px]:grid-cols-2 2xl:grid-cols-3">
        <Card title={d.detail.frames} tick={METRIC_COLORS.fps}>
          <KVList
            rows={[
              { label: d.detail.totalFrames, text: fmtInt(analysis.frames.total_frames) },
              { label: d.detail.weightedJank, text: `${fmt(analysis.frames.weighted_jank_percent, 2)} ${d.units.percent}` },
              { label: d.detail.worstWindowP95, text: `${fmt(analysis.frames.worst_window_p95_ms, 1)} ${d.units.ms}` },
              { label: d.detail.worstWindowP99, text: `${fmt(analysis.frames.worst_window_p99_ms, 1)} ${d.units.ms}` },
              { label: d.detail.sessionFrameP50P90, text: `${fmt(analysis.frames.session_p50_ms ?? null, 2)} / ${fmt(analysis.frames.session_p90_ms ?? null, 2)}` },
              { label: d.detail.sessionFrameP95P99, text: `${fmt(analysis.frames.session_p95_ms ?? null, 2)} / ${fmt(analysis.frames.session_p99_ms ?? null, 2)}` },
              { label: d.detail.rawFrameCounts, text: `${fmtInt(analysis.frames.raw_frame_count ?? 0)} / ${fmtInt(analysis.frames.raw_frames_dropped ?? 0)}` },
            ]}
          />
          {!analysis.frames.raw_frame_count && (
            <p className="mt-2 break-words text-xs text-muted">{d.detail.rawFrameUnavailable}</p>
          )}
          {(analysis.frames.raw_frames_dropped ?? 0) > 0 && <p className="mt-1 text-xs text-amber-500">{d.detail.rawFramePartial}</p>}
        </Card>
        <Card title={d.detail.cpu} tick={METRIC_COLORS.cpu} unit={d.units.percent}>
          <StatRows
            headers={[d.detail.average, d.detail.p95, d.detail.max]}
            rows={[
              { label: d.detail.appCpu, stats: m.cpu_app },
              { label: d.detail.systemCpu, stats: m.cpu_system },
              { label: d.detail.normalizedCpu, stats: m.cpu_norm },
            ]}
          />
          <p className="mt-2 border-t border-line/60 pt-2 text-[10px] leading-relaxed text-faint">
            {d.detail.cpuNormalizationHelp}
          </p>
        </Card>
        <Card title={d.detail.gpu} tick={METRIC_COLORS.gpu}>
          <StatRows
            headers={[d.detail.average, d.detail.p95, d.detail.max]}
            rows={[
              { label: d.detail.busy, unit: d.units.percent, stats: m.gpu_busy },
              { label: d.detail.clock, unit: d.units.mhz, digits: 0, cells: [m.gpu_clock?.average ?? null, null, m.gpu_clock?.max ?? null] },
              { label: d.detail.gpuTemp, unit: d.units.celsius, cells: [null, null, m.gpu_temp?.max ?? null] },
            ]}
          />
        </Card>
        <Card title={anonymousMemory ? sessionMemoryLabels.anonymous : d.detail.memory} tick={METRIC_COLORS.mem} unit={memoryUnit}>
          <KVList
            rows={[
              { label: d.detail.peak, text: fmt(peakMemory, 0) },
              { label: d.detail.average, text: fmt(memoryStats?.average ?? null, 0) },
              ...(memoryRatio != null
                ? [{ label: d.detail.ratioOfRam, text: `${fmt(memoryRatio, 1)} ${d.units.percent}` }]
                : []),
            ]}
          />
        </Card>
        <Card title={d.detail.powerBattery} tick={METRIC_COLORS.power}>
          <StatRows
            headers={[d.detail.average, d.detail.p95, d.detail.max]}
            rows={[{ label: d.detail.power, unit: d.units.mw, digits: 0, stats: m.power_mw }]}
          />
          <div className="mt-2 border-t border-line/60 pt-2">
            <KVList
              rows={[
                {
                  label: d.detail.levelChange,
                  text:
                    analysis.battery_start_level != null && analysis.battery_end_level != null
                      ? d.detail.firstLast(
                          Math.round(analysis.battery_start_level),
                          Math.round(analysis.battery_end_level)
                        )
                      : '—',
                },
                { label: d.detail.change, text: `${fmt(analysis.battery_change_pp, 1)} ${d.units.pp}` },
              ]}
            />
          </div>
        </Card>
        {analysis.temp_zones.length > 0 && (
          <Card title={d.detail.temperature} tick={METRIC_COLORS.temp} unit={d.units.celsius}>
            <div className="max-h-[200px] overflow-y-auto">
              <div
                className="grid gap-x-4 gap-y-1 text-[12px]"
                style={{ gridTemplateColumns: 'minmax(0,1.4fr) repeat(4, minmax(0,1fr))' }}
              >
                <span className="sticky top-0 bg-surface text-[10px] uppercase text-faint">{d.detail.zone}</span>
                <span className="sticky top-0 bg-surface text-right text-[10px] uppercase text-faint">{d.detail.start}</span>
                <span className="sticky top-0 bg-surface text-right text-[10px] uppercase text-faint">{d.detail.end}</span>
                <span className="sticky top-0 bg-surface text-right text-[10px] uppercase text-faint">{d.detail.max}</span>
                <span className="sticky top-0 bg-surface text-right text-[10px] uppercase text-faint">{d.detail.change}</span>
                {analysis.temp_zones.map((z) => (
                  <Fragment key={z.zone}>
                    <span className="truncate text-muted">{z.zone}</span>
                    <span className="text-right font-mono tabular-nums text-ink">{fmt(z.start, 1)}</span>
                    <span className="text-right font-mono tabular-nums text-ink">{fmt(z.end, 1)}</span>
                    <span className="text-right font-mono tabular-nums text-ink">{fmt(z.max, 1)}</span>
                    <span className="text-right font-mono tabular-nums text-ink">{fmt(z.change, 1)}</span>
                  </Fragment>
                ))}
              </div>
            </div>
          </Card>
        )}
      </div>
    </div>
  )
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <span className="inline-flex min-w-0 flex-wrap items-baseline gap-x-1.5 gap-y-1">
      <span className="flex-none text-[10px] uppercase tracking-wide text-faint">{label}</span>
      <span className={['break-words text-ink', mono ? 'font-mono tabular-nums' : ''].join(' ')} title={value}>
        {value}
      </span>
    </span>
  )
}

interface KpiProps {
  label: string
  value: string
  unit?: string
  sub?: string
  tick: string
}

function KpiCard({ label, value, unit, sub, tick, size }: KpiProps & { size: 'lg' | 'sm' }) {
  return (
    <div className={['mpi-panel relative min-w-0 px-5', size === 'lg' ? 'py-5' : 'py-4'].join(' ')}>
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden="true" className="h-2 w-2 flex-none rounded-full" style={{ background: tick }} />
        <span className="text-xs font-medium leading-relaxed text-muted">{label}</span>
        {unit && (
          <span className="ms-auto flex-none font-mono text-xs text-muted">
            {unit}
          </span>
        )}
      </div>
      <div className={['mt-3 break-words font-mono font-semibold leading-tight tabular-nums tracking-tight text-ink', size === 'lg' ? 'text-3xl' : 'text-2xl'].join(' ')}>
        {value}
      </div>
      {sub && (
        <div className="mt-2 break-words text-xs leading-relaxed text-muted" title={sub}>
          {sub}
        </div>
      )}
    </div>
  )
}

/** Shared card shell: metric tick + uppercase title + optional mono unit chip. */
function Card({ title, tick, unit, children }: { title: string; tick: string; unit?: string; children: React.ReactNode }) {
  return (
    <div className="mpi-panel min-w-0 p-4">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span aria-hidden="true" className="h-3.5 w-[3px] flex-none rounded-full" style={{ background: tick }} />
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {unit && (
          <span className="ml-auto flex-none rounded-[5px] border border-line bg-bg px-1 py-px font-mono text-[10px] text-faint">
            {unit}
          </span>
        )}
      </div>
      {children}
    </div>
  )
}

type StatKey = keyof Pick<MetricStats, 'average' | 'median' | 'min' | 'max' | 'p05' | 'p95' | 'p99' | 'stddev'>

/** Aligned avg/p95/max rows. A row either maps `stats` over the default keys or
 *  supplies `cells` aligned 1:1 with the headers (null renders as an em dash),
 *  so sparse metrics like GPU clock never left-pack under the wrong header. */
function StatRows({
  headers,
  rows,
}: {
  headers: string[]
  rows: { label: string; unit?: string; digits?: number; stats?: MetricStats; cells?: (number | null)[] }[]
}) {
  const defaultCols: StatKey[] = ['average', 'p95', 'max']
  return (
    <div
      className="grid gap-x-2 gap-y-2"
      style={{ gridTemplateColumns: `minmax(0,1.3fr) repeat(${headers.length}, minmax(0,1fr))` }}
    >
      <span />
      {headers.map((h) => (
        <span key={h} className="text-right text-[10px] uppercase text-faint">
          {h}
        </span>
      ))}
      {rows.map((r) => {
        const digits = r.digits ?? 1
        const cells = r.cells ?? defaultCols.map((c) => r.stats?.[c] ?? null)
        return (
          <Fragment key={r.label}>
            <span className="break-words text-xs leading-relaxed text-muted" title={r.label}>
              {r.label}
              {r.unit && <span className="text-faint"> {r.unit}</span>}
            </span>
            {cells.map((v, i) => (
              <span key={i} className="break-words text-right font-mono text-xs tabular-nums text-ink">
                {fmt(v, digits)}
              </span>
            ))}
          </Fragment>
        )
      })}
    </div>
  )
}

/** Right-aligned key-value rows sharing the StatRows text rhythm. */
function KVList({ rows }: { rows: { label: string; text: string }[] }) {
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((r) => (
        <div key={r.label} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-xs leading-relaxed">
          <span className="text-muted">{r.label}</span>
          <span className="break-words font-mono tabular-nums text-ink">{r.text}</span>
        </div>
      ))}
    </div>
  )
}
