// Test fixtures for the analysis dashboard.

import type {
  MetricStats,
  SessionAnalysis,
  SessionListItem,
} from '../lib/types'

export function makeStats(over: Partial<MetricStats> = {}): MetricStats {
  return {
    valid_count: 10,
    total_count: 10,
    coverage_percent: 100,
    average: 60,
    median: 60,
    min: 55,
    max: 65,
    p05: 56,
    p95: 64,
    p99: 65,
    stddev: 2,
    ...over,
  }
}

const METRIC_KEYS = [
  'fps',
  'cpu_app',
  'cpu_system',
  'cpu_norm',
  'gpu_busy',
  'gpu_clock',
  'gpu_temp',
  'app_memory_mb',
  'power_mw',
  'battery_level',
] as const

export function makeAnalysis(over: Partial<SessionAnalysis> = {}): SessionAnalysis {
  const metrics: Record<string, MetricStats> = {}
  for (const k of METRIC_KEYS) metrics[k] = makeStats()
  return {
    session_id: 'sess-1',
    file_name: 'a.csv',
    file_path: '/raw/a.csv',
    is_legacy: false,
    schema_version: 2,
    session_name: null,
    app_label: 'Example Game',
    app_icon: null,
    bundle_id: 'com.example.game',
    app_version: '1.0',
    device_model: 'Pixel 8',
    platform: 'android',
    os_version: '14',
    code_name: '',
    hw_ver: '',
    serial_hash: 'abc',
    started_at: '2026-07-11T10:00:00',
    ended_at: '2026-07-11T10:05:00',
    duration_sec: 300,
    sample_count: 10,
    metrics,
    fps_stability_percent: 90,
    frames: {
      total_frames: 1000,
      janky_frames: 25,
      small_jank: 20,
      big_jank: 5,
      weighted_jank_percent: 2.5,
      worst_window_p95_ms: 18,
      worst_window_p99_ms: 30,
      missed_vsync: 3,
      slow_ui_thread: 1,
      frame_deadline_missed: 2,
    },
    temp_zones: [
      { zone: 'battery', start: 30, end: 35, max: 35, change: 5 },
      { zone: 'cpu', start: 40, end: 50, max: 52, change: 10 },
    ],
    battery_zone: 'battery',
    max_temp_battery: 35,
    hottest_sensor_zone: 'cpu',
    hottest_sensor_max: 52,
    peak_app_memory_mb: 250,
    memory_ratio_percent: 12,
    battery_start_level: 80,
    battery_end_level: 74,
    battery_change_pp: -6,
    timeseries: {
      t: [0, 1, 2, 3, 4],
      fps: [60, 59, 58, 60, 61],
      cpu_system: [30, 32, 31, 33, 30],
      cpu_app: [18, 19, 18, 20, 19],
      cpu_norm: [18, 19, 18, 20, 19],
      gpu_busy: [50, 55, 53, 52, 54],
      app_memory_mb: [240, 245, 250, 248, 246],
      power_mw: [1800, 1900, 2000, 1850, 1875],
      battery_level: [80, 79, 78, 77, 74],
      'thermal:battery': [30, 31, 32, 34, 35],
      'thermal:cpu': [40, 44, 48, 50, 52],
    },
    ...over,
  }
}

export function makeListItem(over: Partial<SessionListItem> = {}): SessionListItem {
  return {
    session_id: 'sess-1',
    file_name: 'a.csv',
    file_path: '/raw/a.csv',
    is_legacy: false,
    schema_version: 2,
    session_name: null,
    app_label: 'Example Game',
    app_icon: null,
    bundle_id: 'com.example.game',
    app_version: '1.0',
    device_model: 'Pixel 8',
    platform: 'android',
    os_version: '14',
    serial_hash: 'abc',
    started_at: '2026-07-11T10:00:00',
    ended_at: '2026-07-11T10:05:00',
    duration_sec: 300,
    sample_count: 10,
    modified_at: 1,
    ...over,
  }
}
