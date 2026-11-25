import { randomUUID } from 'node:crypto'

/** Access-only lease payload. It is never part of renderer IPC or persisted by this bridge. */
export interface CompanionCloudSession {
  user_id: string
  access_token: string
  expires_at: number
}
export interface CompanionCloudStatus {
  connected: number
  signed_in: number
  unavailable: number
}
const INTERNAL_PREFIX = 'mpi-main-companion:'
const SYNC_COMMAND = 'sync_companion_cloud'

/** Both the command and reserved correlation IDs are unavailable to renderers. */
export function isMainOnlyCompanionRequest(input: unknown): boolean {
  if (!input || typeof input !== 'object') return false
  const args = input as { command?: unknown; id?: unknown }
  return args.command === SYNC_COMMAND || (typeof args.id === 'string' && args.id.startsWith(INTERNAL_PREFIX))
}

/** Routes secret-bearing requests only between Electron main and the owned sidecar. */
export class CompanionCloudBridge {
  private pending = new Map<string, { resolve(value: CompanionCloudStatus): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout> }>()

  constructor(
    private readonly send: (request: unknown) => boolean,
    private readonly timeoutMs = 60_000,
  ) {}

  sync(session: CompanionCloudSession | null): Promise<CompanionCloudStatus> {
    const id = INTERNAL_PREFIX + randomUUID()
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error('companion-sync-unavailable'))
      }, this.timeoutMs)
      timer.unref?.()
      this.pending.set(id, { resolve, reject, timer })
      let sent = false
      const lease = session ? {
        user_id: session.user_id, access_token: session.access_token, expires_at: session.expires_at,
      } : null
      try { sent = this.send({ id, command: SYNC_COMMAND, payload: { session: lease } }) } catch { /* No raw transport details escape main. */ }
      if (!sent) {
        clearTimeout(timer)
        this.pending.delete(id)
        reject(new Error('companion-sync-unavailable'))
      }
    })
  }

  /** Returns true for every internal ID, including late/duplicate responses. */
  consume(input: unknown): boolean {
    if (!input || typeof input !== 'object') return false
    const message = input as { id?: unknown; result?: unknown; error?: unknown }
    if (typeof message.id !== 'string' || !message.id.startsWith(INTERNAL_PREFIX)) return false
    const pending = this.pending.get(message.id)
    if (!pending) return true
    this.pending.delete(message.id)
    clearTimeout(pending.timer)
    const result = message.result as Partial<CompanionCloudStatus> | null
    if (message.error || !result || ['connected', 'signed_in', 'unavailable'].some((key) => {
      const value = result[key as keyof CompanionCloudStatus]
      return typeof value !== 'number' || !Number.isInteger(value) || value < 0
    })) pending.reject(new Error('companion-sync-unavailable'))
    else pending.resolve({ connected: result.connected!, signed_in: result.signed_in!, unavailable: result.unavailable! })
    return true
  }

  disconnect(): void {
    for (const request of this.pending.values()) {
      clearTimeout(request.timer)
      request.reject(new Error('companion-sync-unavailable'))
    }
    this.pending.clear()
  }
}

/** One heartbeat at a time; changed auth invalidates any access token still being fetched. */
export class CompanionCloudCoordinator {
  private timer: ReturnType<typeof setInterval> | null = null
  private running: Promise<void> | null = null
  private generation = 0
  private rerun = false
  private stopped = true

  constructor(
    private readonly getSession: () => Promise<CompanionCloudSession | null>,
    private readonly sync: (session: CompanionCloudSession | null) => Promise<unknown>,
    private readonly intervalMs = 10_000,
  ) {}

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.timer = setInterval(() => { void this.tick() }, this.intervalMs)
    this.timer.unref?.()
    void this.tick()
  }

  sessionChanged(): void {
    this.generation += 1
    if (this.stopped) return
    if (this.running) this.rerun = true
    else void this.tick()
  }

  /** Revocation bypasses an in-flight authentication so the sidecar can cancel its lease. */
  revoke(): void {
    this.generation += 1
    if (this.stopped) return
    this.rerun = true
    void this.sync(null).catch(() => {})
  }

  stop(): void {
    if (this.stopped) return
    this.stopped = true
    this.generation += 1
    this.rerun = false
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    void this.sync(null).catch(() => {})
  }

  tick(): Promise<void> {
    if (this.stopped) return Promise.resolve()
    if (this.running) return this.running
    const generation = this.generation
    this.running = (async () => {
      try {
        const session = await this.getSession()
        if (this.stopped || generation !== this.generation) return
        await this.sync(session)
      } catch { /* Retry on the next tick; collection stays independent of cloud login. */ }
    })().finally(() => {
      this.running = null
      if (this.rerun && !this.stopped) {
        this.rerun = false
        void this.tick()
      }
    })
    return this.running
  }
}
