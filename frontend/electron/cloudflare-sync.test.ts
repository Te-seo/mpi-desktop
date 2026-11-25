import {FixtureCloudSync as CloudSync} from './__tests__/private-upload'
// @vitest-environment node
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createCipheriv, createDecipheriv, createHash } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cloudProvider } from './cloud-provider'
import { GoogleTestSync } from './__tests__/google-sync'


const owner = '11111111-1111-4111-8111-111111111111'
const other = '22222222-2222-4222-8222-222222222222'
const origin = 'https://mpi-preview.example.test'
const provider = cloudProvider({ MPI_BACKEND: 'cloudflare', MPI_CF_API_ORIGIN: origin })
const key = Buffer.alloc(32, 17), iv = Buffer.alloc(16, 4)
const secure = {
  isEncryptionAvailable: () => true,
  encryptString(value: string) { const cipher = createCipheriv('aes-256-cbc', key, iv); return Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]) },
  decryptString(value: Buffer) { const cipher = createDecipheriv('aes-256-cbc', key, iv); return Buffer.concat([cipher.update(value), cipher.final()]).toString('utf8') },
}
const dirs: string[] = [], clients: CloudSync[] = []
function directory() { const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mpi-cf-test-')); dirs.push(dir); return dir }
function auth(now: number, userId = owner, access = 'a'.repeat(43)) { return { status: 200, body: {
  userId, sessionId: 'synthetic-session', clientKind: 'desktop', accessToken: access, refreshToken: 'r'.repeat(43),
  accessExpiresAt: Math.floor(now / 1000) + 300, refreshExpiresAt: Math.floor(now / 1000) + 86400,
} } }
function capture(ownerId = owner) { return { sessionId: 'capture-1', ownerId, name: 'Synthetic capture', payload: {
  metadata: { capture: { duration_total_sec: 600 } }, summary: { fps: 60 }, timeseries: { columns: ['elapsed_sec', 'fps'], rows: [[1, 60], [600, null]] },
} } }
afterEach(() => { for (const client of clients.splice(0)) client.stop(); for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true }) })

