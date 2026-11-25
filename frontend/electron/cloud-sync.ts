import fs from 'node:fs'
import { StatisticsParticipationClient } from './statistics-participation'
import path from 'node:path'
import { createHash, randomBytes } from 'node:crypto'
import { startOAuthLoopback, type OAuthLoopback, type OAuthCallbackResult } from './oauth-loopback'
import type { CompanionCloudSession } from './companion-cloud'
import { cloudProvider, type CloudProvider } from './cloud-provider'

export const MPI_API_ORIGIN = 'https://mpinspect.com'
const REQUEST_TIMEOUT_MS = 15_000

interface AuthSession {
  userId: string
  email: string
  accessToken: string
  refreshToken: string
  expiresAt: number
}

interface SessionRow {
  statistics_visibility?: 'private_only'
  user_id: string
  session_id: string
  source: 'desktop'
  name: string | null
  metadata: Record<string, unknown>
  summary: Record<string, unknown>
  time_series: unknown[]
  sample_count: number
  duration_sec: number
}

export interface CloudStatus {
  available: boolean
  signedIn: boolean
  verified: boolean
  signingIn: boolean
  userId: string | null
  email: string | null
  enabled: boolean
  pending: number
  otherAccountPending: number
  syncing: boolean
  lastSyncedAt: string | null
  error: string | null
}

export interface CaptureReservation {
  reservation_id: string
  user_id: string
  max_duration_sec: number
  expires_at: string
}

export interface DesktopEntitlement {
  user_id: string
  plan: string
  remaining_seconds: number | null
  time_limit_enabled?: boolean
  storage_remaining: number | null
  storage_unlimited: boolean
  cloud_storage: boolean
  pooled: boolean
  storage_bytes?: {
    status: 'available' | 'unavailable'
    used: number | null
    capacity: number | null
    remaining: number | null
    unlimited: boolean
  }
  team_name?: string | null
}

interface PendingFinish { reservationId: string; userId: string; outcome: 'completed' | 'cancelled' }

interface SecureStorage {
  isEncryptionAvailable(): boolean
  encryptString(value: string): Buffer
  decryptString(value: Buffer): string
}

interface RequestResult {
  status: number
  body: unknown
}

type Request = (url: string, headers: Record<string, string>, body: unknown, method?: 'POST' | 'GET') => Promise<RequestResult>

interface OAuthOptions {
  openExternal(url: string): Promise<void>
  callbackPort?: number
  timeoutMs?: number
}

interface OAuthAttempt {
  generation: number
  syncGeneration: number
  verifier: string
  state: string
  loopback?: OAuthLoopback
}

class CloudFailure extends Error {}

/** HTTPS only, bounded even when the server streams a response indefinitely.
 * Raw network/server errors are deliberately never sent over renderer IPC. */
export const cloudRequest: Request = async (url, headers, body, method = 'POST') => {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch(url, {
      method,
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: method === 'GET' ? undefined : JSON.stringify(body),
      signal: controller.signal,
      redirect: 'error',
    })
    const chunks: Uint8Array[] = []
    let size = 0
    const reader = response.body?.getReader()
    if (reader) {
      for (;;) {
        const part = await reader.read()
        if (part.done) break
        size += part.value.byteLength
        if (size > 1_048_576) {
          await reader.cancel()
          throw new CloudFailure('network')
        }
        chunks.push(part.value)
      }
    }
    const text = Buffer.concat(chunks).toString('utf8')
    let parsed: unknown = null
    if (text && text.length <= 1_048_576) {
      try { parsed = JSON.parse(text) } catch { /* A successful upsert has no body. */ }
    }
    return { status: response.status, body: parsed }
  } catch {
    throw new CloudFailure('network')
  } finally {
    clearTimeout(timer)
  }
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function atomicWrite(file: string, value: string): void {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const temp = `${file}.tmp`
  const fd = fs.openSync(temp, 'w', 0o600)
  try {
    fs.writeFileSync(fd, value, 'utf8')
    fs.fsyncSync(fd)
  } finally {
    fs.closeSync(fd)
  }
  fs.renameSync(temp, file)
}

function rowKey(row: SessionRow): string {
  return createHash('sha256').update(`${row.user_id}:${row.session_id}:desktop`).digest('hex')
}

/** Upgrade only this Cloudflare origin's on-disk schema. Keep originals intact;
 * the durable marker prevents old credentials returning after a later logout. */
