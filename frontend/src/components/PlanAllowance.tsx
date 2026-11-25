import { useEffect, useState } from 'react'
import { useMessages } from '../lib/i18n'
import { onCloudStatus } from '../lib/cloud-sync'

interface Allowance { user_id: string; plan: string; remaining_seconds: number | null; time_limit_enabled?: boolean; storage_remaining: number | null; storage_unlimited: boolean; cloud_storage: boolean; pooled: boolean; team_name?: string | null;
  storage_bytes?: { status: 'available' | 'unavailable'; used: number | null; capacity: number | null; remaining: number | null; unlimited: boolean } }

function storageSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const divisor = bytes >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2
  return `${Number((bytes / divisor).toFixed(2))} ${divisor === 1024 ** 3 ? 'GiB' : 'MiB'}`
}

/** Informational view only; the main process reserves fresh server quota for
 * every capture and never trusts this periodically refreshed presentation. */
export function PlanAllowance({ onOpenTeam }: { onOpenTeam?: () => void }) {
  const t = useMessages()
  const [value, setValue] = useState<Allowance | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let disposed = false
    let active = true
    let request = 0
    let owner: string | null = null
    async function refresh() {
      if (!active || !window.api) return
      const version = ++request
      try {
        const next = await window.api.invoke<Allowance>('cloud-entitlement')
        if (disposed || version !== request || !active || (owner && next.user_id !== owner)) return
        owner = next.user_id
        setValue(next); setFailed(false)
      } catch { if (!disposed && version === request) { setValue(null); setFailed(true) } }
    }
    const unsubscribe = onCloudStatus((status) => {
      const permitted = status.signedIn && status.verified === true
      if (!permitted || (owner && status.userId !== owner)) {
        request += 1; setValue(null); active = false
      }
      if (permitted && !active) { active = true; owner = status.userId; void refresh() }
    })
    void refresh()
    const timer = window.setInterval(() => void refresh(), 30_000)
    return () => { disposed = true; unsubscribe(); window.clearInterval(timer) }
  }, [])
  return <div role="status" className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line bg-bg px-5 py-2.5 text-xs tabular-nums text-muted">
    {!value ? <span>{failed ? t.cloud.quotaUnavailable : t.cloud.quotaLoading}</span> : <>
      <span className="font-semibold text-ink">{value.plan}{value.pooled && value.team_name ? ` · ${value.team_name}` : ''}</span>
      {value.time_limit_enabled !== false && value.remaining_seconds !== null && <span>{t.cloud.remainingTime}: {Math.floor(value.remaining_seconds / 3600)}:{String(Math.floor(value.remaining_seconds % 3600 / 60)).padStart(2, '0')}:{String(Math.floor(value.remaining_seconds % 60)).padStart(2, '0')}</span>}
      <span>{value.cloud_storage ? `${t.cloud.storageSlots}: ${value.storage_unlimited ? t.cloud.unlimited : value.storage_remaining ?? '—'}` : t.cloud.localStorageOnly}</span>
      {value.cloud_storage && (value.plan.toLowerCase() === 'team' || value.pooled) && onOpenTeam && <button className="rounded px-1.5 font-semibold text-accent" type="button" onClick={onOpenTeam}>{t.cloud.teamCloud}</button>}
      {value.cloud_storage && value.storage_bytes?.capacity != null && <span>{t.cloud.storageCapacity}: {value.storage_bytes.status === 'available' && value.storage_bytes.used != null ? storageSize(value.storage_bytes.used) : '—'} / {storageSize(value.storage_bytes.capacity)}</span>}
    </>}
  </div>
}
