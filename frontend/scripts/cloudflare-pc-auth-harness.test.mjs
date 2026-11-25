import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { STAGE_ORIGIN, PRODUCTION_ORIGIN, runHarness, validateRequest } from './cloudflare-pc-auth-harness.mjs'

const owner = '11111111-1111-4111-8111-111111111111'
const access = 'a'.repeat(43), refresh = 'r'.repeat(43), companion = 'c'.repeat(43)
function fixture({ rejectEntitlement = false, keepDelegation = false, origin = STAGE_ORIGIN } = {}) {
  const events = [], calls = []
  let authorization, revoked = false
  return { events, calls,
    emit: value => events.push(value),
    openAuthorization: async value => {
      authorization = new URL(value)
      const callback = authorization.searchParams.get('redirect_uri'), state = authorization.searchParams.get('client_state')
      assert.equal((await fetch(`${callback}?code=wrong&state=wrong`)).status, 400)
      assert.equal(calls.length, 0)
      assert.equal((await fetch(`${callback}?code=synthetic-handoff&state=${state}`)).status, 200)
    },
    transport: async (url, headers, body, method) => {
      const route = new URL(url).pathname; calls.push({ route, method })
      assert.equal(new URL(url).origin, origin)
      if (route === '/cf-auth/exchange') {
        assert.equal(body.code, 'synthetic-handoff')
        assert.equal(createHash('sha256').update(body.verifier).digest('base64url'), authorization.searchParams.get('challenge'))
        assert.equal(body.redirectUri, authorization.searchParams.get('redirect_uri'))
        return { status: 200, body: { userId: owner, email: 'private@example.test', clientKind: 'desktop', accessToken: access,
          refreshToken: refresh, accessExpiresAt: Math.floor(Date.now() / 1000) + 300, refreshExpiresAt: Math.floor(Date.now() / 1000) + 86_400 } }
      }
      if (route === '/cf-auth/session') {
        const delegated = headers.Authorization === `Bearer ${companion}`
        if (revoked && !(delegated && keepDelegation)) return { status: 401, body: { error: 'unauthorized' } }
        return { status: 200, body: { userId: owner, clientKind: delegated ? 'android_companion' : 'desktop' } }
      }
      if (route === '/api/desktop/entitlement') return rejectEntitlement ? { status: 503, body: {} } : { status: 200, body: {
        user_id: owner, plan: 'free', remaining_seconds: 3600, storage_remaining: 0, storage_unlimited: false, cloud_storage: false,
      } }
      if (route === '/cf-auth/companion-token') return { status: 200, body: { user_id: owner, access_token: companion, expires_at: Math.floor(Date.now() / 1000) + 300 } }
      if (route === '/cf-auth/logout') { revoked = true; return { status: 200, body: { ok: true } } }
      if (route === '/cf-auth/refresh' && revoked) return { status: 401, body: { error: 'unauthorized' } }
      throw new Error('unexpected synthetic request')
    },
  }
}

test('harness executes actual desktop PKCE/loopback, session, entitlement, delegated auth and logout with no secret output', async () => {
  const f = fixture(), result = await runHarness({ ...f, callbackPort: 0, timeoutMs: 5000 })
  assert.deepEqual(result, { ok: true, phases: ['google_pkce_loopback', 'desktop_session', 'desktop_entitlement', 'companion_delegation', 'logout_revocation'],
    persistence: 'public-marker-only', list: 'not-tested-no-native-read-route' })
  assert.equal(f.calls.filter(x => x.route === '/cf-auth/exchange').length, 1)
  assert.equal(f.calls.filter(x => x.route === '/cf-auth/companion-token').length, 1)
  const output = JSON.stringify([...f.events, result])
  for (const secret of [access, refresh, companion, owner, 'private@example.test', 'synthetic-handoff']) assert.equal(output.includes(secret), false)
})