function upgradeCloudStorage(directory: string, secure: SecureStorage): void {
  const marker = path.join(directory, 'storage-schema.json')
  if (fs.existsSync(marker)) return
  const entries = fs.existsSync(directory) ? fs.readdirSync(directory, { withFileTypes: true }) : []
  const auth = entries.filter(entry => entry.name.endsWith('-auth.json') && entry.name !== 'cloud-auth.json')
  const outboxes = entries.filter(entry => entry.name.endsWith('-outbox') && entry.name !== 'cloud-outbox')
  if (auth.length > 1 || outboxes.length > 1 ||
      auth.some(entry => !entry.isFile()) || outboxes.some(entry => !entry.isDirectory()) ||
      (auth[0] && outboxes[0] && auth[0].name.slice(0, -10) !== outboxes[0].name.slice(0, -7))) {
    throw new CloudFailure('local-storage')
  }
  const copies: { from: string; to: string }[] = []
  if (auth[0]) {
    if (!secure.isEncryptionAvailable()) throw new CloudFailure('secure-storage-unavailable')
    const from = path.join(directory, auth[0].name)
    const stored = object(JSON.parse(fs.readFileSync(from, 'utf8')))
    if (typeof stored?.encrypted !== 'string') throw new CloudFailure('local-storage')
    const decoded = object(JSON.parse(secure.decryptString(Buffer.from(stored.encrypted, 'base64'))))
    const session = object(decoded?.session)
    if (!session || typeof session.userId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(session.userId) ||
        typeof session.email !== 'string' || typeof session.accessToken !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(session.accessToken) ||
        typeof session.refreshToken !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(session.refreshToken) ||
        typeof session.expiresAt !== 'number' || !Number.isFinite(session.expiresAt) || typeof decoded?.enabled !== 'boolean') {
      throw new CloudFailure('local-storage')
    }
    copies.push({ from, to: path.join(directory, 'cloud-auth.json') })
  }
  if (outboxes[0]) {
    const fromDirectory = path.join(directory, outboxes[0].name)
    for (const entry of fs.readdirSync(fromDirectory, { withFileTypes: true })) {
      if (!entry.isFile() || !/^[a-f0-9]{64}\.json$/.test(entry.name)) throw new CloudFailure('local-storage')
      const from = path.join(fromDirectory, entry.name)
      const row = JSON.parse(fs.readFileSync(from, 'utf8')) as SessionRow
      if (row?.source !== 'desktop' || typeof row.user_id !== 'string' || typeof row.session_id !== 'string' ||
          !Array.isArray(row.time_series) || `${rowKey(row)}.json` !== entry.name) throw new CloudFailure('local-storage')
      copies.push({ from, to: path.join(directory, 'cloud-outbox', entry.name) })
    }
  }
  for (const { from, to } of copies) {
    if (fs.existsSync(to)) continue
    atomicWrite(to, fs.readFileSync(from, 'utf8'))
  }
  atomicWrite(marker, JSON.stringify({ version: 2 }))
}

function cloudflareSession(body: unknown, now: number): AuthSession {
  const data = object(body)
  if (!data || typeof data.userId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(data.userId) ||
      data.clientKind !== 'desktop' || typeof data.accessToken !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(data.accessToken) ||
      typeof data.refreshToken !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(data.refreshToken) ||
      typeof data.accessExpiresAt !== 'number' || !Number.isSafeInteger(data.accessExpiresAt) || data.accessExpiresAt * 1000 <= now ||
      typeof data.refreshExpiresAt !== 'number' || !Number.isSafeInteger(data.refreshExpiresAt) || data.refreshExpiresAt < data.accessExpiresAt) throw new CloudFailure('auth-failed')
  return { userId: data.userId, email: typeof data.email === 'string' ? data.email : '', accessToken: data.accessToken,
    refreshToken: data.refreshToken, expiresAt: data.accessExpiresAt * 1000 }
}

/** Owns secrets and all account decisions in Electron main. Outbox files contain
 * their original owner and are only drained while that same owner is signed in. */
export class CloudSync {
  private session: AuthSession | null = null
  private verified = false
  private enabled = false
  private syncing = false
  private lastSyncedAt: string | null = null
  private error: string | null = null
  private quotaPause: { userId: string; error: string } | null = null
  private unreadableOutbox = false
  private rows = new Map<string, SessionRow>()
  private lifecycle: Promise<unknown> = Promise.resolve()
  private retryQueued = false
  private authGeneration = 0
  private sessionGeneration = 0
  private syncGeneration = 0
  private retryTimer: ReturnType<typeof setInterval> | null = null
  private oauthAttempt: OAuthAttempt | null = null
  private readonly authFile: string
  private readonly outboxDir: string
  private readonly finishesFile: string
  private readonly pendingFinishes = new Map<string, PendingFinish>()
  private delegated: { generation: number; session: CompanionCloudSession } | null = null
  private delegating: Promise<CompanionCloudSession | null> | null = null

  constructor(
    directory: string,
    private readonly secure: SecureStorage,
    private readonly onStatus: (status: CloudStatus) => void = () => {},
    private readonly request: Request = cloudRequest,
    private readonly now: () => number = Date.now,
    private readonly oauth?: OAuthOptions,
    private readonly provider: CloudProvider = cloudProvider(),
  ) {
    directory = path.join(directory, provider.storageNamespace)
    this.authFile = path.join(directory, 'cloud-auth.json')
    this.outboxDir = path.join(directory, 'cloud-outbox')
    this.finishesFile = path.join(directory, 'capture-finishes.json')
    try { upgradeCloudStorage(directory, secure) } catch (error) { this.fail(error) }
    this.restore()
  }