describe('Cloudflare storage schema upgrade', () => {
  function previousStorage(dir: string, prefix = 'previous') {
    const namespace = path.join(dir, provider.storageNamespace)
    fs.mkdirSync(namespace, { recursive: true })
    const session = { userId: owner, email: 'private@example.test', accessToken: 'a'.repeat(43),
      refreshToken: 'r'.repeat(43), expiresAt: Date.now() + 3600_000 }
    const stored = JSON.stringify({ encrypted: secure.encryptString(JSON.stringify({ session, enabled: false })).toString('base64') })
    const authFile = path.join(namespace, `${prefix}-auth.json`)
    fs.writeFileSync(authFile, stored)
    const queue = path.join(namespace, `${prefix}-outbox`)
    fs.mkdirSync(queue)
    const row = { user_id: owner, session_id: 'retained-capture', source: 'desktop', name: null,
      metadata: {}, summary: {}, time_series: [[1, 60]], sample_count: 1, duration_sec: 1 }
    const rowFile = `${createHash('sha256').update(`${owner}:retained-capture:desktop`).digest('hex')}.json`
    fs.writeFileSync(path.join(queue, rowFile), JSON.stringify(row))
    return { namespace, authFile, queue, rowFile, stored }
  }

  it('copies encrypted auth and owned queues once without deleting originals or reviving a logged-out account', async () => {
    const dir = directory(), previous = previousStorage(dir)
    const request = vi.fn(async () => ({ status: 204, body: null }))
    const sync = new CloudSync(dir, secure, undefined, request, Date.now, undefined, provider)
    expect(sync.status()).toMatchObject({ signedIn: true, verified: false, enabled: false, pending: 1 })
    expect(fs.readFileSync(path.join(previous.namespace, 'cloud-auth.json'), 'utf8')).toBe(previous.stored)
    expect(fs.readFileSync(previous.authFile, 'utf8')).toBe(previous.stored)
    expect(fs.existsSync(path.join(previous.queue, previous.rowFile))).toBe(true)
    await sync.signOut()
    const restarted = new CloudSync(dir, secure, undefined, request, Date.now, undefined, provider)
    expect(restarted.status()).toMatchObject({ signedIn: false, otherAccountPending: 1 })
    expect(fs.existsSync(previous.authFile)).toBe(true)
    expect(fs.existsSync(path.join(previous.namespace, 'storage-schema.json'))).toBe(true)
  })

  it('preserves an ambiguous storage schema without importing any account', () => {
    const dir = directory(), previous = previousStorage(dir)
    previousStorage(dir, 'another')
    const sync = new CloudSync(dir, secure, undefined, undefined, Date.now, undefined, provider)
    expect(sync.status()).toMatchObject({ signedIn: false, pending: 0, error: 'local-storage' })
    expect(fs.readFileSync(previous.authFile, 'utf8')).toBe(previous.stored)
    expect(fs.existsSync(path.join(previous.namespace, 'cloud-auth.json'))).toBe(false)
    expect(fs.existsSync(path.join(previous.namespace, 'storage-schema.json'))).toBe(false)
  })

  it('rejects an older credential format even within the current origin directory', () => {
    const dir = directory(), previous = previousStorage(dir)
    const stored = JSON.stringify({ encrypted: secure.encryptString(JSON.stringify({ enabled: true,
      session: { userId: owner, email: '', accessToken: 'jwt.header.signature', refreshToken: 'old-refresh', expiresAt: Date.now() + 60_000 },
    })).toString('base64') })
    fs.writeFileSync(previous.authFile, stored)
    const sync = new CloudSync(dir, secure, undefined, undefined, Date.now, undefined, provider)
    expect(sync.status()).toMatchObject({ signedIn: false, pending: 0, error: 'local-storage' })
    expect(fs.readFileSync(previous.authFile, 'utf8')).toBe(stored)
    expect(fs.existsSync(path.join(previous.namespace, 'cloud-auth.json'))).toBe(false)
  })
})

describe('explicit cloud provider selection', () => {
  it('defaults to production Cloudflare, rejects legacy backend selection and isolates each cloud environment', () => {
    expect(cloudProvider({})).toMatchObject({ kind: 'cloudflare', apiOrigin: 'https://mpinspect.com' })
    expect(cloudProvider({}).storageNamespace).not.toBe(provider.storageNamespace)
    expect(() => cloudProvider({ MPI_BACKEND: 'retired-provider' })).toThrow('invalid-cloud-provider')
    for (const value of ['', 'http://preview.test', 'https://preview.test/', 'https://a:secret@preview.test', 'https://preview.test/path', 'https://preview.test?token=secret', 'https://preview.test#x']) {
      expect(() => cloudProvider({ MPI_BACKEND: 'cloudflare', MPI_CF_API_ORIGIN: value })).toThrow('invalid-cloud-origin')
    }
    expect(() => cloudProvider({ MPI_BACKEND: 'unknown' })).toThrow('invalid-cloud-provider')
    expect(provider.storageNamespace).toMatch(/^cloudflare-[a-f0-9]{24}$/)
    expect(cloudProvider({ MPI_BACKEND: 'cloudflare', MPI_CF_API_ORIGIN: 'https://other.example.test' }).storageNamespace).not.toBe(provider.storageNamespace)
  })

  it('a normal launch opens production Cloudflare Google without reading or deleting legacy credentials and queues', async () => {
    vi.stubEnv('MPI_BACKEND', undefined)
    vi.stubEnv('MPI_CF_API_ORIGIN', undefined)
    try {
      const dir = directory(), legacyAuth = '{"legacy":"synthetic encrypted token placeholder"}'
      fs.writeFileSync(path.join(dir, 'cloud-auth.json'), legacyAuth)
      fs.mkdirSync(path.join(dir, 'cloud-outbox'))
      fs.writeFileSync(path.join(dir, 'cloud-outbox', 'saved.json'), '{"legacy":"queued session"}')
      fs.writeFileSync(path.join(dir, 'capture-finishes.json'), '[{"legacy":"pending completion"}]')
      const request = vi.fn(), openExternal = vi.fn(async (_url: string) => {})
      const sync = new CloudSync(dir, secure, undefined, request, Date.now, { callbackPort: 0, openExternal })
      clients.push(sync)
      expect(sync.status()).toMatchObject({ signedIn: false, pending: 0, otherAccountPending: 0, error: null })
      await sync.signInWithGoogle()
      const authorize = new URL(openExternal.mock.calls[0][0])
      expect(authorize.origin + authorize.pathname).toBe('https://mpinspect.com/cf-auth/google/start')
      expect(authorize.searchParams.get('client')).toBe('desktop')
      expect(authorize.searchParams.get('client_state')).toMatch(/^[A-Za-z0-9_-]{43}$/)
      expect(new URL(authorize.searchParams.get('redirect_uri')!).pathname).toBe('/cf-auth/callback')
      expect(request).not.toHaveBeenCalled()
      sync.cancelSignIn()
      expect(fs.readFileSync(path.join(dir, 'cloud-auth.json'), 'utf8')).toBe(legacyAuth)
      expect(fs.readFileSync(path.join(dir, 'cloud-outbox', 'saved.json'), 'utf8')).toBe('{"legacy":"queued session"}')
      expect(fs.readFileSync(path.join(dir, 'capture-finishes.json'), 'utf8')).toBe('[{"legacy":"pending completion"}]')
    } finally { vi.unstubAllEnvs() }
  })
})