test('harness rejects production, foreign endpoints, upload/capture writes and query credentials before networking', () => {
  for (const url of ['https://mpinspect.com/cf-auth/session', 'https://retired-auth.example.test/auth/v1/user',
    `${STAGE_ORIGIN}.attacker.test/cf-auth/session`, `${STAGE_ORIGIN}/api/desktop/sessions`, `${STAGE_ORIGIN}/api/desktop/captures`,
    `${STAGE_ORIGIN}/cf-auth/session?token=secret`, `${STAGE_ORIGIN}/cf-auth/session#secret`,
    `https://user:password@${new URL(STAGE_ORIGIN).host}/cf-auth/session`]) assert.throws(() => validateRequest(url, 'GET'), /request_not_allowed/)
  assert.doesNotThrow(() => validateRequest(`${STAGE_ORIGIN}/cf-auth/session`, 'GET'))
  assert.throws(() => validateRequest(`${STAGE_ORIGIN}/cf-auth/session`, 'POST'), /request_not_allowed/)
})

test('partial failure still revokes the desktop session and returns only a fixed diagnostic', async () => {
  const f = fixture({ rejectEntitlement: true }), result = await runHarness({ ...f, callbackPort: 0, timeoutMs: 5000 })
  assert.equal(result.ok, false); assert.equal(result.failedPhase, 'desktop_entitlement'); assert.equal(result.error, 'harness_failed')
  assert.equal(f.calls.filter(x => x.route === '/cf-auth/logout').length, 1)
  assert.equal(JSON.stringify(f.events).includes(owner), false)
})

test('logout is not reported verified if the parent-revoked delegated credential still authenticates', async () => {
  const f = fixture({ keepDelegation: true }), result = await runHarness({ ...f, callbackPort: 0, timeoutMs: 5000 })
  assert.equal(result.ok, false); assert.equal(result.failedPhase, 'logout_revocation'); assert.equal(result.error, 'revocation_failed')
})

test('production requires prior account expectations before any network call', async () => {
  let calls=0
  await assert.rejects(runHarness({origin:PRODUCTION_ORIGIN,transport:()=>{calls++}}), /existing_account_expectations_required/)
  assert.equal(calls,0)
  assert.throws(()=>validateRequest(`${STAGE_ORIGIN}/cf-auth/session`,'GET',PRODUCTION_ORIGIN),/request_not_allowed/)
  assert.doesNotThrow(()=>validateRequest(`${PRODUCTION_ORIGIN}/cf-auth/session`,'GET',PRODUCTION_ORIGIN))
})

test('explicit production flow matches existing UUID and entitlement without printing identity', async () => {
  const f=fixture({origin:PRODUCTION_ORIGIN})
  const result=await runHarness({...f,origin:PRODUCTION_ORIGIN,callbackPort:0,timeoutMs:5000,
    expectedAccounts:[{userId:owner,entitlement:{plan:'free',remaining_seconds:3600,storage_remaining:0,storage_unlimited:false,cloud_storage:false}}]})
  assert.equal(result.ok,true)
  assert.ok(f.events.some(event=>event.event==='existing_account_verified'&&event.identityMatched&&event.entitlementMatched))
  for(const secret of [owner,access,refresh,companion])assert.equal(JSON.stringify([...f.events,result]).includes(secret),false)
})

test('unexpected production identity or migrated entitlement is rejected and logged out', async () => {
  for(const expected of [
    {userId:'22222222-2222-4222-8222-222222222222',entitlement:{plan:'free'}},
    {userId:owner,entitlement:{plan:'pro'}},
  ]){
    const f=fixture({origin:PRODUCTION_ORIGIN})
    const result=await runHarness({...f,origin:PRODUCTION_ORIGIN,callbackPort:0,timeoutMs:5000,expectedAccounts:[expected]})
    assert.equal(result.ok,false)
    assert.ok(['unexpected_account','entitlement_mismatch'].includes(result.error))
    assert.equal(f.calls.filter(call=>call.route==='/cf-auth/logout').length,1)
  }
})
