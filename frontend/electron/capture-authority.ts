import { randomUUID } from 'node:crypto'
import type { CaptureReservation } from './cloud-sync'

type Request = { id: string; command: string; payload: Record<string, unknown> }
type Lease = { reservation: CaptureReservation | null; userId: string; owner: number; generation: number; timer?: ReturnType<typeof setTimeout>; capture?: string; guard?: string; finished: boolean; activation?: Promise<void>; pausedAt?: number; pausedMs: number }
interface Service {
  userId(): string | null
  unlimited?(): Promise<boolean>
  reserve(sessionId: string, requestedSeconds: number): Promise<CaptureReservation>
  start(reservation: CaptureReservation): Promise<CaptureReservation>
  finish(reservation: CaptureReservation, outcome: 'completed' | 'cancelled'): Promise<void>
  stop(command: 'stop_capture' | 'stop_temperature_guard', id: string): void
}

/** Main-process authorization boundary. Renderer-supplied durations are only a
 * request; the server lease always clamps the Python timer and owns billing. */
export class CaptureAuthority {
  private generation = 0
  private readonly requests = new Map<string, Lease>()
  private readonly captures = new Map<string, Lease>()
  private readonly guards = new Map<string, Lease>()
  private readonly leases = new Set<Lease>()
  private readonly closedOwners = new Set<number>()
  private readonly endedBeforeResponse = new Set<string>()
  private readonly sampledBeforeResponse = new Set<string>()
  private readonly startedBeforeResponse = new Map<string, string>()
  private readonly finishedGuards = new Set<string>()
  constructor(private readonly service: Service, private readonly now = Date.now) {}

  async authorize(raw: unknown, owner: number): Promise<Request> {
    if (this.closedOwners.has(owner)) throw new Error('auth-required')
    const value = raw as Partial<Request> | null
    if (!value || typeof value.id !== 'string' || typeof value.command !== 'string') throw new Error('invalid-command')
    const request: Request = { id: value.id, command: value.command, payload: value.payload && typeof value.payload === 'object' ? { ...value.payload } : {} }
    const creates = request.command === 'start_capture' || request.command === 'create_temperature_guard'
    const authCommands = creates || ['activate_temperature_guard', 'create_temperature_preview', 'activate_temperature_preview', 'companion_metrics',
      'play_memory_qa_sample', 'play_memory_qa_summary'].includes(request.command)
    const userId = this.service.userId()
    if (authCommands && !userId) throw new Error('auth-required')
    if (request.command === 'activate_temperature_guard') {
      const lease = this.guards.get(String(request.payload.guard_id))
      if (!lease || lease.owner !== owner || lease.finished || lease.userId !== userId) throw new Error('auth-required')
    }
    if (request.command === 'pause_capture' || request.command === 'resume_capture') {
      const lease = this.captures.get(String(request.payload.capture_id))
      if (!lease || lease.owner !== owner || lease.finished || lease.userId !== userId) throw new Error('auth-required')
    }
    if (!creates) return request
    const generation = this.generation
    if (this.service.unlimited && await this.service.unlimited()) {
      // The server's explicit policy is authoritative. Continue to track the
      // owner lifecycle, but never reserve quota or force a timer in Python.
      if (generation !== this.generation || this.closedOwners.has(owner) || this.service.userId() !== userId || userId === null) throw new Error('auth-required')
      const lease: Lease = { reservation: null, userId, owner, generation, finished: false, pausedMs: 0 }
      this.leases.add(lease)
      this.requests.set(request.id, lease)
      // Preserve a user's explicitly configured optional timer, not a default cap.
      const explicit = request.payload.auto_stop_after_sec
      if (typeof explicit !== 'number' || !Number.isFinite(explicit) || explicit < 1 || explicit > 3600) delete request.payload.auto_stop_after_sec
      return request
    }
    if (generation !== this.generation || this.closedOwners.has(owner) || this.service.userId() !== userId || userId === null) throw new Error('auth-required')
    const requested = request.payload.auto_stop_after_sec
    const seconds = typeof requested === 'number' && Number.isFinite(requested) && requested >= 1
      ? Math.min(3600, Math.floor(requested)) : 3600
    const reservation = await this.service.reserve(randomUUID(), seconds)
    if (generation !== this.generation || this.closedOwners.has(owner) || this.service.userId() !== userId || reservation.user_id !== userId) {
      void this.service.finish(reservation, 'cancelled').catch(() => {})
      throw new Error('auth-required')
    }
    const remaining = Math.floor((Date.parse(reservation.expires_at) - this.now()) / 1000)
    const allowed = Math.min(seconds, Math.floor(reservation.max_duration_sec))
    if (!Number.isFinite(allowed) || allowed < 1 || remaining < 1) {
      void this.service.finish(reservation, 'cancelled').catch(() => {})
      throw new Error('quota-exhausted')
    }
    const lease: Lease = { reservation, userId, owner, generation, finished: false, pausedMs: 0,
      timer: setTimeout(() => this.expire(lease), Math.min(2_147_483_647, Math.max(1, Date.parse(reservation.expires_at) - this.now()))) }
    lease.timer?.unref?.()
    this.leases.add(lease)
    this.requests.set(request.id, lease)
    request.payload.auto_stop_after_sec = allowed
    return request
  }

