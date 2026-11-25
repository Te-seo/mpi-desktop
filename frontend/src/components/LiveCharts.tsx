import { anonymousMemoryMib, currentMemoryReference, isAndroidAnonymousMemory, memoryChartLabel, observedCategory, type MemoryCategory } from '../lib/chartMemory'
import { snapshotElapsed } from '../lib/sessionCuts'
import { useMemo, useState, type ComponentProps } from 'react'
import { GripVertical } from 'lucide-react'
import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { HoverOverlay } from './HoverOverlay'
import { LiveCrosshairProvider } from './LiveCrosshairContext'
import { MetricCard } from './MetricCard'
import { SummaryOverlay } from './SummaryOverlay'
import {
  deriveFrameQualitySeries,
  isSeriesVisible,
  localizedLiveMetrics,
  metricPrimaryKeys,
  type LiveMetricDef,
  type LiveMetricKey,
  type MetricSeriesDef,
  type MetricVisibility,
} from '../lib/liveMetrics'
import { deriveCpuDisplayMetrics, stableLogicalCpuCount } from '../lib/cpuMetrics'
import type { MetricsSnapshot } from '../lib/types'
import { useLocale, useMessages } from '../lib/i18n'
import type { ChartsMessages } from '../lib/i18n/namespaces/charts'
import { metricStatusLabel } from '../lib/metricProvenance'
import { measuredSkinTemperature, normalizeThermalZones, temperatureZoneForSnapshots } from '../lib/thermalMetrics'

interface Props {
  snapshots: MetricsSnapshot[]
  comparisonSnapshots?: MetricsSnapshot[]
  visibility: MetricVisibility
  /** Display order of the metric cards (all keys; visibility filters per render). */
  order: LiveMetricKey[]
  onReorder(order: LiveMetricKey[]): void
}

const CPU_COLORS = [
  '#3b82f6',
  '#8b5cf6',
  '#ec4899',
  '#ef4444',
  '#f59e0b',
  '#10b981',
  '#06b6d4',
  '#6366f1',
]

const THERMAL_COLORS: Record<string, string> = {
  cpu_max: '#dc2626',
  gpu_max: '#ea580c',
  battery: '#7c3aed',
  skin: '#0ea5e9',
  ddr: '#16a34a',
}

const FALLBACK_THERMAL_COLORS = ['#ef4444', '#f97316', '#eab308', '#22c55e', '#06b6d4', '#8b5cf6']

interface RowDatum {
  t: number
  fps: number | null
  fps_1pct_low: number | null
  stutter_percent: number | null
  smooth_percent: number | null
  small_jank: number | null
  big_jank: number | null
  cpu_app: number | null
  cpu_total: number | null
  cpu_norm: number | null
  mem_total_mb: number | null
  mem_ratio: number | null
  battery: number | null
  battery_ma: number | null
  voltage_mv: number | null
  power_mw: number | null
  gpu_busy: number | null
  gpu_clock: number | null
  [key: string]: number | null // dynamic cpu0..cpuN, thermal:<zone>
}

/* Pure comparison helpers are exported for focused alignment tests; keeping
 * them beside the component also keeps the chart's row contract private. */
/* eslint-disable react-refresh/only-export-components */

/** Convert a saved session to the same elapsed-time coordinate and headline
 * keys used by live cards. Exported for focused duration-alignment tests. */
export function buildComparisonRows(snapshots: MetricsSnapshot[]): RowDatum[] {
  if (!snapshots.length) return []
  const logicalCpuCount = stableLogicalCpuCount(snapshots)
  const temperatureZone = temperatureZoneForSnapshots(snapshots)
  return snapshots.map((s, index) => {
    const appMemKb = s.memory.pss_total_kb || s.memory.rss_total_kb || 0
    const sysTotalKb = s.memory.sys_total_kb || 0
    const vMv = s.battery.voltage_mv ?? null
    const iMa = s.battery.current_ma ?? null
    const cpu = deriveCpuDisplayMetrics(s.cpu, s.device.platform, logicalCpuCount)
    const frameQuality = deriveFrameQualitySeries(s.frames)
    const row: RowDatum = {
      t: Math.round(snapshotElapsed(snapshots, index)),
      fps: s.frames.fps_estimate ?? null,
      ...frameQuality,
      small_jank: s.frames.small_jank ?? null,
      big_jank: s.frames.big_jank ?? null,
      cpu_app: cpu.appWholeDevicePercent,
      cpu_total: cpu.systemPercent,
      cpu_norm: cpu.appNormalizedPercent,
      mem_anon_mib: anonymousMemoryMib(s),
      mem_total_mb: s.memory.sys_used_kb ? Math.round(s.memory.sys_used_kb / 1024) : null,
      mem_ratio: isAndroidAnonymousMemory(s) ? null : sysTotalKb > 0 && appMemKb > 0 ? Math.round((appMemKb / sysTotalKb) * 1000) / 10 : s.cpu.mem_percent || null,
      battery: s.battery.level_percent ?? null,
      battery_ma: iMa,
      voltage_mv: vMv,
      power_mw: vMv !== null && iMa !== null ? Math.round(Math.abs(vMv * iMa) / 1000) : null,
      gpu_busy: s.gpu.busy_percent ?? null,
      gpu_clock: s.gpu.clock_mhz ?? null,
    }
    for (const [key, value] of Object.entries(normalizeThermalZones(s.thermal?.zones_c))) row[`thermal:${key}`] = value
    if (temperatureZone === 'skin') row['thermal:skin'] = measuredSkinTemperature(s)
    return row
  })
}

