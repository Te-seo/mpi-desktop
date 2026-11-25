import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const STAGE_ORIGIN = 'https://mpi-migration-stage.ketchup7124.workers.dev'
export const PRODUCTION_ORIGIN = 'https://mpinspect.com'
const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(path.join(frontend, 'package.json'))
const MARKER = Buffer.from('MPI-HARNESS-NO-CREDENTIALS')
const allowedRequests = new Set([
  'POST /cf-auth/exchange', 'POST /cf-auth/refresh', 'POST /cf-auth/companion-token',
  'POST /cf-auth/logout', 'GET /cf-auth/session', 'GET /api/desktop/entitlement',
])
const serverErrors = new Set(['origin_not_allowed', 'invalid_handoff', 'invalid_json', 'json_required', 'request_too_large',
  'auth_unavailable', 'unauthorized', 'cloudflare_auth_disabled', 'invalid_preview_configuration', 'not_found', 'google_login_only'])
class HarnessFailure extends Error {}
const check = (condition, code) => { if (!condition) throw new HarnessFailure(code) }

/** Compile only these three current application sources, without starting Electron. */
export function loadDesktopAuth() {
  const ts = require('typescript'), cache = new Map()
  const files = new Set(['cloud-sync.ts', 'cloud-provider.ts', 'oauth-loopback.ts'].map(name => path.join(frontend, 'electron', name)))
  function load(file) {
    check(files.has(file), 'unexpected_source_import')
    if (cache.has(file)) return cache.get(file).exports
    const module = { exports: {} }; cache.set(file, module)
    const source = fs.readFileSync(file, 'utf8')
    const code = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    } }).outputText
    new Function('require', 'module', 'exports', code)(name => {
      if (['node:fs', 'node:path', 'node:crypto', 'node:http'].includes(name)) return require(name)
      check(name.startsWith('./'), 'unexpected_source_import')
      return load(path.resolve(path.dirname(file), `${name}.ts`))
    }, module, module.exports)
    return module.exports
  }
  return { ...load(path.join(frontend, 'electron', 'cloud-sync.ts')), ...load(path.join(frontend, 'electron', 'cloud-provider.ts')) }
}

/** A fixed, explicit origin and native-only paths precede the application's transport. */
export function validateRequest(url, method = 'POST', origin = STAGE_ORIGIN) {
  let parsed
  try { parsed = new URL(url) } catch { throw new HarnessFailure('request_not_allowed') }
  check([STAGE_ORIGIN, PRODUCTION_ORIGIN].includes(origin) && parsed.origin === origin && !parsed.username && !parsed.password && !parsed.search && !parsed.hash &&
    allowedRequests.has(`${method} ${parsed.pathname}`), 'request_not_allowed')
}

function validateAuthorization(value, callbackPort, origin) {
  const url = new URL(value), callback = new URL(url.searchParams.get('redirect_uri'))
  check(url.origin === origin && url.pathname === '/cf-auth/google/start' && !url.username && !url.password && !url.hash,
    'authorization_not_allowed')
  check([...url.searchParams.keys()].sort().join(',') === 'challenge,client,client_state,next,redirect_uri' &&
    url.searchParams.get('client') === 'desktop' && url.searchParams.get('next') === '/account' &&
    /^[A-Za-z0-9_-]{43}$/.test(url.searchParams.get('challenge')) && /^[A-Za-z0-9_-]{43}$/.test(url.searchParams.get('client_state')) &&
    callback.protocol === 'http:' && callback.hostname === '127.0.0.1' && callback.pathname === '/cf-auth/callback' &&
    !callback.username && !callback.password && !callback.search && !callback.hash &&
    (callbackPort === 0 || callback.port === String(callbackPort)), 'authorization_not_allowed')
  return url.toString()
}

function removeTemporaryDirectory(directory) {
  const resolved = fs.realpathSync(directory), parent = fs.realpathSync(os.tmpdir())
  check(path.dirname(resolved) === parent && path.basename(resolved).startsWith('mpi-cf-auth-harness-') && !fs.lstatSync(directory).isSymbolicLink(),
    'temporary_directory_mismatch')
  fs.rmSync(resolved, { recursive: true, force: true })
}