  private restore(): void {
    try {
      if (fs.existsSync(this.finishesFile)) {
        const entries: unknown = JSON.parse(fs.readFileSync(this.finishesFile, 'utf8'))
        if (!Array.isArray(entries)) throw new Error('invalid pending completions')
        for (const entry of entries) {
          if (typeof entry?.reservationId === 'string' && typeof entry.userId === 'string' &&
              (entry.outcome === 'completed' || entry.outcome === 'cancelled')) {
            this.pendingFinishes.set(entry.reservationId, entry)
          }
        }
      }
    } catch { this.error = 'local-storage' }
    try {
      if (fs.existsSync(this.authFile)) {
        if (!this.secure.isEncryptionAvailable()) throw new CloudFailure('secure-storage-unavailable')
        const stored = object(JSON.parse(fs.readFileSync(this.authFile, 'utf8')))
        if (typeof stored?.encrypted !== 'string') throw new CloudFailure('local-storage')
        const decoded = object(JSON.parse(this.secure.decryptString(Buffer.from(stored.encrypted, 'base64'))))
        const session = object(decoded?.session)
        if (
          session && typeof session.userId === 'string' && typeof session.email === 'string' &&
          typeof session.accessToken === 'string' && typeof session.refreshToken === 'string' &&
          typeof session.expiresAt === 'number'
        ) {
          this.session = session as unknown as AuthSession
          this.enabled = decoded?.enabled === true
          const pause = object(decoded?.quotaPause)
          if (pause?.userId === this.session.userId &&
              (pause.error === 'cloud-storage-full' || pause.error === 'cloud-storage-quota-unknown')) {
            this.quotaPause = { userId: this.session.userId, error: pause.error }
          }
        } else throw new CloudFailure('local-storage')
      }
    } catch (error) {
      this.fail(error)
    }
    try {
      if (fs.existsSync(this.outboxDir)) {
        for (const file of fs.readdirSync(this.outboxDir)) {
          if (!/^[a-f0-9]{64}\.json$/.test(file)) continue
          try {
          const row = JSON.parse(fs.readFileSync(path.join(this.outboxDir, file), 'utf8')) as SessionRow
          if (row?.source === 'desktop' && typeof row.user_id === 'string' &&
              typeof row.session_id === 'string' && Array.isArray(row.time_series) &&
              `${rowKey(row)}.json` === file) this.rows.set(rowKey(row), row)
          else this.unreadableOutbox = true
          } catch {
            // Keep a corrupt file for recovery while loading every healthy row.
            this.unreadableOutbox = true
          }
        }
      }
    } catch (error) {
      this.fail(error)
    }
  }

  start(): void {
    this.retryTimer = setInterval(() => {
      void this.run(() => this.pollReplacement())
      void this.queueRetry()
    }, 30_000)
    this.retryTimer.unref()
    void this.queueRetry()
  }

  stop(): void {
    if (this.retryTimer) clearInterval(this.retryTimer)
    this.retryTimer = null
    this.cancelSignIn()
  }

  status(): CloudStatus {
    let pending = 0
    for (const row of this.rows.values()) if (row.user_id === this.session?.userId) pending += 1
    return {
      available: this.secure.isEncryptionAvailable(),
      signedIn: this.session !== null,
      verified: this.verified,
      signingIn: this.oauthAttempt !== null,
      userId: this.session?.userId ?? null,
      email: this.session?.email ?? null,
      enabled: this.enabled,
      pending,
      otherAccountPending: this.rows.size - pending,
      syncing: this.syncing,
      lastSyncedAt: this.lastSyncedAt,
      error: this.error ?? (this.quotaPause?.userId === this.session?.userId ? this.quotaPause?.error : null)
        ?? (this.unreadableOutbox ? 'local-storage' : null),
    }
  }

  private rejectReplacement(response: RequestResult, generation: number): void {
    if (generation !== this.authGeneration || response.status !== 401 || object(response.body)?.error !== 'session_replaced') return
    this.authGeneration += 1
    this.syncGeneration += 1
    this.session = null
    this.verified = false
    this.enabled = false
    this.delegated = null
    this.error = 'session-replaced'
    try { fs.rmSync(this.authFile, { force: true }) } catch { /* Current process must stay signed out. */ }
    this.publish()
    throw new CloudFailure('session-replaced')
  }

