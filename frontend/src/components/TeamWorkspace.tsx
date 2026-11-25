import { useEffect, useRef, useState } from 'react'
import { useMessages } from '../lib/i18n'
import { onCloudStatus } from '../lib/cloud-sync'
import { decodeTeamPage, decodeTeamDetail, type TeamDetail, type TeamPage, type TeamRow } from '../lib/team-workspace'

export function TeamWorkspace({ viewerId }: { viewerId: string }) {
  const t = useMessages().cloud
  const [page, setPage] = useState<TeamPage | null>(null)
  const [detail, setDetail] = useState<TeamDetail | null>(null)
  const [failure, setFailure] = useState(false)
  const [busy, setBusy] = useState(false)
  const serial = useRef(0)

  async function load(after?: string) {
    const id = ++serial.current
    setBusy(true)
    setFailure(false)
    setDetail(null)
    try {
      const next = decodeTeamPage(await window.api?.invoke('cloud-team-workspace', after ? { after } : undefined), viewerId)
      if (id !== serial.current) return
      if (!next || (after && next.teamId !== page?.teamId)) throw new Error('team scope changed')
      setPage(!after || !page ? next : { ...next, items: [...page.items, ...next.items.filter(item => !page.items.some(old => old.id === item.id))] })
    } catch {
      if (id === serial.current) { setPage(null); setFailure(true) }
    } finally { if (id === serial.current) setBusy(false) }
  }

  async function show(item: TeamRow) {
    if (!page) return
    const id = ++serial.current
    setDetail(null)
    setBusy(true)
    setFailure(false)
    try {
      const opened = decodeTeamDetail(await window.api?.invoke('cloud-team-workspace', { recordId: item.id }), viewerId, page.teamId, item)
      if (id !== serial.current) return
      if (!opened) throw new Error('unavailable')
      setDetail(opened)
    } catch {
      if (id === serial.current) { setPage(null); setFailure(true) }
    } finally { if (id === serial.current) setBusy(false) }
  }

  useEffect(() => {
    const off = onCloudStatus(status => {
      if (status.verified !== true || !status.signedIn || status.userId !== viewerId) {
        serial.current += 1
        setPage(null)
        setDetail(null)
        setFailure(false)
        setBusy(false)
      }
    })
    return () => { serial.current += 1; off() }
  }, [viewerId])

  return <section aria-labelledby="team-cloud-heading" className="h-full overflow-auto p-6">
    <h1 id="team-cloud-heading" className="font-display text-xl font-semibold">{t.teamCloud}</h1>
    <p className="my-3 text-sm text-muted">{t.teamCloudHelp}</p>
    <button className="mpi-control px-3 text-sm" type="button" disabled={busy} onClick={() => void load()}>{t.teamCloudLoad}</button>
    {failure && <p role="alert" className="mt-3 text-danger">{t.teamCloudUnavailable}</p>}
    {page?.items.length === 0 && <p className="mt-4 text-muted">{t.teamCloudEmpty}</p>}
    <ul className="mt-4 grid gap-2">
      {page?.items.map(item => <li key={item.id} className="rounded-lg border border-line p-3"><button type="button" onClick={() => void show(item)} disabled={busy} className="block font-semibold text-accent">{item.title ?? t.teamCloudUntitled}</button><span className="text-xs text-muted">{item.ownerLabel ?? t.teamCloudMember} · {item.source === 'desktop' ? 'Desktop' : 'Android'}</span></li>)}
    </ul>
    {detail && <div className="mt-4 rounded-lg border border-line p-3" role="status"><strong>{detail.title ?? t.teamCloudUntitled}</strong><p className="text-sm text-muted">{[detail.device, detail.durationSec === null ? null : detail.durationSec.toFixed(1) + ' s', detail.averageFps === null ? null : detail.averageFps.toFixed(1) + ' FPS'].filter(Boolean).join(' · ')}</p></div>}
    {page?.next && <button className="mpi-control mt-3 px-3 text-sm" type="button" disabled={busy} onClick={() => void load(page.next!)}>{t.teamCloudMore}</button>}
  </section>
}