describe('Cloudflare desktop provider', () => {
  it('uses Google PKCE/session/rotating refresh and flat upload DTOs, preserving encrypted origin-scoped state', async () => {
    let now = 1_800_000_000_000, online = false
    const events: unknown[] = [], dir = directory()
    const request = vi.fn(async (url: string, headers: Record<string, string>, body: unknown, method?: string) => {
      expect(url.startsWith(origin + '/')).toBe(true)
      expect(headers).not.toHaveProperty('apikey')
      if (url.endsWith('/exchange')) { expect(body).toMatchObject({ code: 'synthetic-google-code', verifier: expect.any(String), redirectUri: expect.any(String) }); return auth(now) }
      if (url.endsWith('/refresh')) { expect(headers.Authorization).toBe('Bearer ' + 'r'.repeat(43)); expect(body).toEqual({}); return auth(now, owner, 'b'.repeat(43)) }
      if (url.endsWith('/session')) { expect(method).toBe('GET'); return { status: 200, body: { userId: owner, clientKind: 'desktop' } } }
      expect(url).toBe(origin + '/api/desktop/sessions')
      expect(body).toMatchObject({ user_id: owner, session_id: 'capture-1', source: 'desktop', duration_sec: 600, time_series: [[1, 60], [600, null]] })
      expect(body).not.toHaveProperty('payload')
      return { status: online ? 201 : 503, body: null }
    })
    const sync = new GoogleTestSync(dir, secure, value => events.push(value), request, () => now, undefined, provider)
    expect((await sync.completeGoogle()).signedIn).toBe(true)
    sync.enqueue(capture()); await sync.retry()
    expect(sync.status().pending).toBe(1)
    const stored = fs.readFileSync(path.join(dir, provider.storageNamespace, 'cloud-auth.json'), 'utf8')
    for (const secret of ['a'.repeat(43), 'r'.repeat(43), 'test-password']) expect(stored + JSON.stringify(events)).not.toContain(secret)
    expect(fs.existsSync(path.join(dir, 'cloud-auth.json'))).toBe(false)
    expect(new GoogleTestSync(dir, secure, undefined, request).status()).toMatchObject({ signedIn: false, pending: 0, otherAccountPending: 0 })
    const restored = new GoogleTestSync(dir, secure, undefined, request, () => now, undefined, provider)
    expect(restored.status()).toMatchObject({ signedIn: true, verified: false, pending: 1 })
    now += 250_000
    expect((await restored.verifySession()).verified).toBe(true)
    online = true; await restored.retry()
    expect(restored.status().pending).toBe(0)
    const uploads = request.mock.calls.filter(([url]) => url.endsWith('/api/desktop/sessions'))
    expect(uploads.at(-1)![1].Authorization).toBe('Bearer ' + 'b'.repeat(43))
  })

  it('binds the fixed Google callback to state and S256 and never sends secrets to the browser', async () => {
    let authorizeUrl = ''
    const now = Date.now(), events: unknown[] = []
    const request = vi.fn(async (url: string, _headers: unknown, _body: unknown) => {
      expect(url).toBe(origin + '/cf-auth/exchange')
      return auth(now)
    })
    const sync = new GoogleTestSync(directory(), secure, value => events.push(value), request, () => now, {
      openExternal: async url => { authorizeUrl = url }, callbackPort: 0,
    }, provider)
    clients.push(sync)
    await sync.signInWithGoogle()
    const url = new URL(authorizeUrl), callback = url.searchParams.get('redirect_uri')!, state = url.searchParams.get('client_state')!
    expect(url.origin + url.pathname).toBe(origin + '/cf-auth/google/start')
    expect(url.searchParams.get('client')).toBe('desktop')
    expect(new URL(callback).pathname).toBe('/cf-auth/callback')
    expect(state).toMatch(/^[A-Za-z0-9_-]{43}$/)
    for (const query of ['code=attacker', 'code=attacker&state=wrong', `code=attacker&state=${state}&state=${state}`]) {
      expect((await fetch(callback + '?' + query)).status).toBe(400)
      expect(request).not.toHaveBeenCalled()
    }
    expect((await fetch(`${callback}?code=synthetic-code&state=${state}`)).status).toBe(200)
    await vi.waitFor(() => expect(sync.status().signedIn).toBe(true))
    const body = request.mock.calls[0][2] as { code: string; verifier: string; redirectUri: string }
    expect(body).toMatchObject({ code: 'synthetic-code', redirectUri: callback })
    expect(createHash('sha256').update(body.verifier).digest('base64url')).toBe(url.searchParams.get('challenge'))
    expect(authorizeUrl + JSON.stringify(events)).not.toContain(body.verifier)
    await expect(fetch(`${callback}?code=replay&state=${state}`)).rejects.toThrow()
  })

  it('mints an access-only Companion lease, caches it and prevents reuse after account changes or logout', async () => {
    let now = 1_800_000_000_000, user = owner, minted = 0
    const request = vi.fn(async (url: string, _headers: unknown, _body: unknown) => {
      if (url.endsWith('/exchange') || url.endsWith('/refresh')) return auth(now, user)
      if (url.endsWith('/companion-token')) { minted++; return { status: 200, body: { user_id: user, access_token: 'c'.repeat(42) + minted, expires_at: now / 1000 + 300 } } }
      return { status: 204, body: null }
    })
    const sync = new GoogleTestSync(directory(), secure, undefined, request, () => now, undefined, provider)
    await sync.completeGoogle()
    const [first, same] = await Promise.all([sync.companionSession(), sync.companionSession()])
    expect(same).toEqual(first); expect(minted).toBe(1)
    expect(Object.keys(first!).sort()).toEqual(['access_token', 'expires_at', 'user_id'])
    expect(first!.access_token).not.toBe('a'.repeat(43))
    now += 245_000
    expect((await sync.companionSession())!.access_token).not.toBe(first!.access_token)
    user = other; await sync.completeGoogle()
    expect((await sync.companionSession())!.user_id).toBe(other)
    await sync.signOut()
    expect(await sync.companionSession()).toBeNull()
    expect(request.mock.calls.some(([url]) => url === origin + '/cf-auth/logout')).toBe(true)
  })

  it('rejects a refresh that changes identity and retains the original owner’s queued data', async () => {
    let now = 1_800_000_000_000
    const request = vi.fn(async (url: string) => url.endsWith('/refresh') ? auth(now, other)
      : url.endsWith('/exchange') ? auth(now) : { status: 503, body: null })
    const sync = new GoogleTestSync(directory(), secure, undefined, request, () => now, undefined, provider)
    await sync.completeGoogle(); sync.enqueue(capture()); await sync.retry()
    now += 300_000; await sync.retry()
    expect(sync.status()).toMatchObject({ userId: owner, pending: 1, error: 'account-changed' })
  })

  it('keeps capture authority on the desktop parent and persists failed finishes in the selected environment', async () => {
    const now = 1_800_000_000_000, dir = directory()
    let finishOnline = false
    const reservation = { reservation_id: 'reservation-1', user_id: owner, max_duration_sec: 600, expires_at: new Date(now + 600_000).toISOString() }
    const request = vi.fn(async (url: string, headers: Record<string, string>, body: unknown, method?: string) => {
      if (url.endsWith('/exchange')) return auth(now)
      expect(headers.Authorization).toBe('Bearer ' + 'a'.repeat(43))
      if (url.endsWith('/entitlement')) {
        expect(method).toBe('GET')
        return { status: 200, body: { user_id: owner, plan: 'pro', remaining_seconds: 7200, storage_unlimited: false, cloud_storage: true, storage_remaining: 10 } }
      }
      if (url.endsWith('/captures')) { expect(body).toEqual({ session_id: 'capture-1', requested_seconds: 600 }); return { status: 201, body: reservation } }
      if (url.endsWith('/start')) return { status: 200, body: reservation }
      if (url.endsWith('/finish')) { expect(body).toEqual({ outcome: 'completed' }); return { status: finishOnline ? 200 : 503, body: {} } }
      throw new Error('unexpected route ' + url)
    })
    const sync = new GoogleTestSync(dir, secure, undefined, request, () => now, undefined, provider)
    await sync.completeGoogle()
    expect((await sync.entitlement()).plan).toBe('pro')
    expect(await sync.startCaptureReservation(await sync.reserveCapture('capture-1', 600))).toEqual(reservation)
    await sync.finishCapture(reservation, 'completed')
    const finishes = path.join(dir, provider.storageNamespace, 'capture-finishes.json')
    expect(JSON.parse(fs.readFileSync(finishes, 'utf8'))).toHaveLength(1)
    finishOnline = true; await sync.retry()
    expect(JSON.parse(fs.readFileSync(finishes, 'utf8'))).toEqual([])
    expect(request.mock.calls.some(([url]) => url.endsWith('/companion-token'))).toBe(false)
  })

  it('withholds a delegated token whose response arrives after logout', async () => {
    let release!: () => void, entered!: () => void
    const started = new Promise<void>(resolve => { entered = resolve }), blocked = new Promise<void>(resolve => { release = resolve })
    const now = 1_800_000_000_000
    const request = async (url: string) => {
      if (url.endsWith('/exchange')) return auth(now)
      if (url.endsWith('/companion-token')) { entered(); await blocked; return { status: 200, body: { user_id: owner, access_token: 'c'.repeat(43), expires_at: now / 1000 + 300 } } }
      return { status: 204, body: null }
    }
    const sync = new GoogleTestSync(directory(), secure, undefined, request, () => now, undefined, provider)
    await sync.completeGoogle()
    const lease = sync.companionSession()
    const assertion = expect(lease).rejects.toThrow('account-changed')
    await started; await sync.signOut(); release(); await assertion
    expect(await sync.companionSession()).toBeNull()
  })

  it('rejects web/Companion credentials presented as a desktop login', async () => {
    const response = auth(Date.now()); response.body.clientKind = 'android_companion'
    const sync = new GoogleTestSync(directory(), secure, undefined, async () => response, Date.now, undefined, provider)
    expect((await sync.completeGoogle()).signedIn).toBe(false)
  })
})
