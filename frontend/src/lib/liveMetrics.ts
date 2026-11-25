import type { ChartsMessages } from './i18n/namespaces/charts'
import type { TemperatureZone } from './thermalMetrics'

// Central definition of the six live-dashboard metrics. Shared by the chart
// grid (LiveCharts) and the "표시 항목" visibility menu so labels, accent
// colours, and ordering stay in sync.

/** A single plotted/legended series within a metric card. */
export interface MetricSeriesDef {
  key: string
  label: string
  color: string
  /** Which y-axis to plot on. 'primary' shares the headline series' scale
   *  (use for same-unit comparisons like app% vs total%); 'secondary'
   *  (default) gets its own auto-scaled axis (use for different units). */
  axis?: 'primary' | 'secondary'
}

export type LiveMetricKey =
  | 'fps'
  | 'temp'
  | 'cpu'
  | 'cpu_norm'
  | 'gpu'
  | 'mem'
  | 'power'
  | 'batt'

/** A second-level toggle inside a metric card. `key` matches the series key in
 *  the chart row data (e.g. `thermal:battery`, `gpu_clock`) so toggling it
 *  shows/hides exactly that secondary line. */
export interface LiveMetricSubDef {
  key: string
  label: string
}

export interface LiveMetricDef {
  key: LiveMetricKey
  label: string
  /** Accent colour for the value, dot, and sparkline (from the wireframe). */
  accent: string
  unit: string
  digits: number
  /** Optional explanation shown from the metric card's visible help control. */
  helpText?: string
  /** Optional minor items shown as a collapsible tree under the metric. */
  subs?: LiveMetricSubDef[]
}

export const LIVE_METRICS: LiveMetricDef[] = [
  {
    key: 'fps',
    label: 'FPS',
    accent: '#f59e6b',
    unit: 'fps',
    digits: 0,
    subs: [
      { key: 'fps_1pct_low', label: '1% Low (fps)' },
      { key: 'stutter_percent', label: 'Stutter (%)' },
      { key: 'smooth_percent', label: 'Smooth (%)' },
      { key: 'small_jank', label: 'Small Jank' },
      { key: 'big_jank', label: 'Big Jank' },
    ],
  },
  {
    key: 'temp',
    label: 'Temp',
    accent: '#f47171',
    unit: '°C',
    digits: 1,
    // The selected headline sensor is omitted by localizedLiveMetrics.
    subs: [
      { key: 'thermal:cpu_max', label: 'CPU Max' },
      { key: 'thermal:gpu_max', label: 'GPU Max' },
      { key: 'thermal:battery', label: 'Battery' },
      { key: 'thermal:skin', label: 'Skin' },
      { key: 'thermal:ddr', label: 'DDR' },
    ],
  },
  {
    key: 'cpu',
    label: 'CPU utilization',
    accent: '#5b9cf6',
    unit: '%',
    digits: 0,
    // System-wide total % is now the headline; app% is the toggleable overlay.
    subs: [{ key: 'cpu_app', label: 'App CPU (whole device)' }],
  },
  {
    key: 'cpu_norm',
    label: 'App CPU (normalized)',
    accent: '#60a5fa',
    unit: '%',
    digits: 0,
  },
  {
    key: 'gpu',
    label: 'GPU',
    accent: '#a980f0',
    unit: '%',
    digits: 0,
    subs: [{ key: 'gpu_clock', label: 'Clock (MHz)' }],
  },
  {
    key: 'mem',
    label: 'Mem',
    accent: '#3fc78a',
    // Headline is the app's memory as a % of total RAM (ratio); the absolute
    // system "total used" stays available as an MB overlay.
    unit: '%',
    digits: 1,
    subs: [{ key: 'mem_total_mb', label: 'Total used (system, MB)' }],
  },
  {
    key: 'power',
    label: 'Power',
    accent: '#f59e0b',
    unit: 'mW',
    digits: 0,
    subs: [{ key: 'voltage_mv', label: 'Voltage (mV)' }],
  },
  {
    key: 'batt',
    label: 'Battery',
    accent: '#34d3c0',
    unit: '%',
    digits: 0,
    subs: [{ key: 'battery_ma', label: 'Current (mA)' }],
  },
]

