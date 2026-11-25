import { isAndroidAnonymousMemory, memoryChartLabel } from '../lib/chartMemory'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { CircleHelp } from 'lucide-react'
import { formatMmss, formatValue } from '../lib/format'
import { call, saveFile } from '../lib/ipc'
import { useMessages } from '../lib/i18n'
import type { ChartsMessages } from '../lib/i18n/namespaces/charts'
import type { MetricsSnapshot } from '../lib/types'
import { useCrosshair } from './liveCrosshair'
import { temperatureZoneForSnapshots, type TemperatureZone } from '../lib/thermalMetrics'

interface Props {
  /** Full-resolution chart rows (RowDatum) — parallel to `snapshots`. */
  data: Record<string, number | null>[]
  /** Original snapshots, 1:1 with `data`, used to export the selected range. */
  snapshots: MetricsSnapshot[]
}

/** Metrics summarized over a dragged range, keyed by their RowDatum series. */
function aggregateRows(charts: ChartsMessages, temperatureZone: TemperatureZone, anonymous: boolean) {
  return [
    { key: 'fps', label: 'FPS', unit: '', digits: 0 },
    { key: 'cpu_total', label: charts.systemCpu, unit: '%', digits: 0 },
    { key: 'cpu_app', label: charts.appCpuWholeDevice, unit: '%', digits: 0 },
    { key: 'cpu_norm', label: charts.appCpuNormalized, unit: '%', digits: 0 },
    { key: 'gpu_busy', label: 'GPU', unit: '%', digits: 0 },
    { key: `thermal:${temperatureZone}`, label: temperatureZone === 'skin' ? 'Skin' : charts.batteryTemperature, unit: '°C', digits: 1 },
    anonymous ? { key: 'mem_anon_mib', label: memoryChartLabel, unit: 'MiB', digits: 1 } : { key: 'mem_ratio', label: 'Mem', unit: '%', digits: 1 },
    { key: 'power_mw', label: 'Power', unit: 'mW', digits: 0 },
    { key: 'battery', label: 'Batt', unit: '%', digits: 0 },
  ]
}

interface Agg {
  label: string
  unit: string
  digits: number
  avg: number | null
  min: number | null
  max: number | null
}

