import { useEffect, useState } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { useMessages } from '../lib/i18n'
import { friendlyError } from '../lib/friendlyError'

/** Full-width banner shown when the measurement engine (data-engine sidecar)
 * fails to start or crashes. The main process reports health on the `fromMain`
 * channel with a stable sentinel code; friendlyError() maps it to the active
 * locale so this replaces the raw "spawn … ENOENT" main-process crash dialog
 * with a dismissible, natural-language notice and keeps the rest of the app
 * usable. */
export function EngineStatusBanner() {
  const t = useMessages()
  // Holds the raw sentinel/error from main; localized at render time so it
  // re-translates when the language changes.
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (typeof window === 'undefined' || !window.api) return
    return window.api.receive('fromMain', (...args: unknown[]) => {
      const m = args[0] as { type?: string; ok?: boolean; error?: string } | undefined
      if (!m || m.type !== 'engine-status') return
      setError(m.ok ? null : m.error ?? 'engine-start-failed')
    })
  }, [])

  if (!error) return null

  return (
    <div className="flex items-start gap-2 border-b border-[rgba(240,88,77,0.32)] bg-[rgba(240,88,77,0.12)] px-5 py-2.5 text-[13px] text-rec">
      <AlertTriangle size={16} className="mt-0.5 flex-none" aria-hidden />
      <span className="flex-1 leading-snug">{friendlyError(error, t.errors)}</span>
      <button
        type="button"
        onClick={() => setError(null)}
        className="flex-none rounded p-0.5 transition-opacity hover:opacity-70"
        aria-label={t.common.close}
      >
        <X size={15} />
      </button>
    </div>
  )
}
