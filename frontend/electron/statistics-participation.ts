import fs from 'node:fs'
import path from 'node:path'
type ObjectValue = Record<string, unknown>
const object = (v: unknown): ObjectValue | null => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as ObjectValue : null
export function statisticsCommand(value: unknown): ObjectValue {
 const c = object(value)
 if (!c || Object.keys(c).some(k => !['action','request_id','expected_revision','notice_version',...(c.action === 'set_preference' ? ['preference'] : [])].includes(k)) ||
  !['set_preference','grant_consent','withdraw_consent'].includes(String(c.action)) || typeof c.request_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(c.request_id) ||
  !Number.isSafeInteger(c.expected_revision) || Number(c.expected_revision) < 0 || typeof c.notice_version !== 'string' || !/^[A-Za-z0-9._-]{1,96}$/.test(c.notice_version) ||
  (c.action === 'set_preference' && !['inherit','include','exclude'].includes(String(c.preference)))) throw Error('invalid-statistics-command')
 return c
}
export function statisticsState(value: unknown): ObjectValue {
 const s = object(value)
 if (!s || s.schema_version !== 1 || s.purpose !== 'cross_user_statistics' || typeof s.available !== 'boolean' ||
  !['inherit','include','exclude'].includes(String(s.preference)) || !['unknown','granted','withdrawn'].includes(String(s.consent_state)) ||
  !['unresolved','permitted_without_consent','prior_consent_required','not_permitted'].includes(String(s.legal_gate)) ||
  !Number.isSafeInteger(s.revision) || Number(s.revision) < 0 || typeof s.effective_inclusion !== 'boolean' || typeof s.prompt_required !== 'boolean' ||
  s.personal_analysis_requires_participation !== false) throw Error('invalid-statistics-response')
 if (s.current_notice !== null) {
  const n = object(s.current_notice)
  if (!n || typeof n.version !== 'string' || !/^[A-Za-z0-9._-]{1,96}$/.test(n.version) || typeof n.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(n.sha256) ||
   typeof n.content !== 'string' || !n.content || n.content.length > 32000 || typeof n.privacy_url !== 'string' || !/^\/legal\/[a-z/-]+$/.test(n.privacy_url)) throw Error('invalid-statistics-response')
 }
 return s
}
/** Only preferences and request IDs are durable; credentials never enter this file.
 * Pending writes never migrate to another account and are retried only on request. */
export class StatisticsParticipationClient {
 private pending = new Map<string, ObjectValue>()
 private cache = new Map<string, ObjectValue>()
 private busy = new Set<string>()
 constructor(private file: string, private owner: () => string | null,
  private request: (body?: unknown) => Promise<{status: number; body: unknown}>) {
  if (fs.existsSync(file)) {
   const rows: unknown = JSON.parse(fs.readFileSync(file, 'utf8'))
   if (!Array.isArray(rows) || rows.length > 100) throw Error('statistics-storage-unavailable')
   for (const row of rows) { const r = object(row); if (!r || typeof r.owner !== 'string' || !/^[a-f0-9-]{36}$/i.test(r.owner)) throw Error('statistics-storage-unavailable'); this.pending.set(r.owner, statisticsCommand(r.command)) }
  }
 }
 private persist() {
  fs.mkdirSync(path.dirname(this.file), {recursive:true})
  const tmp = this.file + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify([...this.pending].map(([owner,command]) => ({owner,command}))), {mode:0o600})
  fs.renameSync(tmp,this.file)
 }
 private result(id: string, error: string | null = null) {
  const pending = this.pending.get(id) ?? null
  const cached = this.cache.get(id) ?? null
  // A network failure never presents stale ON as an acknowledged current state.
  const state = cached && (pending || error) ? {...cached,effective_inclusion:false} : cached
  return {user_id:id,state,pending,error}
 }
 async invoke(input: unknown, mutate = false) {
  const data = object(input), id = this.owner()
  if (!id || data?.user_id !== id) throw Error('account-changed')
  if (this.busy.has(id)) throw Error('statistics-busy')
  this.busy.add(id)
  try {
   let command: ObjectValue | undefined
   if (mutate) {
    if (data.retry === true) { command = this.pending.get(id); if (!command) throw Error('no-pending-statistics-request') }
    else { command = statisticsCommand(data.command); this.pending.set(id,command); this.persist() }
   }
   try {
    const response = await this.request(command)
    if (id !== this.owner()) throw Error('account-changed')
    if (response.status !== 200) {
     if (response.status === 409) {
      // Recover the authoritative revision, but never silently re-grant or replay a
      // different choice. Keep pending until a fresh explicit decision replaces it.
      const fresh = await this.request()
      if (id !== this.owner()) throw Error('account-changed')
      if (fresh.status === 200) this.cache.set(id,statisticsState(fresh.body))
     }
     return this.result(id,response.status === 409 ? 'statistics-conflict' : 'statistics-unavailable')
    }
    const state = statisticsState(response.body)
    if (Number(state.revision) < Number(this.cache.get(id)?.revision ?? 0)) throw Error('statistics-stale-response')
    this.cache.set(id,state)
    if (command && this.pending.get(id)?.request_id === command.request_id) { this.pending.delete(id); this.persist() }
    return this.result(id)
   } catch (e) {
    if (id !== this.owner() || (e instanceof Error && e.message === 'account-changed')) throw Error('account-changed')
    return this.result(id,'statistics-offline-pending')
   }
  } finally { this.busy.delete(id) }
 }
}
