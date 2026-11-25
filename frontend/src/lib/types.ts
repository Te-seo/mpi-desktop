// Type mirrors of the Python dataclasses in mpi/schema.py.
// Keep these in sync when the Python schema changes.

export type Source = 'live_csv'
export type Platform = 'android' | 'ios'

export interface FpsStats {
  median: number
  average: number
  median_stability: number
  average_stability: number
  first_half: number
  first_half_stability: number
  second_half: number
  second_half_stability: number
  max_fps: number
  fps_std: number
  fps_below_90pct_ratio: number
  norm_fps_std: number | null
  norm_fps_avg: number | null
  norm_fps_max: number | null
}

export interface ThermalSummary {
  zones: Record<string, [number, number, number]>
}

export interface ReportSummary {
  file_path: string
  source: Source
  duration_min: number
  duration_sec: number
  fps: FpsStats | null
  thermal: ThermalSummary | null
  cpu_usage_avg: number | null
  gpu_usage_avg: number | null
  power_mw_avg: number | null
  abs_time_start: string | null
  abs_time_end: string | null
  metadata: Record<string, string>
}

export interface FrameStats {
  total_frames: number
  janky_frames: number
  small_jank: number
  big_jank: number
  jank_percent: number
  p50_ms: number
  p90_ms: number
  p95_ms: number
  p99_ms: number
  missed_vsync: number
  slow_ui_thread: number
  frame_deadline_missed: number
  fps_estimate: number | null
  source?: string | null
  scope?: string | null
  experimental?: boolean
  raw_events?: { timestamp_ns: number; duration_ns: number; source: string; scope: string; stream_id: string }[]
  raw_events_dropped?: number
}

export interface MemStats {
  pss_total_kb: number | null
  rss_total_kb: number | null
  memory_source?: string | null
  anon_rss_kib?: number | null
  swap_kib?: number | null
  anon_rss_plus_swap_kib?: number | null
  play_memory_state?: string | null
  play_memory_state_source?: string | null
  play_memory_category?: string | null
  play_memory_category_source?: string | null
  play_memory_ram_kib?: number | null
  play_memory_sdk_int?: number | null
  play_memory_form_factor?: string | null
  play_memory_pid?: number | null
  play_memory_start_ticks?: number | null
  java_heap_kb: number
  native_heap_kb: number
  graphics_kb: number
  code_kb: number
  stack_kb: number
  /** System-wide RAM totals (0 when unavailable, e.g. iOS). */
  sys_total_kb: number
  sys_used_kb: number
}

export interface CpuStats {
  pid: number
  /** Raw app/process CPU; 100% means one fully busy logical CPU. */
  cpu_percent: number | null
  mem_percent: number
  /** System-wide CPU% across all cores (null when the collector cannot supply it). */
  total_percent: number | null
  per_core_freq_mhz: number[]
  /** App CPU normalization, 0–100: frequency-adjusted on Android, core-only on iOS. */
  cpu_norm?: number | null
  per_core_percent?: (number | null)[]
  logical_core_count?: number | null
  total_source?: string | null
  total_status?: string | null
}

/** Device-wide non-loopback network counters and throughput (null until sampled). */
export interface NetworkStats {
  rx_bytes: number | null
  tx_bytes: number | null
  rx_kbps: number | null
  tx_kbps: number | null
}

export interface GpuStats {
  busy_percent: number | null
  clock_mhz: number | null
  temp_c: number | null
}

export interface ThermalStats {
  zones_c: Record<string, number>
  /** Sensor provenance is absent in captures made before Skin collection. */
  sources?: Record<string, string>
  status?: Record<string, string>
}

export interface BatteryStats {
  level_percent: number | null
  current_ma: number | null
  status: string | null
  voltage_mv: number | null
}

export interface DeviceInfo {
  serial: string
  platform: Platform
  model: string
  /** Apple ProductType and observed regulatory A-number; never a serial. */
  model_identifier?: string
  model_number?: string
  /** User-assigned phone/tablet name; model remains hardware identity. */
  display_name?: string
  os_version: string
  /** Firmware build identifier and human-readable build name, when supplied by the device. */
  build_version?: string
  build_name?: string
  connection_type: 'usb' | 'wireless'
  /** Real hardware serial (ro.serialno). Empty for USB/iOS where `serial` is
   *  already the real id; populated for wireless adb, where `serial` is ip:port. */
  hw_serial?: string
}

export interface PackageInfo {
  bundle_id: string
  label: string | null
  version_name: string | null
  version_code: string | null
  apk_path: string | null
  uid: string | null
}

export interface MetricsSnapshot {
  render_pipeline?: RenderPipeline | null
  /** Collector-confirmed pause history, retained across resume and persistence. */
  pause_used?: boolean
  elapsed_sec?: number | null
  device: DeviceInfo
  package: PackageInfo
  captured_at: string
  duration_sec: number
  frames: FrameStats
  memory: MemStats
  cpu: CpuStats
  /** Omitted by captures created before network export support. */
  network?: NetworkStats
  gpu: GpuStats
  thermal: ThermalStats
  battery: BatteryStats
  raw: Record<string, unknown>
  metric_status?: Record<string, { source: string; scope: string; status: 'fresh' | 'held' | 'unavailable'; age_sec: number | null }>
}

interface BufferQueueMetrics {
  queue_depth_max?: number | null
  queue_wait_samples?: number | null
  queue_wait_ms_avg?: number | null
  queue_wait_ms_max?: number | null
  submit_ms_avg?: number | null
  acquire_ms_avg?: number | null
}