/** Production is explicit and requires an existing-account expectation file. */
export async function runHarness({ emit = () => {}, transport, openAuthorization, callbackPort = 43873, timeoutMs = 300_000,
  origin = STAGE_ORIGIN, expectedAccounts = [] } = {}) {
  check([STAGE_ORIGIN, PRODUCTION_ORIGIN].includes(origin), 'request_not_allowed')
  if (origin === PRODUCTION_ORIGIN) {
    check(Array.isArray(expectedAccounts) && expectedAccounts.length > 0 && expectedAccounts.every(account =>
      /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(account?.userId) && account.entitlement &&
      typeof account.entitlement.plan === 'string' &&
      Object.keys(account.entitlement).every(key => ['plan', 'remaining_seconds', 'storage_remaining', 'storage_unlimited', 'cloud_storage', 'pooled'].includes(key))),
    'existing_account_expectations_required')
  }
  const { CloudSync, cloudRequest, cloudProvider } = loadDesktopAuth()
  const provider = cloudProvider({ MPI_BACKEND: 'cloudflare', MPI_CF_API_ORIGIN: origin })
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'mpi-cf-auth-harness-'))
  const authFile = path.join(directory, provider.storageNamespace, 'cloud-auth.json')
  const send = transport ?? cloudRequest
  let accessToken = '', refreshToken = '', delegatedToken = '', owner = '', logoutStatus = null
  let loginResolve, loginReject, loginSettled = false, timer, sync, phase = 'initialize'
  const phases = []
  const passed = name => { phases.push(name); emit({ event: 'passed', phase: name }) }
  const login = new Promise((resolve, reject) => { loginResolve = resolve; loginReject = reject })
  // Attach a rejection handler before the browser callback can arrive.
  void login.catch(() => {})
  const request = async (url, headers, body, method = 'POST') => {
    validateRequest(url, method, origin)
    const route = new URL(url).pathname
    let response
    try { response = await send(url, headers, body, method) }
    catch { emit({ event: 'request', route, method, transport: 'failed' }); throw new HarnessFailure('transport_failed') }
    emit({ event: 'request', route, method, status: response.status,
      ...(serverErrors.has(response.body?.error) ? { error: response.body.error } : {}) })
    if (['/cf-auth/exchange', '/cf-auth/refresh'].includes(route) && response.status === 200) {
      accessToken = response.body?.accessToken ?? ''; refreshToken = response.body?.refreshToken ?? ''
      owner = response.body?.userId ?? ''
      emit({ event: 'native_token_shape', desktop: response.body?.clientKind === 'desktop',
        validAccess: typeof accessToken === 'string' && /^[A-Za-z0-9_-]{43}$/.test(accessToken),
        validRefresh: typeof refreshToken === 'string' && /^[A-Za-z0-9_-]{43}$/.test(refreshToken),
        validOwner: typeof owner === 'string' && /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(owner),
        accessExpiresInSeconds: Number.isSafeInteger(response.body?.accessExpiresAt) ? response.body.accessExpiresAt - Math.floor(Date.now() / 1000) : null,
        refreshAfterAccess: Number.isSafeInteger(response.body?.refreshExpiresAt) && response.body.refreshExpiresAt >= response.body.accessExpiresAt })
    }
    if (route === '/cf-auth/logout') logoutStatus = response.status
    return response
  }
  const getSession = token => request(`${origin}/cf-auth/session`, { Authorization: `Bearer ${token}` }, undefined, 'GET')
  try {
    // Real persistence is exercised with a fixed public marker. No credential, encrypted
    // credential, user identifier or email is written; DPAPI/Electron safeStorage is out of scope.
    const secure = {
      isEncryptionAvailable: () => true,
      encryptString: () => MARKER,
      decryptString: () => { throw new HarnessFailure('unexpected_restore') },
    }
    sync = new CloudSync(directory, secure, status => {
      if (loginSettled) return
      if (status.signedIn && status.verified && !status.signingIn) {
        loginSettled = true; loginResolve()
      } else if (status.error && !status.signingIn) {
        loginSettled = true; loginReject(new HarnessFailure('google_sign_in_failed'))
      }
    }, request, Date.now, {
      callbackPort, timeoutMs,
      openExternal: async value => {
        const url = validateAuthorization(value, callbackPort, origin)
        emit({ event: 'authorize', url })
        if (openAuthorization) await openAuthorization(url)
      },
    }, provider)
    check(!sync.status().signedIn && sync.status().pending === 0 && sync.status().otherAccountPending === 0, 'profile_not_empty')
    phase = 'google_pkce_loopback'
    // This referenced timer keeps Node alive; the application's callback listener is unref'd.
    timer = setTimeout(() => loginReject(new HarnessFailure('google_timeout')), timeoutMs + 1_000)
    await sync.signInWithGoogle()
    await login
    clearTimeout(timer)
    check(/^[A-Za-z0-9_-]{43}$/.test(accessToken) && /^[A-Za-z0-9_-]{43}$/.test(refreshToken), 'missing_native_tokens')
    check(fs.readFileSync(authFile, 'utf8') === JSON.stringify({ encrypted: MARKER.toString('base64') }), 'unexpected_persistence')
    await sync.setEnabled(false)
    passed(phase)
    phase = 'desktop_session'
    const verified = await sync.verifySession()
    check(verified.signedIn && verified.verified && !verified.error && verified.userId === owner, 'desktop_session_failed')
    const expectedAccount = expectedAccounts.find(account => account.userId === owner)
    if (origin === PRODUCTION_ORIGIN) check(expectedAccount, 'unexpected_account')
    passed(phase)
    phase = 'desktop_entitlement'
    const entitlement = await sync.entitlement()
    if (expectedAccount) {
      check(Object.entries(expectedAccount.entitlement).every(([key, value]) => entitlement[key] === value), 'entitlement_mismatch')
      emit({ event: 'existing_account_verified', identityMatched: true, entitlementMatched: true,
        checkedEntitlementFields: Object.keys(expectedAccount.entitlement).sort() })
    }
    passed(phase)
    phase = 'companion_delegation'
    const delegated = await sync.companionSession()
    check(delegated && delegated.user_id === owner && delegated.access_token !== accessToken && !('refresh_token' in delegated), 'delegation_failed')
    delegatedToken = delegated.access_token
    const principal = await getSession(delegatedToken)
    check(principal.status === 200 && principal.body?.userId === owner && principal.body?.clientKind === 'android_companion', 'delegation_authentication_failed')
    passed(phase)
    phase = 'logout_revocation'
    await sync.signOut()
    check(logoutStatus >= 200 && logoutStatus < 300 && !sync.status().signedIn && !fs.existsSync(authFile) && await sync.companionSession() === null,
      'logout_failed')
    const accessCheck = await getSession(accessToken), delegatedCheck = await getSession(delegatedToken)
    const refreshCheck = await request(`${origin}/cf-auth/refresh`, { Authorization: `Bearer ${refreshToken}` }, {})
    check(accessCheck.status === 401 && delegatedCheck.status === 401 && refreshCheck.status === 401, 'revocation_failed')
    passed(phase)
    return { ok: true, phases, persistence: 'public-marker-only', list: 'not-tested-no-native-read-route' }
  } catch (error) {
    // Never stringify arbitrary exceptions: request failures may include URLs, body or headers.
    return { ok: false, phases, failedPhase: phase, error: error instanceof HarnessFailure ? error.message : 'harness_failed' }
  } finally {
    clearTimeout(timer)
    if (sync) {
      // Revoke on partial failures too. The temporary profile never touches the real Electron profile.
      if (sync.status().signedIn) await sync.signOut()
      sync.stop()
    }
    accessToken = ''; refreshToken = ''; delegatedToken = ''; owner = ''
    removeTemporaryDirectory(directory)
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const stage = process.argv.length === 3 && process.argv[2] === '--run-stage'
  const production = process.argv.length === 5 && process.argv[2] === '--run-production' && process.argv[3] === '--expected-accounts'
  if (!stage && !production) {
    process.stdout.write('Usage: node frontend/scripts/cloudflare-pc-auth-harness.mjs --run-stage | --run-production --expected-accounts <local-json-file>\n')
    process.exitCode = 2
  } else {
    // Only the authorize URL (state + public PKCE challenge) and allowlisted phase results reach stdout.
    let result
    try {
      const expectedAccounts = production ? JSON.parse(fs.readFileSync(path.resolve(process.argv[4]), 'utf8')).accounts : []
      result = await runHarness({ origin: production ? PRODUCTION_ORIGIN : STAGE_ORIGIN, expectedAccounts,
        emit: value => process.stdout.write(JSON.stringify(value) + '\n') })
    } catch (error) { result = { ok: false, error: error instanceof HarnessFailure ? error.message : 'invalid_harness_configuration' } }
    process.stdout.write(JSON.stringify({ event: 'result', ...result }) + '\n')
    process.exitCode = result.ok ? 0 : 1
  }
}