  dispatched(id: string, status: string): void {
    if (status === 'sent') return
    const lease = this.requests.get(id)
    if (lease) { this.requests.delete(id); this.finish(lease, 'cancelled') }
  }

  consume(raw: unknown): void {
    const msg = raw as { id?: string; ok?: boolean; result?: { capture_id?: string; guard_id?: string }; event?: string; capture_id?: string; guard_id?: string; paused?: boolean }
    if (msg.id && this.requests.has(msg.id)) {
      const lease = this.requests.get(msg.id)!
      this.requests.delete(msg.id)
      if (!msg.ok || !msg.result) { this.finish(lease, 'cancelled'); return }
      let capture = msg.result.capture_id
      const guard = msg.result.guard_id
      if (guard && this.startedBeforeResponse.has(guard)) {
        capture = this.startedBeforeResponse.get(guard)
        this.startedBeforeResponse.delete(guard)
      }
      if (capture) { lease.capture = capture; this.captures.set(capture, lease) }
      if (guard && !capture) { lease.guard = guard; this.guards.set(guard, lease) }
      if (!capture && !guard) { this.finish(lease, 'cancelled'); return }
      if (lease.finished || lease.generation !== this.generation || this.service.userId() !== lease.userId) {
        if (capture) this.service.stop('stop_capture', capture)
        if (guard && !capture) this.service.stop('stop_temperature_guard', guard)
        if (capture) this.captures.delete(capture)
        if (guard) this.guards.delete(guard)
        this.finish(lease, 'cancelled')
        return
      }
      if (capture && this.sampledBeforeResponse.delete(capture)) this.activate(lease)
      if (capture && this.endedBeforeResponse.delete(capture)) this.finish(lease, 'completed')
      if (guard && this.endedBeforeResponse.delete(guard) && !capture) this.finish(lease, 'cancelled')
    }
    if (msg.event === 'snapshot' && msg.capture_id) {
      const lease = this.captures.get(msg.capture_id)
      if (lease) this.activate(lease)
      else {
        this.sampledBeforeResponse.add(msg.capture_id)
        if (this.sampledBeforeResponse.size > 1000) this.sampledBeforeResponse.delete(this.sampledBeforeResponse.values().next().value!)
      }
    }
    if (msg.event === 'capture_state' && msg.capture_id && typeof msg.paused === 'boolean') {
      const lease = this.captures.get(msg.capture_id)
      if (lease && !lease.finished) {
        if (msg.paused && lease.pausedAt === undefined) {
          lease.pausedAt = this.now()
          clearTimeout(lease.timer)
        } else if (!msg.paused && lease.pausedAt !== undefined) {
          lease.pausedMs += Math.max(0, this.now() - lease.pausedAt)
          lease.pausedAt = undefined
          this.scheduleExpiry(lease)
        }
      }
    }
    if (msg.event === 'temperature_guard_started' && msg.guard_id && msg.capture_id) {
      const lease = this.guards.get(msg.guard_id)
      if (lease) {
        this.guards.delete(msg.guard_id)
        lease.guard = undefined
        lease.capture = msg.capture_id
        this.captures.set(msg.capture_id, lease)
        if (lease.finished || lease.generation !== this.generation || this.service.userId() !== lease.userId) {
          this.service.stop('stop_capture', msg.capture_id)
          this.finish(lease, 'cancelled')
        } else {
          if (this.sampledBeforeResponse.delete(msg.capture_id)) this.activate(lease)
          if (this.endedBeforeResponse.delete(msg.capture_id)) this.finish(lease, 'completed')
        }
      } else if (this.finishedGuards.has(msg.guard_id)) {
        this.service.stop('stop_capture', msg.capture_id)
      } else {
        this.startedBeforeResponse.set(msg.guard_id, msg.capture_id)
        if (this.startedBeforeResponse.size > 1000) this.startedBeforeResponse.delete(this.startedBeforeResponse.keys().next().value!)
      }
    }
    if (msg.event === 'capture_ended' && msg.capture_id) {
      const lease = this.captures.get(msg.capture_id)
      if (lease) this.finish(lease, 'completed')
      else this.rememberEnded(msg.capture_id)
    }
    if (msg.event === 'temperature_guard_ended' && msg.guard_id) {
      const lease = this.guards.get(msg.guard_id)
      if (lease) this.finish(lease, 'cancelled')
      else this.rememberEnded(msg.guard_id)
    }
  }

