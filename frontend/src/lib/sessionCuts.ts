import type { MetricsSnapshot, SessionCut } from './types'

/** Preserve real timestamps while measuring on the active (pause-free) axis. */
export function snapshotElapsed(snapshots: MetricsSnapshot[], index: number): number {
  const first = snapshots[0]
  const sample = snapshots[index]
  if (!first || !sample) return 0
  if (typeof sample.elapsed_sec === 'number' && typeof first.elapsed_sec === 'number') {
    return Math.max(0, sample.elapsed_sec - first.elapsed_sec)
  }
  return Math.max(0, (Date.parse(sample.captured_at) - Date.parse(first.captured_at)) / 1000)
}

export function closeLiveCut(cuts: SessionCut[], active: Omit<SessionCut, 'end_sec'> | null, snapshots: MetricsSnapshot[]): SessionCut[] {
  const end = snapshotElapsed(snapshots, snapshots.length - 1)
  return active && end > active.start_sec ? [...cuts, { ...active, end_sec: end }] : cuts
}
