import { useState } from 'react'
import { AlertTriangle, CircleHelp } from 'lucide-react'
import { useMessages } from '../../lib/i18n'
import type { SessionAnalysis } from '../../lib/types'
import { fmt, fmtDuration } from './statFormat'
import { METRIC_COLORS, SESSION_COLORS, SESSION_LABELS } from './sessionColors'
import { deltaParts, mismatchDims, type MismatchDim } from './compareMath'
import { sessionMemoryLabels, sessionMemoryType, sessionMemoryUnit, sessionPeakMemory } from './sessionMemory'

interface Row {
  label: string
  unit: string
  digits: number
  get: (a: SessionAnalysis) => number | null
}

/** A metric-colored group of comparison rows (frame / thermal / resource). */
interface RowGroup {
  tick: string
  rows: Row[]
}

/** Session comparison: mismatch warnings + a baseline-relative KPI table. The
 *  first selected session is baseline A by default; any column can be promoted.
 *  Deltas are neutral (sign + unit only), never scored green/red. */
export function ComparisonView({
  analyses,
  selectedPaths,
}: {
  analyses: SessionAnalysis[]
  /** Library selection order (may be longer than analyses when a session failed
   *  to load). Used to keep each session's A–D color/label identity stable. */
  selectedPaths?: string[]
}) {
  const t = useMessages()
  const d = t.dashboard
  const [baselineIdx, setBaselineIdx] = useState(0)
  // Clamp against the current column count so a stale index (after the selection
  // shrinks) can never leave the table baseline-less or delta a column vs itself.
  const baseIdx = Math.min(baselineIdx, Math.max(0, analyses.length - 1))
  const base = analyses[baseIdx]
  // Color + label slot follows the selection order, so a session keeps its A–D
  // identity even when an earlier-selected session failed to load and the
  // analyses array was compacted by the backend.
  const slotOf = (a: SessionAnalysis) => {
    const s = selectedPaths ? selectedPaths.indexOf(a.file_path) : -1
    return s >= 0 ? s : analyses.indexOf(a)
  }

  const memoryTypes = [...new Set(analyses.map(sessionMemoryType))]
  const memoryRows: Row[] = memoryTypes.map(type => ({
    label: type === 'anonymous' || memoryTypes.length > 1 ? `${d.detail.peak} ${sessionMemoryLabels[type]}` : d.kpi.peakAppMemory,
    unit: sessionMemoryUnit(type),
    digits: 0,
    get: (a) => sessionMemoryType(a) === type ? sessionPeakMemory(a) : null,
  }))

  const groups: RowGroup[] = [
    {
      tick: METRIC_COLORS.fps,
      rows: [
        { label: d.kpi.avgFps, unit: d.units.fps, digits: 1, get: (a) => a.metrics.fps?.average ?? null },
        { label: d.kpi.medianFps, unit: d.units.fps, digits: 1, get: (a) => a.metrics.fps?.median ?? null },
        { label: d.kpi.p05Fps, unit: d.units.fps, digits: 1, get: (a) => a.metrics.fps?.p05 ?? null },
        { label: d.kpi.weightedJank, unit: d.units.percent, digits: 2, get: (a) => a.frames.weighted_jank_percent },
      ],
    },
    {
      tick: METRIC_COLORS.temp,
      rows: [
        { label: d.kpi.maxTempBattery, unit: d.units.celsius, digits: 1, get: (a) => a.max_temp_battery },
        { label: d.kpi.hottestSensor, unit: d.units.celsius, digits: 1, get: (a) => a.hottest_sensor_max },
      ],
    },
    {
      tick: METRIC_COLORS.cpu,
      rows: [
        { label: d.detail.appCpu, unit: d.units.percent, digits: 1, get: (a) => a.metrics.cpu_app?.average ?? null },
        { label: d.detail.systemCpu, unit: d.units.percent, digits: 1, get: (a) => a.metrics.cpu_system?.average ?? null },
        { label: d.detail.normalizedCpu, unit: d.units.percent, digits: 1, get: (a) => a.metrics.cpu_norm?.average ?? null },
        { label: d.detail.busy, unit: d.units.percent, digits: 1, get: (a) => a.metrics.gpu_busy?.average ?? null },
        ...memoryRows,
        { label: d.kpi.avgPower, unit: d.units.mw, digits: 0, get: (a) => a.metrics.power_mw?.average ?? null },
      ],
    },
  ]

  const mismatches = collectMismatches(analyses, d)

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[15px] font-semibold text-ink">{d.compare.title}</span>
        <span className="text-[12px] text-muted">({analyses.length})</span>
        {mismatches.length > 0 && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-warn-line bg-warn-soft px-2.5 py-1 text-[11px] text-warn">
            <AlertTriangle size={12} />
            {d.compare.mismatchWarning}: {mismatches.join(', ')}
          </span>
        )}
      </div>

      {/* comparison table card */}
      <div className="mpi-panel min-w-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr>
                <th className="sticky left-0 bg-surface px-3 py-2 text-left text-[11px] font-medium uppercase text-faint">
                  {d.compare.metric}
                </th>
                {analyses.map((a, i) => (
                  <th
                    key={a.file_path + i}
                    className="min-w-[170px] px-3 pb-1.5 pt-2 text-left align-bottom"
                    style={{ boxShadow: `inset 0 2px 0 ${SESSION_COLORS[slotOf(a)]}` }}
                  >
                    <div className="flex items-center gap-1.5">
                      <span
                        className="flex h-4 w-4 flex-none items-center justify-center rounded-[5px] text-[10px] font-bold text-bg"
                        style={{ background: SESSION_COLORS[slotOf(a)] }}
                      >
                        {SESSION_LABELS[slotOf(a)]}
                      </span>
                      <span className="text-[12px] font-medium text-ink" title={a.app_label?.trim() || a.bundle_id?.trim() || d.unknownApp}>
                        {a.app_label?.trim() || a.bundle_id?.trim() || d.unknownApp}
                      </span>
                      {!a.app_label?.trim() && a.bundle_id?.trim() && <span className="flex-none text-[9px] font-normal text-faint">{d.library.packageId}</span>}
                    </div>
                    <div className="mt-0.5 text-[10px] font-normal leading-snug text-faint">
                      {`${a.device_display_name || a.device_model} · ${a.platform.toUpperCase()} · ${fmtDuration(a.duration_sec)}`}
                    </div>
                    <button
                      type="button"
                      onClick={() => setBaselineIdx(i)}
                      className={[
                        'mt-1 rounded-full border px-2 py-0.5 text-[10px] transition-colors',
                        i === baseIdx
                          ? 'border-accent-line bg-accent-soft font-medium text-accent'
                          : 'border-transparent text-faint hover:border-line hover:text-ink',
                      ].join(' ')}
                    >
                      {i === baseIdx ? d.compare.baseline : d.compare.setBaseline}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            {groups.map((group, gi) => (
              <tbody key={gi}>
                {group.rows.map((row, ri) => {
                  const baseVal = row.get(base)
                  return (
                    <tr key={row.label} className={ri === 0 ? 'border-t-2 border-line' : 'border-t border-line'}>
                      <td
                        className="sticky left-0 bg-surface px-3 py-2 text-muted"
                        style={{ boxShadow: `inset 2px 0 0 ${group.tick}` }}
                      >
                        {row.label} <span className="text-faint">{row.unit}</span>
                      </td>
                      {analyses.map((a, i) => {
                        const v = row.get(a)
                        const isBase = i === baseIdx
                        return (
                          <td key={a.file_path + i} className={['px-3 py-2 text-center align-top', isBase ? 'bg-hover/40' : ''].join(' ')}>
                            <div className="font-mono tabular-nums text-ink">{fmt(v, row.digits)}</div>
                            {!isBase && (
                              <div className="mt-0.5 font-mono text-[10px] tabular-nums text-faint">
                                {deltaText(baseVal, v, row.digits)}
                              </div>
                            )}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            ))}
          </table>
        </div>
        <div className="flex items-start gap-1.5 border-t border-line px-3 py-2 text-[11px] leading-relaxed text-muted">
          <CircleHelp size={13} className="mt-0.5 flex-none text-faint" aria-hidden="true" />
          <span>{d.detail.cpuNormalizationHelp}</span>
        </div>
      </div>
    </div>
  )
}

/** Neutral delta: "±abs (±pct%)". Percent is "—" when the baseline is 0 or
 *  either value is unavailable. */
function deltaText(base: number | null, cmp: number | null, digits: number): string {
  const { abs, pct } = deltaParts(base, cmp)
  if (abs === null) return '—'
  const absStr = (abs >= 0 ? '+' : '') + fmt(abs, digits)
  if (pct === null) return absStr
  const pctStr = (pct >= 0 ? '+' : '') + fmt(pct, 1)
  return `${absStr} (${pctStr}%)`
}

function collectMismatches(
  analyses: SessionAnalysis[],
  d: ReturnType<typeof useMessages>['dashboard']
): string[] {
  const labels: Record<MismatchDim, string> = {
    platform: d.compare.mismatchPlatform,
    device: d.compare.mismatchDevice,
    app: d.compare.mismatchApp,
    version: d.compare.mismatchVersion,
  }
  return mismatchDims(analyses).map((k) => labels[k])
}