/** Add duration-aligned comparison values without extending the saved session
 * past its own last sample. Within its range, the closest saved sample wins. */
export function mergeComparisonRows<T extends Record<string, number | null>>(
  liveRows: T[],
  comparisonRows: T[]
): T[] {
  if (!comparisonRows.length) return liveRows
  const firstT = comparisonRows[0].t
  const lastT = comparisonRows[comparisonRows.length - 1].t
  return liveRows.map((row) => {
    if (row.t === null || firstT === null || lastT === null || row.t < firstT || row.t > lastT) return row
    const closest = comparisonRows.reduce((best, candidate) =>
      Math.abs((candidate.t ?? 0) - row.t!) < Math.abs((best.t ?? 0) - row.t!) ? candidate : best
    )
    const merged: Record<string, number | null> = { ...row }
    for (const [key, value] of Object.entries(closest)) {
      if (key !== 't') merged[`comparison:${key}`] = value
    }
    return merged as T
  })
}

/* eslint-enable react-refresh/only-export-components */

/** Per-metric plotting config: which key is the headline series, optional
 *  secondary legend series, and the y-axis bounds. `yMin`/`yMax` are SOFT
 *  framing bounds, not clamps — the axis still grows past `yMax` when a real
 *  reading exceeds it (e.g. multi-core CPU% > 100), so values are never
 *  clipped. They only set the default 0–100 frame for percentage metrics. */
interface MetricPlot {
  primaryKey: string
  /** Legend label for the headline series, shown in the card's legend row so the
   *  primary (e.g. battery temp, system CPU%) is named alongside the secondaries. */
  primaryLabel: string
  secondary: MetricSeriesDef[]
  yMin?: number
  yMax?: number
}

