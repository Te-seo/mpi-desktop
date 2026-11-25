import { randomBytes, timingSafeEqual } from 'node:crypto'
import { createServer } from 'node:http'

export const OAUTH_CALLBACK_PORT = 43873
export const OAUTH_TIMEOUT_MS = 5 * 60_000

export type OAuthCallbackResult = { code: string } | {
  error: 'oauth-cancelled' | 'oauth-timeout' | 'oauth-failed' | 'oauth-unavailable' | 'oauth-callback-unavailable'
}

export interface OAuthLoopback {
  redirectTo: string
  result: Promise<OAuthCallbackResult>
  cancel(): void
}

/** Each attempt owns one loopback listener, bound by an unpredictable path or state.
 * The browser carries an authorization code only; tokens never enter this page. */
export function startOAuthLoopback(
  port = OAUTH_CALLBACK_PORT,
  timeoutMs = OAUTH_TIMEOUT_MS,
  binding?: { path: '/cf-auth/callback'; state: string },
): Promise<OAuthLoopback> {
  return new Promise((resolve, reject) => {
    if (binding && !/^[A-Za-z0-9_-]{43}$/.test(binding.state)) { reject(new Error('oauth-callback-unavailable')); return }
    const callbackPath = binding?.path ?? `/auth/callback/${randomBytes(32).toString('hex')}`
    let expectedHost = ''
    let settled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let complete!: (result: OAuthCallbackResult) => void
    const result = new Promise<OAuthCallbackResult>((done) => { complete = done })
    const finish = (value: OAuthCallbackResult) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      server.close()
      server.closeIdleConnections()
      complete(value)
    }
    const server = createServer({ maxHeaderSize: 8_192 }, (request, response) => {
      response.setHeader('Cache-Control', 'no-store')
      response.setHeader('Referrer-Policy', 'no-referrer')
      response.setHeader('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
      response.setHeader('X-Content-Type-Options', 'nosniff')
      response.setHeader('Connection', 'close')
      let url: URL
      try { url = new URL(request.url ?? '', `http://${expectedHost}`) } catch {
        response.writeHead(400).end()
        return
      }
      if (settled || request.headers.host !== expectedHost || url.host !== expectedHost ||
          url.pathname !== callbackPath || request.method !== 'GET') {
        response.writeHead(404).end()
        return
      }
      if (binding) {
        const states = url.searchParams.getAll('state')
        if (states.length !== 1 || Buffer.byteLength(states[0]) !== Buffer.byteLength(binding.state) ||
            !timingSafeEqual(Buffer.from(states[0]), Buffer.from(binding.state))) {
          response.writeHead(400).end()
          return
        }
      }
      const error = url.searchParams.get('error')
      const errorCode = url.searchParams.get('error_code')
      const codes = url.searchParams.getAll('code')
      let value: OAuthCallbackResult
      if (error || errorCode) {
        value = { error: errorCode === 'provider_disabled' || errorCode === 'oauth_provider_not_supported'
          ? 'oauth-unavailable'
          : error === 'access_denied' ? 'oauth-cancelled' : 'oauth-failed' }
      } else if (codes.length === 1 && codes[0].length > 0 && codes[0].length <= 2_048 &&
                 !/\s/.test(codes[0]) && ![...codes[0]].some(char => char.charCodeAt(0) < 32)) {
        value = { code: codes[0] }
      } else {
        // Do not consume an attempt for a browser prefetch or malformed request.
        response.writeHead(400).end()
        return
      }
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
      response.end('<!doctype html><html lang="ko"><meta charset="utf-8"><title>MPI 로그인</title><h1>MPI로 돌아가세요</h1><p>로그인 요청을 받았습니다. MPI 앱에서 결과를 확인하고 이 탭을 닫으세요.</p></html>')
      finish(value)
    })
    server.requestTimeout = 5_000
    server.headersTimeout = 5_000
    server.timeout = 5_000
    server.maxConnections = 8
    server.once('error', () => {
      if (!expectedHost) reject(new Error('oauth-callback-unavailable'))
      finish({ error: 'oauth-callback-unavailable' })
    })
    server.listen({ port, host: '127.0.0.1', exclusive: true }, () => {
      const address = server.address()
      if (!address || typeof address === 'string') {
        reject(new Error('oauth-callback-unavailable'))
        finish({ error: 'oauth-callback-unavailable' })
        return
      }
      expectedHost = `127.0.0.1:${address.port}`
      server.unref()
      timer = setTimeout(() => finish({ error: 'oauth-timeout' }), timeoutMs)
      timer.unref()
      resolve({
        redirectTo: `http://${expectedHost}${callbackPath}`,
        result,
        cancel: () => finish({ error: 'oauth-cancelled' }),
      })
    })
  })
}
