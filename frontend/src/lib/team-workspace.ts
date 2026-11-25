const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
const record = (input: unknown): Record<string, unknown> | null =>
  input && typeof input === 'object' && !Array.isArray(input) ? input as Record<string, unknown> : null
const shortText = (input: unknown): string | null =>
  typeof input === 'string' && input.trim() ? input.trim().slice(0, 512) : null

export interface TeamRow {
  id: string
  ownerId: string
  ownerLabel: string | null
  source: 'desktop' | 'android_companion'
  title: string | null
}
export interface TeamPage { teamId: string; items: TeamRow[]; next: string | null }

export function decodeTeamPage(input: unknown, viewerId: string): TeamPage | null {
  const value = record(input)
  if (!value || value.contract_version !== 1 || value.viewer_id !== viewerId ||
      typeof value.team_id !== 'string' || !uuid.test(value.team_id) ||
      !Array.isArray(value.items) || value.items.length > 250 ||
      (value.next !== null && (typeof value.next !== 'string' || !uuid.test(value.next)))) return null
  const keys = new Set<string>()
  const items: TeamRow[] = []
  for (const candidate of value.items) {
    const row = record(candidate)
    const payload = record(row?.payload)
    if (!row || !payload || typeof row.record_id !== 'string' || !uuid.test(row.record_id) || keys.has(row.record_id) ||
        typeof row.owner_id !== 'string' || !uuid.test(row.owner_id) ||
        (row.source !== 'desktop' && row.source !== 'android_companion')) return null
    keys.add(row.record_id)
    items.push({
      id: row.record_id,
      ownerId: row.owner_id,
      ownerLabel: shortText(row.owner_label),
      source: row.source,
      title: shortText(payload.title) ?? shortText(payload.name) ?? shortText(record(payload.metadata)?.label),
    })
  }
  return { teamId: value.team_id, items, next: value.next }
}

export interface TeamDetail {
  title: string | null
  durationSec: number | null
  averageFps: number | null
  device: string | null
}

export function decodeTeamDetail(input: unknown, viewerId: string, teamId: string, item: TeamRow): TeamDetail | null {
  const value = record(input)
  if (!value || value.contract_version !== 1 || value.viewer_id !== viewerId || value.team_id !== teamId ||
      value.record_id !== item.id || value.owner_id !== item.ownerId || value.source !== item.source) return null
  const payload = record(value.payload)
  if (!payload) return null
  const metadata = record(payload.metadata)
  const summary = record(payload.summary)
  const averages = record(summary?.averages)
  const nonnegative = (n: unknown) => typeof n === 'number' && Number.isFinite(n) && n >= 0 ? n : null
  return {
    title: shortText(metadata?.label) ?? item.title,
    durationSec: nonnegative(payload.duration_sec),
    averageFps: nonnegative(averages?.fps),
    device: shortText(metadata?.device_name),
  }
}