  private async pollReplacement(): Promise<void> {
    const session = this.session
    const generation = this.authGeneration
    if (!session || this.sessionGeneration !== generation || !this.verified || this.oauthAttempt) return
    try {
      // Check an already verified login even while cloud upload is disabled.
      // A network error or any untyped denial is not a replacement.
      await this.refresh()
      if (generation !== this.authGeneration || !this.session) return
      const response = await this.request(`${this.provider.apiOrigin}/cf-auth/session`, {
        Authorization: `Bearer ${this.session.accessToken}`,
      }, undefined, 'GET')
      this.rejectReplacement(response, generation)
    } catch (error) {
      // Typed replacement was already published. Preserve the ordinary expiry
      // error, but don't log out on failures that leave a valid session intact.
      if (!this.session && this.error !== 'session-replaced') this.fail(error)
    }
  }

  /** A restored credential is not evidence of a valid online sign-in. */
  verifySession(): Promise<CloudStatus> {
    const generation = this.authGeneration
    return this.run(async () => {
      this.verified = false
      if (!this.session) return
      await this.refresh()
      if (!this.session || generation !== this.authGeneration) return
      const response = await this.request(`${this.provider.apiOrigin}/cf-auth/session`, {
        Authorization: `Bearer ${this.session.accessToken}`,
      }, undefined, 'GET')
      if (generation !== this.authGeneration) return
      this.rejectReplacement(response, generation)
      if (response.status === 401 || response.status === 403) {
        this.session = null
        this.enabled = false
        fs.rmSync(this.authFile, { force: true })
        throw new CloudFailure('auth-required')
      }
      if (response.status !== 200) throw new CloudFailure('network')
      if (object(response.body)?.userId !== this.session.userId) throw new CloudFailure('account-changed')
      if (object(response.body)?.clientKind !== 'desktop') throw new CloudFailure('auth-required')
      this.verified = true
      this.error = null
    })
  }

  private async desktopRequest(route: string, body?: unknown, method: 'POST' | 'GET' = 'POST', additionalHeaders?: Record<string, string>, allowForbidden = false): Promise<RequestResult> {
    const generation = this.authGeneration
    const session = this.session
    if (!session || this.sessionGeneration !== generation) throw new CloudFailure('auth-required')
    const response = await this.request(`${this.provider.apiOrigin}${route}`, { ...additionalHeaders, Authorization: `Bearer ${session.accessToken}` }, body, method)
    if (generation !== this.authGeneration || this.session?.userId !== session.userId) throw new CloudFailure('account-changed')
    this.rejectReplacement(response, generation)
    if (response.status === 401 || (response.status === 403 && !allowForbidden)) {
      this.verified = false
      this.error = 'auth-required'
      this.publish()
      throw new CloudFailure('auth-required')
    }
    return response
  }

  private statisticsClient: StatisticsParticipationClient | null = null
  async statisticsParticipation(input: unknown, mutate = false) {
    if (!await this.activeLease()) throw new CloudFailure('auth-required')
    this.statisticsClient ??= new StatisticsParticipationClient(path.join(path.dirname(this.authFile), 'statistics-pending.json'),
      () => this.sessionGeneration === this.authGeneration ? this.session?.userId ?? null : null,
      body => this.desktopRequest('/api/privacy/statistics-participation', body, body ? 'POST' : 'GET'))
    return this.statisticsClient.invoke(input, mutate)
  }

  async entitlement(): Promise<DesktopEntitlement> {
    if (!await this.activeLease()) throw new CloudFailure('auth-required')
    const response = await this.desktopRequest('/api/desktop/entitlement', undefined, 'GET')
    const body = object(response.body)
    if (response.status !== 200 || body?.user_id !== this.session?.userId || typeof body?.plan !== 'string' ||
        (body.time_limit_enabled !== undefined && typeof body.time_limit_enabled !== 'boolean') ||
        (body.time_limit_enabled !== false && (typeof body.remaining_seconds !== 'number' || !Number.isFinite(body.remaining_seconds) || body.remaining_seconds < 0)) ||
        typeof body.storage_unlimited !== 'boolean' || typeof body.cloud_storage !== 'boolean' ||
        !(body.storage_remaining === null || (typeof body.storage_remaining === 'number' && Number.isSafeInteger(body.storage_remaining) && body.storage_remaining >= 0))) {
      throw new CloudFailure('quota-unavailable')
    }
    return body as unknown as DesktopEntitlement
  }

  /** Expose a read-only Team list/detail through narrow IPC, never credentials. */
  async teamWorkspace(input: unknown): Promise<unknown> {
    const lease = await this.activeLease()
    if (!lease || !this.verified) throw new CloudFailure('auth-required')
    const args = object(input)
    const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
    const id = args?.recordId, after = args?.after
    if ((id !== undefined && (typeof id !== 'string' || !uuid.test(id))) ||
        (after !== undefined && (typeof after !== 'string' || !uuid.test(after))) ||
        (id !== undefined && after !== undefined)) throw new CloudFailure('invalid-payload')
    const root = '/api/team/cloud/sessions'
    const route = id ? `${root}/${encodeURIComponent(id as string)}` : after ? `${root}?after=${encodeURIComponent(after as string)}` : root
    const response = await this.desktopRequest(route, undefined, 'GET', { 'X-MPI-Native-Contract': 'team-cloud-v1' }, true)
    const data = object(response.body)
    if (response.status !== 200 || data?.contract_version !== 1 || data.viewer_id !== lease.user_id ||
        typeof data.team_id !== 'string' || !uuid.test(data.team_id)) throw new CloudFailure('team-cloud-unavailable')
    return data
  }

