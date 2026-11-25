import { useRef, useState } from 'react'
import { useMessages } from '../../lib/i18n'
import { call } from '../../lib/ipc'
import { friendlyError } from '../../lib/friendlyError'
import { formatSessionRangeTime, parseSessionRangeTime } from '../../lib/sessionRangeTime'
import type { SessionCut, SessionListItem } from '../../lib/types'

export function SessionCutsEditor({ session, range, onSaved, onAnalyze }: {
  session: SessionListItem
  range?: { start: number; end: number }
  onSaved(cuts: SessionCut[]): void
  onAnalyze(cut: SessionCut): void
}) {
  const t = useMessages()
  const m = t.capture
  const cuts = session.cuts ?? []
  const [draft, setDraft] = useState<{ id: string; name: string; start: string; end: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const start = draft ? parseSessionRangeTime(draft.start) : null
  const end = draft ? parseSessionRangeTime(draft.end) : null
  const valid = draft && draft.name.trim() && draft.name.length <= 100 && start !== null && end !== null && start < end && end <= session.duration_sec
  const button = 'rounded-md border border-line px-2 py-1 text-xs text-muted hover:text-ink disabled:opacity-40'
  const input = 'rounded-md border border-line bg-bg px-2 py-1 text-xs text-ink focus:border-accent focus:outline-none'

  async function persist(next: SessionCut[]) {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(null)
    try {
      const result = await call<{ cuts: SessionCut[] }>('save_session_cuts', { path: session.file_path, cuts: next, expected_cuts: cuts })
      onSaved(result.cuts)
      setDraft(null)
    } catch (err) {
      setError(friendlyError(err instanceof Error ? err.message : String(err), t.errors))
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  return <section aria-label={`${m.cuts} ${session.session_name || session.file_name}`} className="mpi-panel p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-xs font-semibold text-ink">{m.cuts} · {session.session_name || session.app_label || session.file_name}</span>
      <button type="button" className={button} disabled={busy || cuts.length >= 200} onClick={() => {
        setError(null)
        setDraft({ id: crypto.randomUUID(), name: `${m.cutDefault} ${cuts.length + 1}`, start: formatSessionRangeTime(range?.start ?? 0), end: formatSessionRangeTime(range?.end ?? session.duration_sec) })
      }}>{m.addCut}</button>
    </div>
    <p className="mt-2 text-xs leading-relaxed text-muted">{m.cutHint}</p>
    <div className="mt-2 flex flex-col gap-1.5">
      {cuts.map(cut => <div key={cut.id} className="flex flex-wrap items-center gap-2 text-xs">
        <span className="min-w-0 flex-1 break-all text-ink">{cut.name}</span>
        <span className="font-mono text-muted">{formatSessionRangeTime(cut.start_sec)}–{formatSessionRangeTime(cut.end_sec)}</span>
        <button type="button" className={button} disabled={busy} onClick={() => onAnalyze(cut)}>{m.analyzeCut}</button>
        <button type="button" className={button} disabled={busy} aria-label={`${m.editCut} ${cut.name}`} onClick={() => setDraft({ id: cut.id, name: cut.name, start: formatSessionRangeTime(cut.start_sec), end: formatSessionRangeTime(cut.end_sec) })}>{m.editCut}</button>
        <button type="button" className={button} disabled={busy} aria-label={`${m.deleteCut} ${cut.name}`} onClick={() => void persist(cuts.filter(c => c.id !== cut.id))}>{m.deleteCut}</button>
      </div>)}
    </div>
    {draft && <form className="mt-3 flex flex-wrap items-center gap-2" onSubmit={event => {
      event.preventDefault()
      if (!valid || start === null || end === null) return
      const cut = { id: draft.id, name: draft.name.trim(), start_sec: start, end_sec: end }
      void persist(cuts.some(c => c.id === cut.id) ? cuts.map(c => c.id === cut.id ? cut : c) : [...cuts, cut])
    }}>
      <input autoFocus aria-label={m.cutName} maxLength={100} className={`${input} min-w-24 flex-1`} value={draft.name} disabled={busy} onChange={e => setDraft({ ...draft, name: e.target.value })} />
      <input aria-label={m.cutStart} placeholder="0:00" className={`${input} w-24 font-mono`} value={draft.start} disabled={busy} onChange={e => setDraft({ ...draft, start: e.target.value })} />
      <span className="text-faint">–</span>
      <input aria-label={m.cutEnd} placeholder="1:00" className={`${input} w-24 font-mono`} value={draft.end} disabled={busy} onChange={e => setDraft({ ...draft, end: e.target.value })} />
      <button type="submit" className={button} disabled={busy || !valid}>{m.saveCut}</button>
      <button type="button" className={button} disabled={busy} onClick={() => setDraft(null)}>{m.cancel}</button>
      {!valid && <p role="status" className="basis-full text-xs text-rec">{m.invalidCut}</p>}
    </form>}
    {error && <p role="alert" className="mt-2 text-xs text-rec">{error}</p>}
  </section>
}