  private rememberEnded(id: string): void {
    this.endedBeforeResponse.add(id)
    if (this.endedBeforeResponse.size > 1000) this.endedBeforeResponse.delete(this.endedBeforeResponse.values().next().value!)
  }

  private activate(lease: Lease): void {
    const reservation = lease.reservation
    if (!reservation || lease.activation || lease.finished) return
    lease.activation = Promise.resolve().then(() => this.service.start(reservation)).then(active => {
      if (lease.finished) return
      lease.reservation = active
      this.scheduleExpiry(lease)
    }).catch(() => {
      if (lease.capture) this.service.stop('stop_capture', lease.capture)
      // Avoid awaiting this very activation promise in finish().
      lease.activation = undefined
      this.finish(lease, 'cancelled')
    })
  }

  private scheduleExpiry(lease: Lease): void {
    clearTimeout(lease.timer)
    if (!lease.reservation || lease.pausedAt !== undefined || lease.finished) return
    // Pausing never reserves more measurement time. Keep the same server
    // maximum and billing reservation, while excluding pauses from this local
    // watchdog just as the Python timer does. Revocation still ends paused runs.
    const remaining = Date.parse(lease.reservation.expires_at) + lease.pausedMs - this.now()
    lease.timer = setTimeout(() => this.expire(lease), Math.min(2_147_483_647, Math.max(1, remaining)))
    lease.timer.unref?.()
  }

  revoke(): void {
    this.generation += 1
    for (const lease of this.leases) this.expire(lease)
  }

  closeOwner(owner: number): void {
    this.closedOwners.add(owner)
    for (const lease of this.leases) if (lease.owner === owner) this.expire(lease)
  }

  private expire(lease: Lease): void {
    if (lease.capture) this.service.stop('stop_capture', lease.capture)
    if (lease.guard) this.service.stop('stop_temperature_guard', lease.guard)
    this.finish(lease, lease.capture ? 'completed' : 'cancelled')
  }

  private finish(lease: Lease, outcome: 'completed' | 'cancelled'): void {
    if (lease.finished) return
    lease.finished = true
    clearTimeout(lease.timer)
    this.leases.delete(lease)
    if (lease.capture) this.captures.delete(lease.capture)
    if (lease.guard) {
      this.guards.delete(lease.guard)
      this.finishedGuards.add(lease.guard)
      if (this.finishedGuards.size > 1000) this.finishedGuards.delete(this.finishedGuards.values().next().value!)
    }
    void (lease.activation ?? Promise.resolve()).then(() => lease.reservation ? this.service.finish(lease.reservation, outcome) : undefined).catch(() => {})
  }
}