  async reserveCapture(sessionId: string, seconds: number): Promise<CaptureReservation> {
    if (!await this.activeLease()) throw new CloudFailure('auth-required')
    const response = await this.desktopRequest('/api/desktop/captures', { session_id: sessionId, requested_seconds: seconds })
    if (response.status === 409 || response.status === 402) throw new CloudFailure('quota-exhausted')
    const body = object(response.body)
    if (response.status < 200 || response.status >= 300 || !body ||
        typeof body.reservation_id !== 'string' || !/^[a-zA-Z0-9_-]{1,200}$/.test(body.reservation_id) ||
        body.user_id !== this.session?.userId || typeof body.max_duration_sec !== 'number' ||
        !Number.isFinite(body.max_duration_sec) || body.max_duration_sec < 1 || body.max_duration_sec > seconds ||
        typeof body.expires_at !== 'string' || !Number.isFinite(Date.parse(body.expires_at)) || Date.parse(body.expires_at) <= this.now()) {
      throw new CloudFailure('quota-unavailable')
    }
    this.verified = true
    return body as unknown as CaptureReservation
  }

  async finishCapture(reservation: CaptureReservation, outcome: 'completed' | 'cancelled'): Promise<void> {
    // Durably retain completion before networking; retry only with its owner.
    this.pendingFinishes.set(reservation.reservation_id, { reservationId: reservation.reservation_id, userId: reservation.user_id, outcome })
    atomicWrite(this.finishesFile, JSON.stringify([...this.pendingFinishes.values()]))
    await this.flushFinishes()
  }

  async startCaptureReservation(reservation: CaptureReservation): Promise<CaptureReservation> {
    const response = await this.desktopRequest(`/api/desktop/captures/${encodeURIComponent(reservation.reservation_id)}/start`, {})
    const body = object(response.body)
    if (response.status < 200 || response.status >= 300 || !body || body.reservation_id !== reservation.reservation_id ||
        body.user_id !== reservation.user_id || typeof body.max_duration_sec !== 'number' || !Number.isFinite(body.max_duration_sec) ||
        body.max_duration_sec < 1 || body.max_duration_sec > reservation.max_duration_sec ||
        typeof body.expires_at !== 'string' || !Number.isFinite(Date.parse(body.expires_at)) || Date.parse(body.expires_at) <= this.now()) {
      throw new CloudFailure('quota-unavailable')
    }
    return body as unknown as CaptureReservation
  }

  private async flushFinishes(): Promise<void> {
    for (const [id, item] of this.pendingFinishes) {
      if (item.userId !== this.session?.userId) continue
      try {
        const response = await this.desktopRequest(`/api/desktop/captures/${encodeURIComponent(id)}/finish`, { outcome: item.outcome })
        if (response.status < 200 || response.status >= 300) continue
        this.pendingFinishes.delete(id)
        atomicWrite(this.finishesFile, JSON.stringify([...this.pendingFinishes.values()]))
      } catch { /* A later online retry keeps the idempotent completion. */ }
    }
  }

  /** Main-process use only. Upload preferences do not affect companion login. */
  private async activeLease(): Promise<CompanionCloudSession | null> {
    const generation = this.authGeneration
    if (!this.session || this.sessionGeneration !== generation) return null
    // A long outbox upload must not starve the mobile heartbeat. Only refreshes
    // join the serialized auth lifecycle; a currently valid token is read now.
    if (Number.isFinite(this.session.expiresAt) && this.session.expiresAt > this.now() + 60_000) {
      return {
        user_id: this.session.userId,
        access_token: this.session.accessToken,
        expires_at: Math.floor(this.session.expiresAt / 1000),
      }
    }
    let lease: CompanionCloudSession | null = null
    await this.run(async () => {
      if (!this.session || generation !== this.authGeneration || this.sessionGeneration !== generation) return
      await this.refresh()
      if (!this.session || generation !== this.authGeneration || this.sessionGeneration !== generation ||
          this.session.expiresAt <= this.now() + 5_000) return
      lease = {
        user_id: this.session.userId,
        access_token: this.session.accessToken,
        expires_at: Math.floor(this.session.expiresAt / 1000),
      }
    })
    return generation === this.authGeneration ? lease : null
  }

