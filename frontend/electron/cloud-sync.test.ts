import {FixtureCloudSync as CloudSync} from './__tests__/private-upload'
// @vitest-environment node
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createCipheriv, createDecipheriv, createHash } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { GoogleTestSync } from './__tests__/google-sync'
import { cloudProvider } from './cloud-provider'
import { cloudRequest, MPI_API_ORIGIN } from './cloud-sync'

const OWNER_A = '11111111-1111-4111-8111-111111111111'
const OWNER_B = '22222222-2222-4222-8222-222222222222'
const key = Buffer.alloc(32, 17)
const iv = Buffer.alloc(16, 4)
const secure = {
  isEncryptionAvailable: () => true,
  encryptString(value: string) {
    const cipher = createCipheriv('aes-256-cbc', key, iv)
    return Buffer.concat([cipher.update(value, 'utf8'), cipher.final()])
  },
  decryptString(value: Buffer) {
    const cipher = createDecipheriv('aes-256-cbc', key, iv)
    return Buffer.concat([cipher.update(value), cipher.final()]).toString('utf8')
  },
}
const dirs: string[] = []
const oauthSyncs: CloudSync[] = []
function directory() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mpi-cloud-test-'))
  dirs.push(dir)
  return dir
}
afterEach(() => {
  for (const sync of oauthSyncs.splice(0)) sync.stop()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

describe('desktop Google sign-in', () => {
  function createGoogleSync(request: ConstructorParameters<typeof CloudSync>[3], timeoutMs?: number) {
    let authorizeUrl = ''
    const dir = directory()
    const publications: unknown[] = []
    const openExternal = vi.fn(async (url: string) => { authorizeUrl = url })
    const sync = new CloudSync(dir, secure, (status) => publications.push(status), request, Date.now, {
      openExternal, callbackPort: 0, timeoutMs,
    }, cloudProvider({}))
    oauthSyncs.push(sync)
    return { sync, dir, publications, openExternal, authorize: () => new URL(authorizeUrl),
      callback: () => new URL(authorizeUrl).searchParams.get('redirect_uri')! + '?state=' + new URL(authorizeUrl).searchParams.get('client_state') }
  }

  it('exchanges a browser code with a private S256 verifier, encrypts credentials and preserves row ownership', async () => {
    const uploads: string[] = []
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) return auth(OWNER_B)
      uploads.push((body as { user_id: string }).user_id)
      return { status: 201, body: null }
    })
    const flow = createGoogleSync(request)
    flow.sync.enqueue(session('account-a'))
    const started = await flow.sync.signInWithGoogle()
    expect(started).toMatchObject({ signedIn: false, signingIn: true, error: null })
    expect(request).not.toHaveBeenCalled()
    expect(flow.authorize().origin).toBe(MPI_API_ORIGIN)
    expect(flow.authorize().pathname).toBe('/cf-auth/google/start')
    expect(flow.authorize().searchParams.get('client')).toBe('desktop')
    expect(flow.authorize().searchParams.get('client_state')).toMatch(/^[A-Za-z0-9_-]{43}$/)
    await fetch(`${flow.callback()}&code=private-browser-code`)
    await vi.waitFor(() => expect(flow.sync.status().signedIn).toBe(true))
    const exchange = request.mock.calls.find(([url]) => url.endsWith('/exchange'))!
    const body = exchange[2] as { code: string; verifier: string }
    expect(body.code).toBe('private-browser-code')
    expect(body.verifier).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(createHash('sha256').update(body.verifier).digest('base64url'))
      .toBe(flow.authorize().searchParams.get('challenge'))
    expect(flow.authorize().toString()).not.toContain(body.verifier)
    expect(flow.sync.status()).toMatchObject({ signingIn: false, userId: OWNER_B, enabled: true, otherAccountPending: 1 })
    expect(uploads).toEqual([])
    const persisted = fs.readFileSync(path.join(flow.dir, cloudProvider({}).storageNamespace, 'cloud-auth.json'), 'utf8')
    const events = JSON.stringify(flow.publications)
    for (const secret of [body.verifier, body.code, 'a'.repeat(43), 'r'.repeat(43)]) {
      expect(persisted).not.toContain(secret)
      expect(events).not.toContain(secret)
    }
    expect(new CloudSync(flow.dir, secure, undefined, request, undefined, undefined, cloudProvider({})).status()).toMatchObject({ signedIn: true, userId: OWNER_B })
  })

  it('prevents duplicate browser windows and reports a disabled Google provider callback', async () => {
    const flow = createGoogleSync(vi.fn(async () => ({ status: 503, body: { error: 'auth_unavailable' } })))
    await flow.sync.signInWithGoogle()
    await flow.sync.signInWithGoogle()
    expect(flow.openExternal).toHaveBeenCalledTimes(1)
    await fetch(flow.callback() + '&error=server_error&error_code=provider_disabled')
    await vi.waitFor(() => expect(flow.sync.status()).toMatchObject({ signingIn: false, error: 'oauth-unavailable' }))
  })

  it.each([
    ['error=access_denied&error_description=private-provider-message', 'oauth-cancelled'],
    ['error=server_error&error_description=private-provider-message', 'oauth-failed'],
    ['code=private-invalid-code', 'oauth-failed'],
  ])('sanitizes provider or code exchange failures: %s', async (query, expected) => {
    const request = vi.fn(async () => ({ status: 400, body: { error_description: 'private-provider-message' } }))
    const flow = createGoogleSync(request)
    await flow.sync.signInWithGoogle()
    await fetch(`${flow.callback()}&${query}`)
    await vi.waitFor(() => expect(flow.sync.status().signingIn).toBe(false))
    expect(flow.sync.status()).toMatchObject({ signedIn: false, error: expected })
    expect(JSON.stringify(flow.publications)).not.toContain('private-provider-message')
    expect(fs.existsSync(path.join(flow.dir, cloudProvider({}).storageNamespace, 'cloud-auth.json'))).toBe(false)
  })

  it('cleans up after browser-open failure and permits retry', async () => {
    const flow = createGoogleSync(vi.fn(async () => auth()))
    flow.openExternal.mockRejectedValueOnce(new Error('private-browser-details'))
    await flow.sync.signInWithGoogle()
    expect(flow.sync.status()).toMatchObject({ signingIn: false, error: 'browser-open-failed' })
    await flow.sync.signInWithGoogle()
    expect(flow.sync.status()).toMatchObject({ signingIn: true, error: null })
  })

  it.each(['cancel', 'logout', 'stop'] as const)('ignores a token exchange finishing after %s', async (action) => {
    let finish!: () => void
    let began!: () => void
    const blocked = new Promise<void>((resolve) => { finish = resolve })
    const exchanging = new Promise<void>((resolve) => { began = resolve })
    const request = vi.fn(async (url: string) => {
      if (url.endsWith('/exchange')) { began(); await blocked; return auth(OWNER_A) }
      return { status: 201, body: null }
    })
    const flow = createGoogleSync(request)
    await flow.sync.signInWithGoogle()
    await fetch(`${flow.callback()}&code=late-code`)
    await exchanging
    const actionResult = action === 'logout' ? flow.sync.signOut()
      : action === 'cancel' ? flow.sync.cancelSignIn() : flow.sync.stop()
    finish()
    await actionResult
    await flow.sync.retry()
    expect(flow.sync.status()).toMatchObject({ signingIn: false, signedIn: false })
    expect(flow.publications).not.toContainEqual(expect.objectContaining({ userId: OWNER_A }))
    expect(fs.existsSync(path.join(flow.dir, cloudProvider({}).storageNamespace, 'cloud-auth.json'))).toBe(false)
  })

  it('cancels during loopback setup without opening the browser later', async () => {
    const flow = createGoogleSync(vi.fn(async () => auth()))
    const login = flow.sync.signInWithGoogle()
    flow.sync.cancelSignIn()
    await login
    expect(flow.openExternal).not.toHaveBeenCalled()
    expect(flow.sync.status()).toMatchObject({ signingIn: false, error: 'oauth-cancelled' })
  })

  it('times out and rejects a late browser callback', async () => {
    const flow = createGoogleSync(vi.fn(async () => auth()), 25)
    await flow.sync.signInWithGoogle()
    await vi.waitFor(() => expect(flow.sync.status().error).toBe('oauth-timeout'))
    expect(flow.sync.status().signingIn).toBe(false)
    await expect(fetch(`${flow.callback()}&code=late`)).rejects.toThrow()
  })

  it('preserves a sync pause requested while the browser login is pending', async () => {
    const request = vi.fn(async (_url: string) => auth())
    const flow = createGoogleSync(request)
    flow.sync.enqueue(session())
    await flow.sync.signInWithGoogle()
    await flow.sync.setEnabled(false)
    await fetch(`${flow.callback()}&code=valid-code`)
    await vi.waitFor(() => expect(flow.sync.status().signedIn).toBe(true))
    expect(flow.sync.status()).toMatchObject({ enabled: false, pending: 1 })
    expect(request.mock.calls.filter(([url]) => url.includes('/api/desktop/sessions'))).toHaveLength(0)
  })
})
function auth(userId = OWNER_A, expires = 3600, token = 'a'.repeat(43), now = Date.now()) {
  return { status: 200, body: {
    userId, email: 'private@example.com', clientKind: 'desktop',
    accessToken: token.padEnd(43, '_'), refreshToken: 'r'.repeat(43),
    accessExpiresAt: Math.floor(now / 1000) + expires, refreshExpiresAt: Math.floor(now / 1000) + expires + 86400,
  } }
}
function delegated(now = Date.now(), token = 'c'.repeat(43)) {
  return { status: 200, body: { user_id: OWNER_A, access_token: token.padEnd(43, '_'), expires_at: Math.floor(now / 1000) + 300 } }
}
function session(sessionId = 'session-1', ownerId: string | null = OWNER_A, rows: unknown[] = [[1, 60], [2, null]]) {
  return {
    sessionId, ownerId, name: 'Test session', payload: {
      metadata: { capture: { duration_total_sec: 2 } }, summary: { fps: 60 },
      timeseries: { columns: ['elapsed_sec', 'fps'], rows },
    },
  }
}