export function LiveCharts({ snapshots, comparisonSnapshots = [], visibility, order, onReorder }: Props) {
  const t = useMessages()
  const { locale } = useLocale()
  const ko = locale === 'ko'
  const sample = snapshots.at(-1)
  const anonymous = isAndroidAnonymousMemory(sample)
  const legacyLabel = sample?.device.platform === 'ios' ? 'iOS app memory' : 'Legacy PSS / RSS'
  const appKey = sample ? JSON.stringify([sample.device.serial, sample.package.bundle_id]) : ''
  const [manualCategories, setManualCategories] = useState<Record<string, MemoryCategory | undefined>>({})
  const observed = observedCategory(sample)
  const category = observed ?? manualCategories[appKey] ?? null
  const reference = currentMemoryReference(sample, category)
  const stateLabel = reference && ({ foreground: ko ? '포그라운드' : 'foreground', background: ko ? '백그라운드' : 'background',
    user_perceived_services: ko ? '사용자 인식 서비스' : 'perceptible service' } as Record<string, string>)[reference.state]
  const referenceLabel = reference ? `${ko ? 'Play 로컬 참고' : 'Play local reference'} · ${reference.tier} · ${stateLabel} · ${reference.mib} MiB` : undefined
  const memorySelector = anonymous && !observed ? (
    <select aria-label={ko ? '앱 유형' : 'App category'}
      className="max-w-full rounded border border-line bg-bg px-1 text-[11px] text-ink"
      value={manualCategories[appKey] ?? ''}
      onChange={e => {
        const option = e.currentTarget.value
        setManualCategories(old => ({ ...old, [appKey]: option === 'app' || option === 'game' ? option : undefined }))
      }}>
      <option value="">{ko ? '유형 선택' : 'Choose type'}</option>
      <option value="app">{ko ? '앱' : 'App'}</option>
      <option value="game">{ko ? '게임' : 'Game'}</option>
    </select>
  ) : undefined
  const temperatureZone = temperatureZoneForSnapshots(snapshots)
  const comparisonTemperatureZone = temperatureZoneForSnapshots(comparisonSnapshots)
  const primaryKeys = useMemo(() => metricPrimaryKeys(temperatureZone), [temperatureZone])
  const metricDefs = useMemo(() => localizedLiveMetrics(t.charts, temperatureZone), [t.charts, temperatureZone])
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )
  const { data, cpuSeries, thermalSeries } = useMemo(() => {
    if (snapshots.length === 0) {
      return {
        data: [] as RowDatum[],
        cpuSeries: [] as MetricSeriesDef[],
        thermalSeries: [] as MetricSeriesDef[],
      }
    }
    const logicalCpuCount = stableLogicalCpuCount(snapshots)
    let coreSeriesCount = 0
    const thermalKeys = new Set<string>()
    for (const s of snapshots) {
      coreSeriesCount = Math.max(coreSeriesCount, s.cpu?.per_core_freq_mhz?.length ?? 0, s.cpu?.per_core_percent?.length ?? 0)
      for (const k of Object.keys(normalizeThermalZones(s.thermal?.zones_c))) thermalKeys.add(k)
    }

    const data: RowDatum[] = snapshots.map((s, index) => {
      const t = Math.round(snapshotElapsed(snapshots, index))
      const zones = normalizeThermalZones(s.thermal?.zones_c)
      const cpu = deriveCpuDisplayMetrics(s.cpu, s.device.platform, logicalCpuCount)
      // App memory: PSS when available, else RSS (live streaming reports RSS).
      const appMemKb = s.memory.pss_total_kb || s.memory.rss_total_kb || 0
      // App memory as a % of total RAM (the MEM card's headline 비율%). Prefer
      // the directly-measured ratio so it matches the plotted MB; fall back to
      // the backend-provided cpu.mem_percent when system RAM totals are absent
      // (e.g. iOS reports no MemTotal).
      const sysTotalKb = s.memory.sys_total_kb || 0
      const memRatio =
        sysTotalKb > 0 && appMemKb > 0
          ? Math.round((appMemKb / sysTotalKb) * 1000) / 10
          : s.cpu.mem_percent || null
      const vMv = s.battery.voltage_mv ?? null
      const iMa = s.battery.current_ma ?? null
      // Power (mW) = |V(mV) × I(mA)| / 1000. current_ma sign flips by vendor, so
      // take magnitude for the consumption reading.
      const powerMw = vMv !== null && iMa !== null ? Math.round(Math.abs(vMv * iMa) / 1000) : null
      const frameQuality = deriveFrameQualitySeries(s.frames)
      const row: RowDatum = {
        t,
        fps: s.frames.fps_estimate ?? null,
        ...frameQuality,
        small_jank: s.frames.small_jank ?? null,
        big_jank: s.frames.big_jank ?? null,
        cpu_app: cpu.appWholeDevicePercent,
        cpu_total: cpu.systemPercent,
        cpu_norm: cpu.appNormalizedPercent,
        mem_anon_mib: anonymousMemoryMib(s),
      mem_total_mb: s.memory.sys_used_kb ? Math.round(s.memory.sys_used_kb / 1024) : null,
        mem_ratio: isAndroidAnonymousMemory(s) ? null : memRatio,
        battery: s.battery.level_percent ?? null,
        battery_ma: s.battery.current_ma ?? null,
        voltage_mv: vMv,
        power_mw: powerMw,
        gpu_busy: s.gpu.busy_percent ?? null,
        gpu_clock: s.gpu.clock_mhz ?? null,
      }
      const freqs = s.cpu?.per_core_freq_mhz ?? []
      for (let i = 0; i < coreSeriesCount; i++) {
        row[`cpu${i}`] = freqs[i] ?? null
        row[`cpu_util_${i}`] = s.cpu.per_core_percent?.[i] ?? null
      }
      for (const k of thermalKeys) {
        row[`thermal:${k}`] = zones[k] ?? null
      }
      if (temperatureZone === 'skin') row['thermal:skin'] = measuredSkinTemperature(s)
      return row
    })

    const cpuSeries: MetricSeriesDef[] = []
    for (let i = 0; i < coreSeriesCount; i++) {
      cpuSeries.push({ key: `cpu${i}`, label: `c${i} MHz`, color: CPU_COLORS[i % CPU_COLORS.length] })
      if (snapshots.some((s) => s.cpu.per_core_percent?.length)) {
        cpuSeries.push({ key: `cpu_util_${i}`, label: `c${i} %`, color: CPU_COLORS[i % CPU_COLORS.length], axis: 'primary' })
      }
    }

    const thermalSeries: MetricSeriesDef[] = []
    let fallbackIdx = 0
    for (const k of thermalKeys) {
      thermalSeries.push({
        key: `thermal:${k}`,
        label: k,
        color: THERMAL_COLORS[k] ?? FALLBACK_THERMAL_COLORS[fallbackIdx++ % FALLBACK_THERMAL_COLORS.length],
      })
    }

    const alignedData = comparisonSnapshots.length > 0
      ? mergeComparisonRows(data, buildComparisonRows(comparisonSnapshots))
      : data
    return { data: alignedData, cpuSeries, thermalSeries }
  }, [snapshots, comparisonSnapshots, temperatureZone])

  const plots = useMemo<Record<LiveMetricKey, MetricPlot>>(
    () => ({
      fps: {
        primaryKey: primaryKeys.fps,
        primaryLabel: 'fps',
        // Jank shares the FPS y-axis (axis: 'primary') so the jank line is
        // framed against the actual FPS scale instead of its own auto-zoom.
        secondary: [
          {
            key: 'fps_1pct_low',
            label: '1% Low (fps)',
            color: '#fb923c',
            axis: 'primary',
          },
          {
            key: 'stutter_percent',
            label: 'Stutter (%)',
            color: '#ef4444',
          },
          {
            key: 'smooth_percent',
            label: 'Smooth (%)',
            color: '#22c55e',
          },
          { key: 'small_jank', label: 'small jank', color: '#fbbf24', axis: 'primary' },
          { key: 'big_jank', label: 'big jank', color: '#ef4444', axis: 'primary' },
        ],
        yMin: 0,
      },
      // New Android captures use verified HAL Skin. iOS and older recordings
      // retain battery temperature; the chosen sensor is never drawn twice.
      temp: {
        primaryKey: primaryKeys.temp,
        primaryLabel: temperatureZone === 'skin' ? 'Skin' : 'battery',
        secondary: thermalSeries.filter((s) => s.key !== primaryKeys.temp),
      },
      cpu: {
        primaryKey: primaryKeys.cpu,
        // System-wide total % is the headline; app CPU% shares the same %
        // axis (axis: 'primary'); per-core MHz keep their own secondary axis.
        primaryLabel: t.charts.systemCpu,
        secondary: [
          { key: 'cpu_app', label: t.charts.appCpuWholeDevice, color: '#1d4ed8', axis: 'primary' },
          ...cpuSeries,
        ],
        yMin: 0,
        yMax: 100,
      },
      cpu_norm: {
        primaryKey: primaryKeys.cpu_norm,
        primaryLabel: t.charts.appCpuNormalized,
        secondary: [],
        yMin: 0,
        yMax: 100,
      },
      gpu: {
        primaryKey: primaryKeys.gpu,
        primaryLabel: 'busy %',
        secondary: [{ key: 'gpu_clock', label: 'clk', color: '#a855f7' }],
        yMin: 0,
        yMax: 100,
      },
      mem: anonymous
        ? { primaryKey: 'mem_anon_mib', primaryLabel: memoryChartLabel, secondary: [], yMin: 0 }
        : { primaryKey: primaryKeys.mem, primaryLabel: legacyLabel, secondary: [{ key: 'mem_total_mb', label: 'total used', color: '#16a34a' }], yMin: 0, yMax: 100 },
      power: {
        primaryKey: primaryKeys.power,
        primaryLabel: 'power',
        secondary: [{ key: 'voltage_mv', label: 'mV', color: '#a16207' }],
        yMin: 0,
      },
      batt: {
        primaryKey: primaryKeys.batt,
        primaryLabel: 'level',
        secondary: [{ key: 'battery_ma', label: 'mA', color: '#34d3c0' }],
        yMin: 0,
        yMax: 100,
      },
    }),
    [cpuSeries, thermalSeries, t.charts, primaryKeys, temperatureZone, anonymous, legacyLabel]
  )

  if (snapshots.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-sm text-muted">
        {t.charts.emptyState}
      </div>
    )
  }

  // Visible cards in saved order. `order` carries every key; hidden ones are
  // filtered out for rendering but kept in `order` so re-enabling restores place.
  const visibleDefs = order
    .map((k) => metricDefs.find((m) => m.key === k))
    .filter((m): m is LiveMetricDef => !!m && isSeriesVisible(visibility, m.key))
  const visibleKeys = visibleDefs.map((m) => m.key)

  function onDragEnd(event: DragEndEvent) {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oldIdx = visibleKeys.indexOf(active.id as LiveMetricKey)
    const newIdx = visibleKeys.indexOf(over.id as LiveMetricKey)
    if (oldIdx < 0 || newIdx < 0) return
    const reordered = arrayMove(visibleKeys, oldIdx, newIdx)
    // Rebuild the full order: reordered visible keys, then hidden keys after.
    const hidden = order.filter((k) => !reordered.includes(k))
    onReorder([...reordered, ...hidden])
  }

  return (
    <LiveCrosshairProvider>
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={visibleKeys} strategy={verticalListSortingStrategy}>
          <div className="flex flex-col gap-3 p-1">
            {visibleDefs.map((m) => {
              const plot = plots[m.key]
              const secondary = plot.secondary.filter((s) => isSeriesVisible(visibility, s.key))
              const comparisonSecondary =
                comparisonSnapshots.length > 0 && m.key === 'cpu' && isSeriesVisible(visibility, 'cpu_app')
                  ? [{
                      key: 'comparison:cpu_app',
                      label: t.charts.appCpuWholeDevice,
                      color: '#1d4ed8',
                      axis: 'primary' as const,
                    }]
                  : []
              return (
                <SortableMetricCard
                  key={m.key}
                  chartsT={t.charts}
                  metricKey={m.key}
                  label={m.label}
                  accent={m.accent}
                  unit={m.key === 'mem' && anonymous ? 'MiB' : m.unit}
                  digits={m.digits}
                  helpText={m.key === 'mem' && anonymous
                    ? (ko ? '선택한 프로세스의 RssAnon + VmSwap입니다. 기기에서 확인할 수 없는 구간은 비워 둡니다. 기준선은 최신 상태의 로컬 참고값이며 Play Vitals 판정이 아닙니다.'
                          : 'RssAnon + VmSwap of the selected process. Unreadable samples are gaps. Reference is for the latest local state only, not a Play Vitals result.')
                    : m.helpText}
                  headerControl={m.key === 'mem' ? memorySelector : undefined}
                  horizontalReference={m.key === 'mem' && reference && referenceLabel ? { value: reference.mib, label: referenceLabel } : undefined}
                  statusText={m.key === 'temp' && temperatureZone === 'skin'
                    ? measuredSkinTemperature(snapshots[snapshots.length - 1]) !== null
                      ? t.charts.skinMeasured : t.charts.skinUnavailable
                    : metricStatusLabel(snapshots[snapshots.length - 1], m.key)}
                  data={data}
                  primaryKey={plot.primaryKey}
                  primaryLabel={plot.primaryLabel}
                  secondary={secondary}
                  comparisonKey={comparisonSnapshots.length > 0 &&
                    (m.key !== 'temp' || temperatureZone === comparisonTemperatureZone) &&
                    (m.key !== 'mem' || (anonymous === isAndroidAnonymousMemory(comparisonSnapshots.at(-1)) &&
                      sample?.device.platform === comparisonSnapshots.at(-1)?.device.platform))
                    ? `comparison:${plot.primaryKey}` : undefined}
                  comparisonSecondary={comparisonSecondary}
                  comparisonLabel={t.liveTracing.comparisonLegend}
                  yMin={plot.yMin}
                  yMax={plot.yMax}
                />
              )
            })}
          </div>
        </SortableContext>
      </DndContext>
      <HoverOverlay data={data} temperatureZone={temperatureZone} anonymousMemory={anonymous} legacyMemoryLabel={legacyLabel} />
      <SummaryOverlay data={data} snapshots={snapshots} />
    </LiveCrosshairProvider>
  )
}

/** Wraps a MetricCard as a dnd-kit sortable item, injecting a grip drag handle.
 *  The handle owns the drag listeners, so the chart plot's own mouse interactions
 *  (hover/pin/select/zoom) are never hijacked by the sort. */
function SortableMetricCard({
  chartsT,
  ...props
}: ComponentProps<typeof MetricCard> & { chartsT: ChartsMessages }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: props.metricKey,
  })
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : 1,
    zIndex: isDragging ? 20 : undefined,
    position: 'relative' as const,
  }
  const handle = (
    <button
      type="button"
      {...attributes}
      {...listeners}
      className="flex-none cursor-grab touch-none text-faint transition-colors hover:text-muted active:cursor-grabbing"
      aria-label={chartsT.reorderDragLabel}
      title={chartsT.reorderDragTitle}
    >
      <GripVertical size={16} />
    </button>
  )
  return (
    <div ref={setNodeRef} style={style}>
      <MetricCard {...props} dragHandle={handle} />
    </div>
  )
}