  async companionSession(): Promise<CompanionCloudSession | null> {
    const lease = await this.activeLease()
    if (!lease) return null
    const generation = this.authGeneration
    if (this.delegated?.generation === generation && this.delegated.session.expires_at > this.now() / 1000 + 60) return this.delegated.session
    if (this.delegating) return this.delegating
    this.delegating = (async () => {
      const response = await this.desktopRequest('/cf-auth/companion-token', {})
      if (generation !== this.authGeneration) return null
      const data = object(response.body)
      if (response.status !== 200 || data?.user_id !== lease.user_id || typeof data.access_token !== 'string' ||
          !/^[A-Za-z0-9_-]{43}$/.test(data.access_token) || typeof data.expires_at !== 'number' ||
          !Number.isSafeInteger(data.expires_at) || data.expires_at <= this.now() / 1000 || data.expires_at > this.now() / 1000 + 360) throw new CloudFailure('auth-required')
      const session = { user_id: lease.user_id, access_token: data.access_token, expires_at: data.expires_at }
      this.delegated = { generation, session }
      return session
    })().finally(() => { this.delegating = null })
    return this.delegating
  }

  private parseSession(body: unknown): AuthSession { return cloudflareSession(body, this.now()) }

  private publish(): CloudStatus {
    const status = this.status()
    this.onStatus(status)
    return status
  }

  private fail(error: unknown): void {
    this.error = error instanceof CloudFailure ? error.message : 'local-storage'
  }

  private persistAuth(session: AuthSession, enabled: boolean): void {
    if (!this.secure.isEncryptionAvailable()) throw new CloudFailure('secure-storage-unavailable')
    atomicWrite(this.authFile, JSON.stringify({
      encrypted: this.secure.encryptString(JSON.stringify({ session, enabled,
        quotaPause: this.quotaPause?.userId === session.userId ? this.quotaPause : null,
      })).toString('base64'),
    }))
  }

  private run(task: () => Promise<void>): Promise<CloudStatus> {
    const operation = this.lifecycle.catch(() => {}).then(async () => {
      try { await task() } catch (error) { this.fail(error) }
      return this.publish()
    })
    this.lifecycle = operation
    return operation
  }

  async signInWithGoogle(): Promise<CloudStatus> {
    if (this.oauthAttempt) return this.status()
    const attempt: OAuthAttempt = {
      generation: ++this.authGeneration,
      syncGeneration: this.syncGeneration,
      verifier: randomBytes(32).toString('base64url'),
      state: randomBytes(32).toString('base64url'),
    }
    this.oauthAttempt = attempt
    this.error = null
    this.publish()
    try {
      if (!this.secure.isEncryptionAvailable()) throw new CloudFailure('secure-storage-unavailable')
      if (!this.oauth) throw new CloudFailure('oauth-unavailable')
      try { attempt.loopback = await startOAuthLoopback(this.oauth.callbackPort, this.oauth.timeoutMs, { path: '/cf-auth/callback', state: attempt.state }) }
      catch { throw new CloudFailure('oauth-callback-unavailable') }
      if (this.oauthAttempt !== attempt) {
        attempt.loopback.cancel()
        return this.status()
      }
      const authorize = new URL(`${this.provider.apiOrigin}/cf-auth/google/start`)
      authorize.search = new URLSearchParams({
        client: 'desktop', next: '/account', redirect_uri: attempt.loopback.redirectTo,
        challenge: createHash('sha256').update(attempt.verifier).digest('base64url'), client_state: attempt.state,
      }).toString()
      void attempt.loopback.result.then((result) => this.finishGoogleSignIn(attempt, result))
      try { await this.oauth.openExternal(authorize.toString()) }
      catch { throw new CloudFailure('browser-open-failed') }
    } catch (error) {
      if (this.oauthAttempt === attempt) {
        this.clearOAuth()
        this.fail(error instanceof CloudFailure ? error : new CloudFailure('oauth-failed'))
      }
    }
    return this.publish()
  }

  cancelSignIn(): CloudStatus {
    if (this.oauthAttempt) {
      this.authGeneration += 1
      this.clearOAuth()
      this.error = 'oauth-cancelled'
    }
    return this.publish()
  }

  private clearOAuth(): void {
    const attempt = this.oauthAttempt
    this.oauthAttempt = null
    if (attempt) {
      attempt.verifier = ''
      attempt.loopback?.cancel()
      // Cancel/error restores the still-signed-in desktop account.
      this.sessionGeneration = this.authGeneration
    }
  }

  private finishGoogleSignIn(attempt: OAuthAttempt, result: OAuthCallbackResult): Promise<CloudStatus> {
    return this.run(async () => {
      if (this.oauthAttempt !== attempt || attempt.generation !== this.authGeneration) return
      try {
        if ('error' in result) throw new CloudFailure(result.error)
        const response = await this.request(`${this.provider.apiOrigin}/cf-auth/exchange`,
          {}, { code: result.code, verifier: attempt.verifier, redirectUri: attempt.loopback!.redirectTo })
        if (this.oauthAttempt !== attempt || attempt.generation !== this.authGeneration) return
        if (response.status < 200 || response.status >= 300) throw new CloudFailure('oauth-failed')
        let session: AuthSession
        try { session = this.parseSession(response.body) }
        catch { throw new CloudFailure('oauth-failed') }
        const enabled = attempt.syncGeneration === this.syncGeneration
        this.persistAuth(session, enabled)
        this.session = session
        if (this.quotaPause?.userId !== session.userId) this.quotaPause = null
        this.verified = true
        this.sessionGeneration = attempt.generation
        this.enabled = enabled
        this.error = null
      } catch (error) {
        if (this.oauthAttempt !== attempt) return
        this.fail(error instanceof CloudFailure ? error : new CloudFailure('oauth-failed'))
        this.clearOAuth()
        return
      }
      this.clearOAuth()
      this.publish()
      await this.drain()
    })
  }