describe('durable cloud session sync', () => {
  it('encrypts credentials, persists before upload, and restores offline rows for retry', async () => {
    const dir = directory()
    let online = false
    const request = vi.fn(async (url: string) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) return auth()
      expect(fs.readdirSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox'))).toHaveLength(1)
      return { status: online ? 201 : 503, body: null }
    })
    const first = new GoogleTestSync(dir, secure, undefined, request)
    await first.completeGoogle()
    expect(first.enqueue(session()).ok).toBe(true)
    await first.retry()
    expect(first.status().pending).toBe(1)
    const persisted = fs.readFileSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-auth.json'), 'utf8')
    for (const secret of ['a'.repeat(43), 'r'.repeat(43), 'private@example.com', 'private-password'])
      expect(persisted).not.toContain(secret)
    expect(JSON.stringify(first.status())).not.toContain('token')
    const restored = new GoogleTestSync(dir, secure, undefined, request)
    expect(restored.status().signedIn).toBe(true)
    expect(restored.status().pending).toBe(1)
    online = true
    await restored.retry()
    expect(restored.status().pending).toBe(0)
    expect(fs.readdirSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox'))).toEqual([])
  })

  it('upserts duplicate session ids and preserves full rows, columns, missing values and duration', async () => {
    const request = vi.fn(async (url: string) => (url.endsWith('/exchange') || url.endsWith('/refresh')) ? auth() : { status: 201, body: null })
    const sync = new GoogleTestSync(directory(), secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session())
    await sync.retry()
    sync.enqueue(session())
    await sync.retry()
    const uploads = request.mock.calls.filter(([url]) => url.includes('/api/desktop/sessions'))
    expect(uploads).toHaveLength(2)
    // Inspect actual HTTP arguments rather than mirroring transformation logic.
    const last = request.mock.calls.at(-1) as unknown as [string, Record<string, string>, Record<string, unknown>]
    expect(last[0]).toBe(`${MPI_API_ORIGIN}/api/desktop/sessions`)
    expect(last[1].Authorization).toBe('Bearer ' + 'a'.repeat(43))
    expect(last[2]).toMatchObject({ user_id: OWNER_A, session_id: 'session-1', source: 'desktop',
      time_series: [[1, 60], [2, null]], sample_count: 2, duration_sec: 2,
      metadata: { timeseries_columns: ['elapsed_sec', 'fps'], capture: { session_id: 'session-1' } },
    })
  })

  it('retains the original account after logout and never sends A rows while B is logged in', async () => {
    let active = OWNER_A
    const uploads: string[] = []
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) return auth(active)
      if (url.includes('/api/desktop/sessions')) uploads.push((body as { user_id: string }).user_id)
      return { status: 201, body: null }
    })
    const sync = new GoogleTestSync(directory(), secure, undefined, request)
    await sync.completeGoogle()
    await sync.signOut()
    expect(sync.enqueue(session()).ok).toBe(true)
    await sync.retry()
    expect(sync.status()).toMatchObject({ signedIn: false, enabled: false, otherAccountPending: 1 })
    active = OWNER_B
    await sync.completeGoogle()
    expect(uploads).toEqual([])
    expect(sync.status().otherAccountPending).toBe(1)
    active = OWNER_A
    await sync.completeGoogle()
    expect(uploads).toEqual([OWNER_A])
    expect(sync.status().pending).toBe(0)
  })

  it('keeps a revised session queued when replaced during an in-flight upload', async () => {
    let release!: () => void
    const started = new Promise<void>((resolve) => { release = resolve })
    let finish!: () => void
    const blocked = new Promise<void>((resolve) => { finish = resolve })
    let uploadCount = 0
    const request = vi.fn(async (url: string) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) return auth()
      if (++uploadCount === 1) { release(); await blocked }
      return { status: 201, body: null }
    })
    const sync = new GoogleTestSync(directory(), secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session())
    await started
    sync.enqueue(session('session-1', OWNER_A, [[1, 61], [2, 62], [3, null]]))
    finish()
    await sync.retry()
    expect(sync.status().pending).toBe(1)
    await sync.retry()
    expect(uploadCount).toBe(2)
    expect(sync.status().pending).toBe(0)
  })

  it('loads healthy files beside a corrupt file and continues past a rejected row', async () => {
    const dir = directory()
    let online = false
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) return auth()
      const row = body as { session_id: string }
      return { status: online && row.session_id === 'healthy' ? 201 : 400, body: null }
    })
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session('rejected'))
    sync.enqueue(session('healthy'))
    await sync.retry()
    fs.writeFileSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox', `${'0'.repeat(64)}.json`), '{broken')
    online = true
    const restored = new GoogleTestSync(dir, secure, undefined, request)
    expect(restored.status().pending).toBe(2)
    await restored.retry()
    expect(restored.status().pending).toBe(1)
    expect(restored.status().error).toBe('http-400')
    expect(fs.existsSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox', `${'0'.repeat(64)}.json`))).toBe(true)
  })

  it('requires secure storage, ignores unauthenticated captures, and rejects overlong ids', async () => {
    const request = vi.fn(async () => auth())
    const sync = new GoogleTestSync(directory(), { ...secure, isEncryptionAvailable: () => false }, undefined, request)
    await sync.completeGoogle()
    expect(sync.status().error).toBe('secure-storage-unavailable')
    expect(request).not.toHaveBeenCalled()
    expect(sync.enqueue(session('local', null)).skipped).toBe(true)
    expect(sync.enqueue(session('x'.repeat(201))).ok).toBe(false)
  })

  it('continues to report a corrupt outbox file after healthy sessions are saved', async () => {
    const dir = directory()
    fs.mkdirSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox'), { recursive: true })
    fs.writeFileSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox', `${'0'.repeat(64)}.json`), '{broken')
    const request = vi.fn(async (url: string) => (url.endsWith('/exchange') || url.endsWith('/refresh')) ? auth() : { status: 201, body: null })
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session())
    await sync.retry()
    expect(sync.status()).toMatchObject({ pending: 0, error: 'local-storage' })
  })

  it('bounds the network deadline and suppresses transport error details', async () => {
    vi.useFakeTimers()
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, options) => {
      return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener('abort', () => reject(new Error('Bearer private-token transport details')))
      })
    })
    try {
      const result = cloudRequest(`${MPI_API_ORIGIN}/cf-auth/refresh`, {}, {}).catch((error) => error.message)
      await vi.advanceTimersByTimeAsync(15_000)
      expect(await result).toBe('network')
    } finally {
      fetch.mockRestore()
      vi.useRealTimers()
    }
  })

  it('rotates expired credentials once and clears expired auth without deleting pending rows', async () => {
    let time = 1_000_000
    let revoked = false
    const request = vi.fn(async (url: string) => {
      if (url.endsWith('/exchange')) return auth(OWNER_A, 120, undefined, time)
      if (url.endsWith('/refresh')) return revoked ? { status: 400, body: { token: 'never exposed' } } : auth(OWNER_A, 120, 'rotated-token', time)
      return { status: 503, body: null }
    })
    const sync = new GoogleTestSync(directory(), secure, undefined, request, () => time)
    await sync.completeGoogle()
    sync.enqueue(session())
    await sync.retry()
    time += 61_000
    await sync.retry()
    expect(request.mock.calls.filter(([url]) => url.endsWith('/refresh'))).toHaveLength(1)
    revoked = true
    time += 61_000
    await sync.retry()
    expect(sync.status()).toMatchObject({ signedIn: false, enabled: false, otherAccountPending: 1, error: 'auth-required' })
  })

  it.each(['pause', 'logout'] as const)('does not let a queued enable undo a later %s while uploading', async (action) => {
    let release!: () => void
    let started!: () => void
    const startedPromise = new Promise<void>((resolve) => { started = resolve })
    const blocked = new Promise<void>((resolve) => { release = resolve })
    const uploads: string[] = []
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) return auth()
      if (url.includes('/api/desktop/sessions')) {
        uploads.push((body as { session_id: string }).session_id)
        if (uploads.length === 1) { started(); await blocked }
      }
      return { status: 201, body: null }
    })
    const dir = directory()
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session('first'))
    await startedPromise
    sync.enqueue(session('next'))
    const disable = sync.setEnabled(false)
    const enable = sync.setEnabled(true)
    const stop = action === 'logout' ? sync.signOut() : sync.setEnabled(false)
    release()
    await Promise.all([disable, enable, stop])
    expect(uploads).toEqual(['first'])
    expect(sync.status()).toMatchObject(action === 'logout'
      ? { signedIn: false, enabled: false, otherAccountPending: 1 }
      : { signedIn: true, enabled: false, pending: 1 })
    expect(fs.existsSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-auth.json'))).toBe(action !== 'logout')
    expect(new GoogleTestSync(dir, secure, undefined, request).status()).toMatchObject({ enabled: false })
    // The stopped row remains durable and only drains after a new user action.
    if (action === 'logout') await sync.completeGoogle()
    else await sync.setEnabled(true)
    expect(uploads).toEqual(['first', 'next'])
    expect(sync.status().pending).toBe(0)
  })

  it('does not let a sign-in finishing after a later pause enable uploads', async () => {
    let release!: () => void
    let started!: () => void
    const startedPromise = new Promise<void>((resolve) => { started = resolve })
    const blocked = new Promise<void>((resolve) => { release = resolve })
    const uploads: string[] = []
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) { started(); await blocked; return auth() }
      if (url.includes('/api/desktop/sessions')) uploads.push((body as { session_id: string }).session_id)
      return { status: 201, body: null }
    })
    const dir = directory()
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    sync.enqueue(session())
    const login = sync.completeGoogle()
    await startedPromise
    const pause = sync.setEnabled(false)
    release()
    await Promise.all([login, pause])
    expect(uploads).toEqual([])
    expect(sync.status()).toMatchObject({ signedIn: true, enabled: false, pending: 1 })
    expect(new GoogleTestSync(dir, secure, undefined, request).status())
      .toMatchObject({ signedIn: true, enabled: false, pending: 1 })
    await sync.setEnabled(true)
    expect(uploads).toEqual(['session-1'])
  })

  it('does not let a sign-in finishing after logout restore auth, enable or drain', async () => {
    let release!: () => void
    let started!: () => void
    const startedPromise = new Promise<void>((resolve) => { started = resolve })
    const blocked = new Promise<void>((resolve) => { release = resolve })
    const publications: unknown[] = []
    const request = vi.fn(async (url: string) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) { started(); await blocked; return auth() }
      return { status: 201, body: null }
    })
    const dir = directory()
    const sync = new GoogleTestSync(dir, secure, (status) => publications.push(status), request)
    sync.enqueue(session())
    const login = sync.completeGoogle()
    await startedPromise
    const logout = sync.signOut()
    release()
    await Promise.all([login, logout])
    expect(sync.status()).toMatchObject({ signedIn: false, enabled: false, otherAccountPending: 1 })
    expect(publications).not.toContainEqual(expect.objectContaining({ signedIn: true }))
    expect(request.mock.calls.filter(([url]) => url.includes('/api/desktop/sessions'))).toEqual([])
    expect(fs.existsSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-auth.json'))).toBe(false)
    expect(new GoogleTestSync(dir, secure, undefined, request).status())
      .toMatchObject({ signedIn: false, enabled: false, otherAccountPending: 1 })
  })
})

