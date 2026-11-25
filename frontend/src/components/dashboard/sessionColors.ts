// Fixed per-session identity for the comparison view and comparison charts.
// Up to 4 sessions are distinguished by a stable color AND an A–D label / line
// style, so they never rely on color alone (accessibility).

export const SESSION_COLORS = ['#34d3c0', '#f0a441', '#7c9cff', '#e46ab0']
export const SESSION_LABELS = ['A', 'B', 'C', 'D']
/** Solid / dashed / dash-dot / dotted, paired with SESSION_COLORS by index. */
export const SESSION_DASH: (string | undefined)[] = [undefined, '6 4', '2 3', '1 4']

/**
 * Accessible, fixed metric identity used by the analysis charts. Session
 * identity is deliberately expressed with A-D labels and line patterns, so a
 * metric keeps the same colour no matter which session is being inspected.
 */
export const METRIC_COLORS = {
  fps: '#0f9d8f',
  temp: '#e05a47',
  cpu: '#3978d4',
  cpu_app: '#8256c7',
  cpu_norm: '#5964b3',
  gpu: '#c5488d',
  mem: '#1686a5',
  power: '#b36b00',
  battery: '#568b2f',
} as const

export type DashboardMetricKey = keyof typeof METRIC_COLORS
