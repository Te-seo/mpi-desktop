// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CompanionCloudBridge, CompanionCloudCoordinator, isMainOnlyCompanionRequest, type CompanionCloudSession } from './companion-cloud'

const lease: CompanionCloudSession = { user_id: 'owner-a', access_token: 'private-access', expires_at: 9_999_999_999 }
const counts = { connected: 2, signed_in: 1, unavailable: 1 }
afterEach(() => vi.useRealTimers())

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('main-only companion bridge', () => {
  it('consumes internal replies including duplicates and ignores renderer correlation IDs', async () => {
    const requests: { id: string; payload: { session: CompanionCloudSession | null } }[] = []
    const bridge = new CompanionCloudBridge((request) => { requests.push(request as typeof requests[number]); return true })
    const withRefresh = { ...lease, refresh_token: 'private-refresh' }
    const response = bridge.sync(withRefresh)
    expect(requests[0].payload.session).toEqual(lease)
    expect(JSON.stringify(requests)).not.toMatch(/refresh_token|refreshToken/)
    expect(bridge.consume({ id: requests[0].id, result: { ...counts, private: 'not-forwarded' } })).toBe(true)
    await expect(response).resolves.toEqual(counts)
    expect(bridge.consume({ id: requests[0].id, result: { secret: 'not-broadcast' } })).toBe(true)
    expect(bridge.consume({ id: 'renderer-request', result: counts })).toBe(false)
  })
  it('rejects renderer access to the command or any reserved correlation ID', () => {
    expect(isMainOnlyCompanionRequest({ command: 'sync_companion_cloud', payload: { session: lease } })).toBe(true)
    expect(isMainOnlyCompanionRequest({ command: 'list_devices', id: 'mpi-main-companion:forged' })).toBe(true)
    expect(isMainOnlyCompanionRequest({ command: 'list_devices', id: 'normal-request' })).toBe(false)
    expect(isMainOnlyCompanionRequest(null)).toBe(false)
  })
  it('sanitizes failures and consumes late responses after a timeout', async () => {
    vi.useFakeTimers()
    let id = ''
    const bridge = new CompanionCloudBridge((request) => { id = (request as { id: string }).id; return true }, 50)
    const result = bridge.sync(lease)
    const rejected = expect(result).rejects.toThrow('companion-sync-unavailable')
    await vi.advanceTimersByTimeAsync(51)
    await rejected
    expect(bridge.consume({ id, error: 'private-token' })).toBe(true)
  })
  it('clears pending requests on process exit and does not return transport details', async () => {
    const bridge = new CompanionCloudBridge(() => true)
    const result = bridge.sync(lease)
    bridge.disconnect()
    await expect(result).rejects.toThrow('companion-sync-unavailable')
    await expect(new CompanionCloudBridge(() => { throw new Error('private-token') }).sync(lease)).rejects.toThrow('companion-sync-unavailable')
  })
})

describe('companion cloud heartbeat coordinator', () => {
  it('serializes periodic work and retries after a failed exchange', async () => {
    vi.useFakeTimers()
    const inFlight = deferred<unknown>()
    const sync = vi.fn().mockImplementationOnce(() => inFlight.promise).mockResolvedValue(undefined)
    const coordinator = new CompanionCloudCoordinator(async () => lease, sync)
    coordinator.start()
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(sync).toHaveBeenCalledTimes(1)
    inFlight.resolve(undefined)
    await coordinator.tick()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(sync).toHaveBeenCalledTimes(2)
    coordinator.stop()
  })
  it('never forwards a session that resolves after logout or account change', async () => {
    const pending = deferred<CompanionCloudSession | null>()
    const getSession = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(null)
    const sync = vi.fn().mockResolvedValue(undefined)
    const coordinator = new CompanionCloudCoordinator(getSession, sync)
    coordinator.start()
    coordinator.revoke()
    coordinator.sessionChanged()
    pending.resolve(lease)
    await coordinator.tick()
    await Promise.resolve()
    expect(sync.mock.calls.every(([session]) => session === null)).toBe(true)
    coordinator.stop()
  })
  it('revokes immediately while device authentication is still pending', async () => {
    const pending = deferred<unknown>()
    const sync = vi.fn().mockImplementationOnce(() => pending.promise).mockResolvedValue(undefined)
    const coordinator = new CompanionCloudCoordinator(async () => lease, sync)
    coordinator.start()
    await Promise.resolve()
    expect(sync).toHaveBeenLastCalledWith(lease)
    coordinator.revoke()
    expect(sync).toHaveBeenLastCalledWith(null)
    coordinator.stop()
    pending.resolve(undefined)
    await coordinator.tick()
    expect(sync.mock.calls.filter(([value]) => value !== null)).toHaveLength(1)
  })
  it('stops heartbeat and rejects late session fetch after shutdown', async () => {
    vi.useFakeTimers()
    const pending = deferred<CompanionCloudSession | null>()
    const sync = vi.fn().mockResolvedValue(undefined)
    const coordinator = new CompanionCloudCoordinator(() => pending.promise, sync)
    coordinator.start()
    coordinator.stop()
    pending.resolve(lease)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(sync.mock.calls).toEqual([[null]])
  })
})