describe('private companion access lease', () => {
  it('shares only access credentials while desktop uploads are paused', async () => {
    const events: unknown[] = []
    const request = vi.fn(async (url: string) => url.endsWith('/companion-token') ? delegated(1_000_000) : auth(OWNER_A, 3600, undefined, 1_000_000))
    const sync = new GoogleTestSync(directory(), secure, (status) => events.push(status), request, () => 1_000_000)
    await sync.completeGoogle()
    await sync.setEnabled(false)
    expect(await sync.companionSession()).toEqual({ user_id: OWNER_A, access_token: 'c'.repeat(43), expires_at: 1300 })
    expect(sync.status().enabled).toBe(false)
    expect(JSON.stringify(events)).not.toMatch(/private-access-token|private-refresh-token/)
  })
  it('refreshes a near-expiry lease even while upload is paused', async () => {
    let now = 1_000_000
    const request = vi.fn(async (url: string) => url.endsWith('/companion-token') ? delegated(now, 'new-delegated')
      : auth(OWNER_A, 3600, url.endsWith('/refresh') ? 'new-access' : undefined, now))
    const sync = new GoogleTestSync(directory(), secure, undefined, request, () => now)
    await sync.completeGoogle()
    await sync.setEnabled(false)
    now += 3_550_000
    expect((await sync.companionSession())?.access_token).toBe('new-delegated'.padEnd(43, '_'))
    expect(request.mock.calls.filter(([url]) => url.endsWith('/refresh'))).toHaveLength(1)
    expect(sync.status().enabled).toBe(false)
  })
  it('withholds a refresh finishing after logout', async () => {
    let now = 1_000_000
    let release!: () => void
    let started!: () => void
    const pending = new Promise<void>((resolve) => { release = resolve })
    const entered = new Promise<void>((resolve) => { started = resolve })
    const request = vi.fn(async (url: string) => {
      if (url.endsWith('/refresh')) { started(); await pending; return auth(OWNER_A, 3600, 'new-access', now) }
      return auth(OWNER_A, 3600, undefined, now)
    })
    const sync = new GoogleTestSync(directory(), secure, undefined, request, () => now)
    await sync.completeGoogle()
    now += 3_550_000
    const session = sync.companionSession()
    await entered
    const logout = sync.signOut()
    expect(await sync.companionSession()).toBeNull()
    release()
    await expect(session).resolves.toBeNull()
    await logout
    expect(await sync.companionSession()).toBeNull()
  })
  it('does not lose the existing account after a failed replacement login', async () => {
    let exchanges = 0
    const request = vi.fn(async (url: string) => url.endsWith('/companion-token') ? delegated()
      : ++exchanges === 1 ? auth() : { status: 400, body: {} })
    const sync = new GoogleTestSync(directory(), secure, undefined, request)
    await sync.completeGoogle()
    await sync.completeGoogle()
    expect((await sync.companionSession())?.user_id).toBe(OWNER_A)
  })
})