  signOut(): Promise<CloudStatus> {
    this.clearOAuth()
    // Stop the next queued upload immediately, even while one request is in flight.
    this.enabled = false
    this.verified = false
    this.authGeneration += 1
    this.syncGeneration += 1
    this.publish()
    return this.run(async () => {
      const token = this.session?.accessToken
      fs.rmSync(this.authFile, { force: true })
      this.session = null
      this.quotaPause = null
      this.enabled = false
      this.error = null
      this.publish()
      if (token) {
        // Local logout always succeeds; revocation is best effort when offline.
        try {
          await this.request(`${this.provider.apiOrigin}/cf-auth/logout`, {
            Authorization: `Bearer ${token}`,
          }, {})
        } catch { /* Encrypted credentials have already been removed. */ }
      }
    })
  }

  setEnabled(enabled: unknown): Promise<CloudStatus> {
    if (enabled === false) {
      this.syncGeneration += 1
      this.enabled = false
      this.publish()
    }
    const generation = this.syncGeneration
    return this.run(async () => {
      if (typeof enabled !== 'boolean') throw new CloudFailure('invalid-payload')
      // A later pause/logout takes effect immediately, including against an
      // enable waiting behind an in-flight network request in another window.
      if (enabled && generation !== this.syncGeneration) return
      if (!this.session) throw new CloudFailure('auth-required')
      this.persistAuth(this.session, enabled)
      this.enabled = enabled
      this.error = null
      if (enabled) await this.drain()
    })
  }

  enqueue(input: unknown): { ok: boolean; skipped?: boolean; status: CloudStatus } {
    try {
      const args = object(input)
      // A measurement started with no account stays local. Never assign an old
      // account's capture to a newly logged-in user at capture end/export time.
      if (!args?.ownerId)
        return { ok: true, skipped: true, status: this.status() }
      if (typeof args.ownerId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(args.ownerId))
        throw new CloudFailure('invalid-payload')
      const payload = object(args.payload)
      const metadata = object(payload?.metadata)
      const summary = object(payload?.summary)
      const capture = object(metadata?.capture)
      const series = object(payload?.timeseries)
      const id = args.sessionId
      if (!payload || !metadata || !summary || typeof id !== 'string' || !id || id.length > 200 ||
          !series || !Array.isArray(series.columns) || !Array.isArray(series.rows))
        throw new CloudFailure('invalid-payload')
      const duration = capture?.duration_total_sec ?? capture?.duration_sec ?? summary.duration_sec ?? 0
      const row: SessionRow = {
        statistics_visibility: 'private_only',
        user_id: args.ownerId,
        session_id: id,
        source: 'desktop',
        name: typeof args.name === 'string' ? args.name.slice(0, 512) : null,
        metadata: { ...metadata, capture: { ...capture, session_id: id }, timeseries_columns: series.columns },
        summary,
        time_series: series.rows,
        sample_count: series.rows.length,
        duration_sec: typeof duration === 'number' && Number.isFinite(duration) ? Math.max(0, duration) : 0,
      }
      const key = rowKey(row)
      // Only successful durable writes are eligible to be sent or counted.
      atomicWrite(path.join(this.outboxDir, `${key}.json`), JSON.stringify(row))
      this.rows.set(key, row)
      if (this.error !== 'session-replaced') this.error = null
      this.publish()
      void this.queueRetry()
      return { ok: true, status: this.status() }
    } catch (error) {
      this.fail(error)
      return { ok: false, status: this.publish() }
    }
  }

  retry(): Promise<CloudStatus> {
    // Only a deliberate retry (for example after upgrading or deleting cloud
    // sessions) resumes full payload uploads for this account.
    if (this.quotaPause && this.quotaPause.userId === this.session?.userId) {
      const previous = this.quotaPause
      this.quotaPause = null
      try {
        if (this.session) this.persistAuth(this.session, this.enabled)
        this.error = null
      } catch (error) {
        this.quotaPause = previous
        this.fail(error)
        return Promise.resolve(this.publish())
      }
    }
    return this.queueRetry()
  }

  private queueRetry(): Promise<CloudStatus> {
    if (this.retryQueued) return this.lifecycle.then(() => this.status())
    this.retryQueued = true
    return this.run(async () => {
      try { await this.flushFinishes(); await this.drain() } finally { this.retryQueued = false }
    })
  }