export interface RenderPipeline {
  state: string
  frames_observed?: number | null
  actual_duration_ms_avg?: number | null
  buffer_stuffing?: number | null
  buffer_queue?: BufferQueueMetrics
  surface_present?: {
    source: 'android.surfaceflinger.frame'
    surface_present_frames?: number | null
    surface_present_intervals?: number | null
    surface_interval_ms_avg?: number | null
    surface_interval_ms_max?: number | null
  }
  points?: (BufferQueueMetrics & {
    bucket_id: number
    revision: number
    elapsed_sec: number
    actual_duration_ms_avg?: number | null
    surface_present_frames?: number | null
    surface_present_intervals?: number | null
    surface_interval_ms_avg?: number | null
    surface_interval_ms_max?: number | null
  })[]
}

// --- Analysis Dashboard: mirrors of mpi/analysis.py dataclasses ------
// Every derived statistic is nullable: null renders as "—" (no valid sample).

export interface MetricStats {
  valid_count: number
  total_count: number
  coverage_percent: number
  average: number | null
  median: number | null
  min: number | null
  max: number | null
  p05: number | null
  p95: number | null
  p99: number | null
  stddev: number | null
}

/** Canonical metric keys in SessionAnalysis.metrics (see analysis.py). */
export type MetricKey =
  | 'fps'
  | 'cpu_app'
  | 'cpu_system'
  | 'cpu_norm'
  | 'gpu_busy'
  | 'gpu_clock'
  | 'gpu_temp'
  | 'app_memory_mb'
  | 'anon_rss_plus_swap_mib'
  | 'power_mw'
  | 'battery_level'

export interface ZoneTempStats {
  zone: string
  start: number | null
  end: number | null
  max: number | null
  change: number | null
}

export interface FrameSummary {
  total_frames: number
  janky_frames: number
  small_jank: number
  big_jank: number
  weighted_jank_percent: number | null
  worst_window_p95_ms: number | null
  worst_window_p99_ms: number | null
  missed_vsync: number | null
  slow_ui_thread: number | null
  frame_deadline_missed: number | null
  raw_frame_count?: number
  raw_frames_dropped?: number
  session_p50_ms?: number | null
  session_p90_ms?: number | null
  session_p95_ms?: number | null
  session_p99_ms?: number | null
  raw_frame_sources?: string[]
  raw_frame_scope?: string[]
}

export interface SessionCut {
  id: string
  name: string
  start_sec: number
  end_sec: number
}

export interface SessionListItem {
  cuts?: SessionCut[]
  session_id: string
  file_name: string
  file_path: string
  is_legacy: boolean
  schema_version: number | null
  session_name: string | null
  app_label: string | null
  /** Cached launcher icon supplied as a data URI when available. */
  app_icon: string | null
  bundle_id: string
  app_version: string | null
  device_model: string
  device_model_identifier?: string | null
  device_model_number?: string | null
  device_display_name?: string | null
  device_build_version?: string | null
  device_build_name?: string | null
  platform: string
  os_version: string
  serial_hash: string
  started_at: string | null
  ended_at: string | null
  duration_sec: number
  sample_count: number
  modified_at: number
}

export interface SessionAnalysisRange {
  path: string
  start_sec: number
  end_sec: number
}

export interface SessionAnalysis {
  render_pipeline?: RenderPipeline | null
  cuts?: SessionCut[]
  session_id: string
  file_name: string
  file_path: string
  is_legacy: boolean
  schema_version: number | null
  session_name: string | null
  app_label: string | null
  app_icon: string | null
  bundle_id: string
  app_version: string | null
  device_model: string
  device_model_identifier?: string | null
  device_model_number?: string | null
  device_display_name?: string | null
  device_build_version?: string | null
  device_build_name?: string | null
  platform: string
  os_version: string
  code_name: string
  hw_ver: string
  serial_hash: string
  started_at: string | null
  ended_at: string | null
  duration_sec: number
  sample_count: number
  /** Keyed by MetricKey (fps, cpu_app, …). */
  metrics: Record<string, MetricStats>
  fps_stability_percent: number | null
  frames: FrameSummary
  temp_zones: ZoneTempStats[]
  battery_zone: string | null
  max_temp_battery: number | null
  hottest_sensor_zone: string | null
  hottest_sensor_max: number | null
  peak_app_memory_mb: number | null
  memory_source?: string | null
  memory_reference_context?: {
    play_memory_sdk_int?: number | null
    play_memory_ram_kib?: number | null
    play_memory_form_factor?: string | null
    play_memory_state?: string | null
    play_memory_state_source?: string | null
    play_memory_category?: string | null
  }
  memory_ratio_percent: number | null
  battery_start_level: number | null
  battery_end_level: number | null
  battery_change_pp: number | null
  /** Nullable per-sample series. Keys: t, <MetricKey>, and thermal:<zone>. */
  timeseries: Record<string, (number | null)[]>
  metric_provenance?: { fps_sources: string[]; fps_scopes: string[]; cpu_total_sources: string[]; status_counts: Record<string, Record<string, number>> }
}

export type ImportStatus = 'imported' | 'duplicate' | 'failed'

export interface ImportResult {
  source: string
  file_name: string
  status: ImportStatus
  session_id?: string
  path?: string
  reason?: string
  duplicate_of?: string
}

export interface ImportResponse {
  directory: string
  imported: number
  duplicates: number
  failed: number
  results: ImportResult[]
}

export interface ListSessionsResponse {
  directory: string
  sessions: SessionListItem[]
}

export interface DeleteSessionResult {
  path: string
  file_name: string
  status: 'deleted' | 'failed'
  reason?: string
}

export interface DeleteSessionsResponse {
  directory: string
  deleted: number
  failed: number
  results: DeleteSessionResult[]
}

export interface LoadAnalysesResponse {
  analyses: SessionAnalysis[]
  errors: { path: string; error: string }[]
  dropped: number
}