it('keeps the companion heartbeat available during a blocked outbox upload', async () => {
  let release!: () => void
  let started!: () => void
  const pending = new Promise<void>((resolve) => { release = resolve })
  const entered = new Promise<void>((resolve) => { started = resolve })
  const request = vi.fn(async (url: string) => {
    if (url.includes('/api/desktop/sessions')) { started(); await pending; return { status: 201, body: null } }
    return url.endsWith('/companion-token') ? delegated() : auth()
  })
  const sync = new GoogleTestSync(directory(), secure, undefined, request)
  await sync.completeGoogle()
  sync.enqueue(session())
  await entered
  // This read must resolve before the upload is released.
  expect((await sync.companionSession())?.user_id).toBe(OWNER_A)
  release()
  await sync.retry()
})

describe('desktop identity and server quota', () => {
  it('restores credentials without admitting the workspace until the server confirms identity', async () => {
    const dir = directory()
    const request = vi.fn(async (url: string) => url.endsWith('/session') ? { status: 200, body: { userId: OWNER_A, clientKind: 'desktop' } } : auth())
    const first = new GoogleTestSync(dir, secure, undefined, request)
    await first.completeGoogle()
    const restored = new GoogleTestSync(dir, secure, undefined, request)
    expect(restored.status()).toMatchObject({ signedIn: true, verified: false })
    expect(await restored.verifySession()).toMatchObject({ signedIn: true, verified: true })
    expect(request.mock.calls.at(-1)?.[0]).toBe(`${MPI_API_ORIGIN}/cf-auth/session`)
  })
  it.each([401, 503])('never admits cached identity when verification responds %s', async code => {
    const request = vi.fn(async (url: string) => url.endsWith('/session') ? { status: code, body: null } : auth())
    const sync = new GoogleTestSync(directory(), secure, undefined, request)
    await sync.completeGoogle()
    expect(await sync.verifySession()).toMatchObject({ verified: false, signedIn: code !== 401 })
  })
  it('passes optional byte allowance to the desktop while accepting older entitlement responses', async () => {
    const bytes = { status: 'available', used: 512, capacity: 1024, remaining: 512, unlimited: false }
    let includeBytes = false
    const request = vi.fn(async (url: string) => url.endsWith('/entitlement') ? { status: 200, body: {
      user_id: OWNER_A, plan: 'pro', remaining_seconds: 7200, storage_remaining: 10,
      storage_unlimited: false, cloud_storage: true, pooled: false,
      ...(includeBytes ? { storage_bytes: bytes } : {}),
    } } : auth())
    const sync = new GoogleTestSync(directory(), secure, undefined, request)
    await sync.completeGoogle()
    expect((await sync.entitlement()).storage_bytes).toBeUndefined()
    includeBytes = true
    expect((await sync.entitlement()).storage_bytes).toEqual(bytes)
  })
  it('rejects unknown/over-granted server leases and does not expose tokens in renderer status', async () => {
    let cap = 120
    const request = vi.fn(async (url: string) => (url.endsWith('/exchange') || url.endsWith('/refresh')) ? auth() : ({ status: 201, body: {
      reservation_id: 'reservation-1', user_id: OWNER_A, max_duration_sec: cap, expires_at: new Date(Date.now() + 600_000).toISOString(),
    } }))
    const sync = new GoogleTestSync(directory(), secure, undefined, request)
    await sync.completeGoogle()
    expect((await sync.reserveCapture('session-1', 120)).max_duration_sec).toBe(120)
    cap = 121
    await expect(sync.reserveCapture('session-1', 120)).rejects.toThrow('quota-unavailable')
    expect(JSON.stringify(sync.status())).not.toContain('a'.repeat(43))
    expect(JSON.stringify(sync.status())).not.toContain('r'.repeat(43))
  })
  it('persists failed completion and retries it only as its original owner', async () => {
    const dir = directory()
    let active = OWNER_A
    let online = false
    const completions: unknown[] = []
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if ((url.endsWith('/exchange') || url.endsWith('/refresh'))) return auth(active)
      if (url.endsWith('/finish')) { if (!online) throw new Error('offline'); completions.push(body) }
      return { status: 200, body: {} }
    })
    const first = new GoogleTestSync(dir, secure, undefined, request)
    await first.completeGoogle()
    await first.finishCapture({ reservation_id: 'lease-1', user_id: OWNER_A, max_duration_sec: 120, expires_at: new Date().toISOString() }, 'completed')
    expect(fs.readFileSync(path.join(dir, cloudProvider({}).storageNamespace, 'capture-finishes.json'), 'utf8')).toContain('lease-1')
    const restored = new GoogleTestSync(dir, secure, undefined, request)
    await restored.signOut(); active = OWNER_B; online = true
    await restored.completeGoogle(); await restored.retry()
    expect(completions).toEqual([])
    await restored.signOut(); active = OWNER_A
    await restored.completeGoogle(); await restored.retry()
    expect(completions).toEqual([{ outcome: 'completed' }])
    expect(JSON.parse(fs.readFileSync(path.join(dir, cloudProvider({}).storageNamespace, 'capture-finishes.json'), 'utf8'))).toEqual([])
  })
  it.each([409, 413])('keeps a rejected session durable for storage response %s', async code => {
    const dir = directory()
    const request = vi.fn(async (url: string) => (url.endsWith('/exchange') || url.endsWith('/refresh')) ? auth() : ({ status: code, body: { error: 'storage_full' } }))
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session()); await sync.retry()
    expect(sync.status()).toMatchObject({ pending: 1, error: code === 409 ? 'cloud-storage-full' : 'cloud-payload-too-large' })
    expect(fs.readdirSync(path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox'))).toHaveLength(1)
  })

  it.each([
    [409, 'storage_full', 'cloud-storage-full'],
    [409, 'storage_bytes_full', 'cloud-storage-full'],
    [409, 'storage_quota_unknown', 'cloud-storage-quota-unknown'],
    [503, 'storage_measurement_unavailable', 'cloud-storage-quota-unknown'],
  ] as const)('pauses automatic uploads after %s %s until explicit retry, including after restart', async (code, error, statusError) => {
    const dir = directory()
    let available = false
    const uploads: string[] = []
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if (url.endsWith('/exchange') || url.endsWith('/refresh')) return auth()
      uploads.push((body as { session_id: string }).session_id)
      return available ? { status: 201, body: null } : { status: code, body: { error } }
    })
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session('first'))
    await sync.retry()
    expect(uploads).toEqual(['first'])
    expect(sync.status()).toMatchObject({ pending: 1, error: statusError })

    vi.useFakeTimers()
    let restored: CloudSync | undefined
    try {
      sync.start()
      sync.enqueue(session('second'))
      await vi.advanceTimersByTimeAsync(90_000)
      expect(uploads).toEqual(['first'])
      expect(sync.status()).toMatchObject({ pending: 2, error: statusError })
      const outbox = path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox')
      const saved = fs.readdirSync(outbox).map(name => fs.readFileSync(path.join(outbox, name), 'utf8'))
      expect(saved).toHaveLength(2)
      sync.stop()

      restored = new CloudSync(dir, secure, undefined, request, undefined, undefined, cloudProvider({}))
      restored.start()
      await vi.advanceTimersByTimeAsync(90_000)
      expect(uploads).toEqual(['first'])
      expect(restored.status()).toMatchObject({ pending: 2, error: statusError })
      expect(fs.readdirSync(outbox).map(name => fs.readFileSync(path.join(outbox, name), 'utf8'))).toEqual(saved)

      available = true
      await restored.retry()
      expect(uploads[0]).toBe('first')
      expect(uploads.slice(1).sort()).toEqual(['first', 'second'])
      expect(restored.status()).toMatchObject({ pending: 0, error: null })
      expect(fs.readdirSync(outbox)).toEqual([])
    } finally {
      sync.stop()
      restored?.stop()
      vi.useRealTimers()
    }
  })

  it('does not apply one account\'s storage pause to another account or its queued captures', async () => {
    const dir = directory()
    let active = OWNER_A
    const uploads: string[] = []
    const request = vi.fn(async (url: string, _headers: unknown, body: unknown) => {
      if (url.endsWith('/exchange') || url.endsWith('/refresh')) return auth(active)
      const owner = (body as { user_id: string }).user_id
      uploads.push(owner)
      return owner === OWNER_A ? { status: 409, body: { error: 'storage_bytes_full' } } : { status: 201, body: null }
    })
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session('account-a'))
    await sync.retry()
    sync.enqueue(session('account-b', OWNER_B))
    await sync.setEnabled(true)
    expect(uploads).toEqual([OWNER_A])

    active = OWNER_B
    await sync.completeGoogle()
    expect(uploads).toEqual([OWNER_A, OWNER_B])
    expect(sync.status()).toMatchObject({ userId: OWNER_B, pending: 0, otherAccountPending: 1, error: null })
    const restored = new CloudSync(dir, secure, undefined, request, undefined, undefined, cloudProvider({}))
    expect(restored.status()).toMatchObject({ userId: OWNER_B, pending: 0, otherAccountPending: 1, error: null })
    const outbox = path.join(dir, cloudProvider({}).storageNamespace, 'cloud-outbox')
    expect(fs.readdirSync(outbox)).toHaveLength(1)
    expect(JSON.parse(fs.readFileSync(path.join(outbox, fs.readdirSync(outbox)[0]), 'utf8')).user_id).toBe(OWNER_A)
  })

  it('continues auth refresh and capture completion retries while session uploads are quota-paused', async () => {
    const dir = directory()
    let finishOnline = false
    const request = vi.fn(async (url: string) => {
      if (url.endsWith('/exchange') || url.endsWith('/refresh')) return auth(OWNER_A, 120)
      if (url.endsWith('/finish')) return { status: finishOnline ? 200 : 503, body: {} }
      return { status: 409, body: { error: 'storage_full' } }
    })
    const sync = new GoogleTestSync(dir, secure, undefined, request, () => Date.now())
    await sync.completeGoogle()
    sync.enqueue(session())
    await sync.retry()
    await sync.finishCapture({ reservation_id: 'quota-lease', user_id: OWNER_A, max_duration_sec: 120,
      expires_at: new Date(Date.now() + 120_000).toISOString() }, 'completed')
    vi.useFakeTimers()
    try {
      finishOnline = true
      sync.start()
      await vi.advanceTimersByTimeAsync(90_000)
      expect(request.mock.calls.filter(([url]) => url.includes('/api/desktop/sessions'))).toHaveLength(1)
      expect(request.mock.calls.some(([url]) => url.endsWith('/refresh'))).toBe(true)
      expect(JSON.parse(fs.readFileSync(path.join(dir, cloudProvider({}).storageNamespace, 'capture-finishes.json'), 'utf8'))).toEqual([])
      expect(sync.status()).toMatchObject({ pending: 1, error: 'cloud-storage-full' })
    } finally {
      sync.stop()
      vi.useRealTimers()
    }
  })

  it('keeps automatic retries for ordinary temporary server failures', async () => {
    const dir = directory()
    let online = false
    const uploads: string[] = []
    const request = vi.fn(async (url: string) => {
      if (url.endsWith('/exchange') || url.endsWith('/refresh')) return auth()
      uploads.push(url)
      return online ? { status: 201, body: null } : { status: 503, body: { error: 'temporarily_unavailable' } }
    })
    const sync = new GoogleTestSync(dir, secure, undefined, request)
    await sync.completeGoogle()
    sync.enqueue(session())
    await sync.retry()
    expect(sync.status()).toMatchObject({ pending: 1, error: 'http-503' })
    vi.useFakeTimers()
    try {
      sync.start()
      await vi.advanceTimersByTimeAsync(30_000)
      expect(uploads.length).toBeGreaterThan(1)
      online = true
      await vi.advanceTimersByTimeAsync(30_000)
      expect(sync.status()).toMatchObject({ pending: 0, error: null })
    } finally {
      sync.stop()
      vi.useRealTimers()
    }
  })
})
