import { useMessages } from '../lib/i18n'

interface Props {
  state: 'idle' | 'refreshing' | 'tracing' | 'paused' | 'stopped' | 'error'
  message?: string | null
  /** Raw, untranslated text for the hover tooltip (debugging). Falls back to
   *  `message` when absent, so the friendly text shown inline still has a title. */
  rawMessage?: string | null
  sampleCount: number
  lastCapturedAt: string | null
  durationSec: number | null
}

const STATE_COLORS: Record<Props['state'], string> = {
  idle: 'text-muted',
  refreshing: 'text-accent',
  tracing: 'text-ok',
  paused: 'text-accent',
  stopped: 'text-muted',
  error: 'text-rec',
}

/** Render a UTC ISO timestamp (captured_at) as local HH:MM:SS. Falls back to the
 *  raw HH:MM:SS substring if the string is not parseable. Without this the time
 *  showed in UTC, reading hours off for non-UTC users (e.g. KST = UTC+9). */
function formatLocalTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso.slice(11, 19)
  return date.toLocaleTimeString('en-GB', { hour12: false })
}

export function StatusBar({ state, message, rawMessage, sampleCount, lastCapturedAt, durationSec }: Props) {
  const t = useMessages()
  const stateLabels: Record<Props['state'], string> = {
    idle: t.statusBar.stateIdle,
    refreshing: t.statusBar.stateRefreshing,
    tracing: t.statusBar.stateTracing,
    paused: t.capture.paused,
    stopped: t.statusBar.stateStopped,
    error: t.statusBar.stateError,
  }
  return (
    <footer className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-line bg-surface px-5 py-2 text-xs">
      <span className={`font-semibold ${STATE_COLORS[state]}`}>{stateLabels[state]}</span>
      {message && (
        <span className="min-w-0 break-words text-muted" title={rawMessage ?? message}>
          · {message}
        </span>
      )}
      <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-muted tabular-nums">
        <span>
          samples: <span className="font-mono text-ink">{sampleCount}</span>
        </span>
        {lastCapturedAt && (
          <span>
            last @ <span className="font-mono text-ink">{formatLocalTime(lastCapturedAt)}</span>
          </span>
        )}
        {durationSec !== null && (
          <span>
            duration <span className="font-mono text-ink">{durationSec.toFixed(1)}s</span>
          </span>
        )}
      </span>
    </footer>
  )
}