export function SummaryOverlay({ data, snapshots }: Props) {
  const t = useMessages()
  const temperatureZone = temperatureZoneForSnapshots(snapshots)
  const anonymous = isAndroidAnonymousMemory(snapshots.at(-1))
  const rows = useMemo(() => aggregateRows(t.charts, temperatureZone, anonymous), [t.charts, temperatureZone, anonymous])
  const { selection, menu, closeMenu } = useCrosshair()
  const [saving, setSaving] = useState(false)

  const range = selection && selection.mode === 'summary' ? selection : null

  // Indices of the rows inside the dragged range (also used for the export subset).
  const inRange = useMemo(() => {
    if (!range) return [] as number[]
    const out: number[] = []
    for (let i = 0; i < data.length; i++) {
      const t = data[i].t as number
      if (typeof t === 'number' && t >= range.startT && t <= range.endT) out.push(i)
    }
    return out
  }, [data, range])

  const aggs = useMemo<Agg[]>(() => {
    return rows.map(({ key, label, unit, digits }) => {
      let sum = 0
      let n = 0
      let min = Infinity
      let max = -Infinity
      for (const i of inRange) {
        const v = data[i][key]
        if (v === null || v === undefined || Number.isNaN(v)) continue
        sum += v
        n += 1
        if (v < min) min = v
        if (v > max) max = v
      }
      return {
        label,
        unit,
        digits,
        avg: n > 0 ? sum / n : null,
        min: n > 0 ? min : null,
        max: n > 0 ? max : null,
      }
    }).filter((a) => a.avg !== null)
  }, [data, inRange, rows])

  // Close the context menu on Escape or an outside click.
  useEffect(() => {
    if (!menu) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeMenu()
    }
    const onDown = () => closeMenu()
    window.addEventListener('keydown', onKey)
    // Defer so the opening contextmenu's own mousedown doesn't immediately close it.
    const id = window.setTimeout(() => window.addEventListener('mousedown', onDown), 0)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.clearTimeout(id)
      window.removeEventListener('mousedown', onDown)
    }
  }, [menu, closeMenu])

  const saveRange = useCallback(async () => {
    if (!range || inRange.length === 0) return
    const subset = inRange
      .filter((i) => i < snapshots.length)
      .map((i) => snapshots[i])
    if (subset.length === 0) return
    const a = formatMmss(range.startT).replace(':', '')
    const b = formatMmss(range.endT).replace(':', '')
    const target = await saveFile({
      defaultPath: `mpi_range_${a}-${b}.csv`,
      filters: [{ name: 'CSV', extensions: ['csv'] }],
    })
    closeMenu()
    if (!target) return
    setSaving(true)
    try {
      await call('export_live_csv', { save_path: target, snapshots: subset })
    } catch {
      /* surfaced elsewhere; keep the overlay quiet */
    } finally {
      setSaving(false)
    }
  }, [range, inRange, snapshots, closeMenu])

  if (!range) return null

  return createPortal(
    <>
      {/* range summary card — out of the way along the bottom, non-interactive */}
      <div className="pointer-events-none fixed bottom-16 left-1/2 z-40 w-[calc(100vw-1rem)] max-w-[46rem] -translate-x-1/2 overflow-hidden rounded-md border border-neutral-200 bg-white/95 px-3 py-2 text-xs shadow-lg backdrop-blur dark:border-neutral-700 dark:bg-neutral-900/95">
        <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 border-b border-neutral-200 pb-1 dark:border-neutral-700">
          <span className="font-mono text-neutral-700 dark:text-neutral-200">
            {formatMmss(range.startT)}–{formatMmss(range.endT)}
          </span>
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">
            {t.summary.rangeSummary} · {inRange.length} samples
          </span>
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_repeat(3,minmax(2.75rem,auto))] gap-x-1 gap-y-0.5 sm:grid-cols-[minmax(11rem,auto)_repeat(3,minmax(3.5rem,1fr))] sm:gap-x-3">
          <span className="text-[10px] uppercase text-neutral-400" />
          <span className="text-right text-[10px] uppercase text-neutral-400">avg</span>
          <span className="text-right text-[10px] uppercase text-neutral-400">min</span>
          <span className="text-right text-[10px] uppercase text-neutral-400">max</span>
          {aggs.map((a) => (
            <Cells key={a.label} agg={a} />
          ))}
        </div>
        <div className="mt-1.5 flex items-start gap-1 text-[10px] leading-relaxed text-neutral-500">
          <CircleHelp size={11} className="mt-0.5 flex-none" aria-hidden="true" />
          <span>{t.charts.cpuNormalizationHelp}</span>
        </div>
        <div className="mt-1 text-[10px] text-neutral-500">{t.summary.saveHint}</div>
      </div>

      {/* right-click "save range" menu */}
      {menu && (
        <div
          className="fixed z-50 min-w-[160px] overflow-hidden rounded-md border border-neutral-200 bg-white text-xs shadow-xl dark:border-neutral-700 dark:bg-neutral-900"
          style={{ left: menu.x, top: menu.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <button
            type="button"
            disabled={saving}
            onClick={saveRange}
            className="block w-full px-3 py-2 text-left text-neutral-800 hover:bg-neutral-100 disabled:opacity-50 dark:text-neutral-100 dark:hover:bg-neutral-800"
          >
            {saving ? t.summary.saving : t.summary.saveRange(inRange.length)}
          </button>
        </div>
      )}
    </>,
    document.body
  )
}

function Cells({ agg }: { agg: Agg }) {
  return (
    <>
      <span className="min-w-0 break-words pr-1 leading-tight text-neutral-500">{agg.label}</span>
      <Num v={agg.avg} digits={agg.digits} unit={agg.unit} />
      <Num v={agg.min} digits={agg.digits} unit={agg.unit} />
      <Num v={agg.max} digits={agg.digits} unit={agg.unit} />
    </>
  )
}

function Num({ v, digits, unit }: { v: number | null; digits: number; unit: string }) {
  return (
    <span className="text-right font-mono text-neutral-900 tabular-nums dark:text-neutral-100">
      {formatValue(v, digits)}
      {unit ? <span className="ml-0.5 text-neutral-400">{unit}</span> : null}
    </span>
  )
}