  private async refresh(force = false): Promise<void> {
    const previous = this.session
    const generation = this.authGeneration
    if (!previous) throw new CloudFailure('auth-required')
    if (!force && previous.expiresAt > this.now() + 60_000) return
    const response = await this.request(`${this.provider.apiOrigin}/cf-auth/refresh`,
      { Authorization: `Bearer ${previous.refreshToken}` }, {})
    if (generation !== this.authGeneration) return
    this.rejectReplacement(response, generation)
    if (response.status < 200 || response.status >= 300) {
      if (response.status === 400 || response.status === 401 || response.status === 403) {
        fs.rmSync(this.authFile, { force: true })
        this.session = null
        this.verified = false
        this.enabled = false
        throw new CloudFailure('auth-required')
      }
      throw new CloudFailure(`http-${response.status}`)
    }
    const next = this.parseSession(response.body)
    if (next.userId !== previous.userId) throw new CloudFailure('account-changed')
    this.persistAuth(next, this.enabled)
    this.session = next
  }

  private async upload(row: SessionRow): Promise<RequestResult> {
    if (!this.session || this.session.userId !== row.user_id) throw new CloudFailure('account-changed')
    const capabilityResponse = await this.desktopRequest('/api/privacy/statistics-participation', undefined, 'GET')
    const capability = object(object(object(capabilityResponse.body)?.capabilities)?.private_only_upload)
    if (capabilityResponse.status !== 200 || object(capabilityResponse.body)?.account_id !== row.user_id || capability?.contract_version !== 1 || capability.supported !== true ||
        capability.enforcement_version !== 'private-measurement-v1' || capability.scope !== 'immutable_measurement_private_only') throw new CloudFailure('private-upload-unavailable')
    if (!this.session || this.session.userId !== row.user_id) throw new CloudFailure('account-changed')
    // This acknowledgement belongs only to the immutable queued owner/session.
    // It may safely finish after logout; no later upload may start for that account.
    const generation = this.authGeneration
    const response = await this.request(`${this.provider.apiOrigin}/api/desktop/sessions`, { Authorization: `Bearer ${this.session.accessToken}` }, { ...row, statistics_visibility: 'private_only' })
    this.rejectReplacement(response, generation)
    if (response.status >= 200 && response.status < 300) {
      const ack = object(response.body)
      if (ack?.ok !== true || ack.session_id !== row.session_id || ack.statistics_visibility !== 'private_only' ||
          ack.statistics_enforcement_version !== 'private-measurement-v1') throw new CloudFailure('private-upload-unavailable')
    }
    return response
  }

  private async drain(): Promise<void> {
    if (!this.enabled || !this.session) return
    this.syncing = true
    this.publish()
    try {
      // Also refresh at restart/periodically while the outbox is empty so a
      // long-running app keeps its encrypted login usable for the next capture.
      await this.refresh()
      if (this.quotaPause?.userId === this.session?.userId) return
      let rowFailure: string | null = null
      for (const [key, row] of this.rows) {
        if (!this.enabled || !this.session) break
        if (row.user_id !== this.session.userId) continue
        let response = await this.upload(row)
        if (response.status === 401) {
          await this.refresh(true)
          if (!this.enabled) break
          response = await this.upload(row)
        }
        if (response.status < 200 || response.status >= 300) {
          const code = object(response.body)?.error
          const quotaUnknown = code === 'storage_quota_unknown' || code === 'storage_measurement_unavailable'
          const quotaBlocked = (response.status === 409 &&
            (code === 'storage_full' || code === 'storage_bytes_full' || quotaUnknown))
            || (response.status === 503 && quotaUnknown)
          rowFailure = quotaBlocked
            ? quotaUnknown ? 'cloud-storage-quota-unknown' : 'cloud-storage-full'
            : response.status === 413 ? 'cloud-payload-too-large' : `http-${response.status}`
          if (quotaBlocked) {
            this.quotaPause = { userId: row.user_id, error: rowFailure }
            if (this.session?.userId === row.user_id) this.persistAuth(this.session, this.enabled)
          }
          this.error = rowFailure
          this.publish()
          // Quota applies to the account. Keep every row durable and avoid
          // retransmitting full sessions every 30 seconds until user retry.
          if (quotaBlocked) break
          // A rejected row stays pending, but must not block healthy rows.
          continue
        }
        // Enqueue can replace a row during the request. Preserve that newer
        // revision for the next upsert instead of deleting its durable file.
        if (this.rows.get(key) === row) {
          fs.rmSync(path.join(this.outboxDir, `${key}.json`))
          this.rows.delete(key)
        }
        this.lastSyncedAt = new Date(this.now()).toISOString()
        this.error = rowFailure
        this.publish()
      }
    } finally {
      this.syncing = false
      this.publish()
    }
  }
}
