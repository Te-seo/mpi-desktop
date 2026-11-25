// @vitest-environment node
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { CaptureAuthority } from './capture-authority'
import { CloudSync } from './cloud-sync'
import { cloudProvider } from './cloud-provider'

const user = '11111111-1111-4111-8111-111111111111'

describe('capture with no plan time limit', () => {
  it('runs past the former cap without reserve/settle, preserves user timer, and still stops on revocation', async () => {
    vi.useFakeTimers()
    try {
      const reserve = vi.fn(), start = vi.fn(), finish = vi.fn(), stop = vi.fn()
      const gate = new CaptureAuthority({ userId: () => user, unlimited: async () => true, reserve, start, finish, stop })
      const first = await gate.authorize({ id: 'p1', command: 'start_capture', payload: {} }, 1)
      expect(first.payload).not.toHaveProperty('auto_stop_after_sec')
      const second = await gate.authorize({ id: 'p2', command: 'start_capture', payload: { auto_stop_after_sec: 60 } }, 2)
      expect(second.payload).toHaveProperty('auto_stop_after_sec', 60)
      gate.consume({ id: 'p1', ok: true, result: { capture_id: 'one' } })
      gate.consume({ id: 'p2', ok: true, result: { capture_id: 'two' } })
      gate.consume({ event: 'snapshot', capture_id: 'one' })
      await vi.advanceTimersByTimeAsync(3_601_000)
      expect(stop).not.toHaveBeenCalled()
      await expect(gate.authorize({ id: 'badpause', command: 'pause_capture', payload: { capture_id: 'one' } }, 2)).rejects.toThrow('auth-required')
      gate.revoke()
      expect(stop.mock.calls).toEqual([['stop_capture', 'one'], ['stop_capture', 'two']])
      expect(reserve).not.toHaveBeenCalled()
      expect(start).not.toHaveBeenCalled()
      expect(finish).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('rejects an authorization superseded by signout while entitlement is pending', async () => {
    let allow!: (value: boolean) => void
    const service = { userId: () => user, unlimited: () => new Promise<boolean>(r => { allow = r }), reserve: vi.fn(), start: vi.fn(), finish: vi.fn(), stop: vi.fn() }
    const gate = new CaptureAuthority(service)
    const pending = gate.authorize({ id: 'a', command: 'start_capture', payload: {} }, 1)
    gate.revoke()
    allow(true)
    await expect(pending).rejects.toThrow('auth-required')
    expect(service.reserve).not.toHaveBeenCalled()
  })
})

it('keeps a verified desktop login on network failure and clears it only for the typed replacement', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mpi-desktop-policy-'))
  const provider = cloudProvider({ MPI_CF_API_ORIGIN: 'https://local-fixture.example.test' })
  const secure = { isEncryptionAvailable: () => true, decryptString: (value: Buffer) => value.toString(), encryptString: (value: string) => Buffer.from(value) }
  const directory = path.join(root, provider.storageNamespace)
  const authFile = path.join(directory, 'cloud-auth.json')
  fs.mkdirSync(directory)
  let now = Date.UTC(2026, 10, 24)
  fs.writeFileSync(authFile, JSON.stringify({ encrypted: Buffer.from(JSON.stringify({
    session: { userId: user, email: '', accessToken: 'access', refreshToken: 'refresh', expiresAt: now + 600_000 },
    enabled: false,
  })).toString('base64') }))
  let condition: 'ok' | 'network' | 'replace' = 'ok'
  const request = vi.fn(async () => {
    if (condition === 'network') throw new Error('offline')
    if (condition === 'replace') return { status: 401, body: { error: 'session_replaced' } }
    return { status: 200, body: { userId: user, clientKind: 'desktop' } }
  })
  const sync = new CloudSync(root, secure, undefined, request, () => now, undefined, provider)
  vi.useFakeTimers()
  try {
    await expect(sync.verifySession()).resolves.toMatchObject({ verified: true, signedIn: true })
    sync.start()
    condition = 'network'
    now += 31_000
    await vi.advanceTimersByTimeAsync(30_000)
    expect(sync.status()).toMatchObject({ verified: true, signedIn: true, enabled: false })
    expect(fs.existsSync(authFile)).toBe(true)
    condition = 'replace'
    now += 31_000
    await vi.advanceTimersByTimeAsync(30_000)
    expect(sync.status()).toMatchObject({ verified: false, signedIn: false, error: 'session-replaced' })
    expect(fs.existsSync(authFile)).toBe(false)
  } finally { sync.stop(); vi.useRealTimers(); fs.rmSync(root, { recursive: true, force: true }) }
})