/** The row-data key that supplies each metric card's headline (대표) value.
 *  Shared by the chart grid (LiveCharts `plots`) and the hover overlay so the
 *  big value, the sparkline, and the tooltip never disagree about what a card
 *  represents. */
export const METRIC_PRIMARY_KEY: Record<LiveMetricKey, string> = {
  fps: 'fps',
  temp: 'thermal:battery',
  cpu: 'cpu_total',
  cpu_norm: 'cpu_norm',
  gpu: 'gpu_busy',
  mem: 'mem_ratio',
  power: 'power_mw',
  batt: 'battery',
}

/** Use one sensor selection for the card, hover tooltip and range summary. */
export function metricPrimaryKeys(temperatureZone: TemperatureZone): Record<LiveMetricKey, string> {
  return { ...METRIC_PRIMARY_KEY, temp: `thermal:${temperatureZone}` }
}

/** Visibility map keyed by both major metric keys (LiveMetricKey) and minor
 *  sub-series keys. A missing key is treated as visible, so newly added
 *  series and dynamic zones default on without a migration. */
export type MetricVisibility = Record<string, boolean>

export const DEFAULT_METRIC_VISIBILITY: MetricVisibility = {
  fps: true,
  temp: true,
  cpu: true,
  cpu_norm: true,
  gpu: true,
  mem: true,
  power: true,
  batt: true,
  ...Object.fromEntries(
    LIVE_METRICS.flatMap((m) => (m.subs ?? []).map((s) => [s.key, true] as const))
  ),
}

/** Localize product-facing metric names while keeping stable keys/order/config. */
export function localizedLiveMetrics(charts: ChartsMessages, temperatureZone: TemperatureZone = 'battery'): LiveMetricDef[] {
  return LIVE_METRICS.map((metric) => {
    if (metric.key === 'temp') {
      return {
        ...metric,
        label: temperatureZone === 'skin' ? 'Skin' : charts.batteryTemperature,
        helpText: temperatureZone === 'skin' ? charts.skinTemperatureHelp : undefined,
        subs: metric.subs?.filter((sub) => sub.key !== `thermal:${temperatureZone}`),
      }
    }
    if (metric.key === 'cpu') {
      return {
        ...metric,
        label: charts.cpuUsage,
        helpText: charts.cpuUsageHelp,
        subs: [{ key: 'cpu_app', label: charts.appCpuWholeDevice }],
      }
    }
    if (metric.key === 'cpu_norm') {
      return {
        ...metric,
        label: charts.appCpuNormalized,
        helpText: charts.cpuNormalizationHelp,
      }
    }
    return metric
  })
}

/** True unless the series has been explicitly toggled off. Unknown keys (e.g.
 *  dynamic CPU cores or thermal zones not in the static tree) default visible. */
export function isSeriesVisible(visibility: MetricVisibility, key: string): boolean {
  return visibility[key] !== false
}

export interface FrameQualitySeries {
  fps_1pct_low: number | null
  stutter_percent: number | null
  smooth_percent: number | null
}

/**
 * Derive the FPS card's quality series from one real frame window. Missing or
 * invalid inputs remain unavailable instead of becoming a plausible-looking 0.
 */
export function deriveFrameQualitySeries(frames: {
  total_frames?: number | null
  janky_frames?: number | null
  p99_ms?: number | null
}): FrameQualitySeries {
  const total = frames.total_frames
  const janky = frames.janky_frames
  const p99 = frames.p99_ms

  if (
    typeof total !== 'number' ||
    !Number.isFinite(total) ||
    total <= 0 ||
    typeof janky !== 'number' ||
    !Number.isFinite(janky) ||
    typeof p99 !== 'number' ||
    !Number.isFinite(p99) ||
    p99 <= 0
  ) {
    return {
      fps_1pct_low: null,
      stutter_percent: null,
      smooth_percent: null,
    }
  }

  const stutter = Math.min(100, Math.max(0, (janky / total) * 100))
  return {
    fps_1pct_low: 1000 / p99,
    stutter_percent: stutter,
    smooth_percent: 100 - stutter,
  }
}
