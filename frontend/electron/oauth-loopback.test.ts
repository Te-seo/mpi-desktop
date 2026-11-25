// @vitest-environment node
import { createServer, request } from 'node:http'
import { describe, expect, it } from 'vitest'
import { startOAuthLoopback } from './oauth-loopback'

function get(url: string, host?: string): Promise<{ status: number; body: string; headers: Record<string, unknown> }> {
  return new Promise((resolve, reject) => {
    request(url, { headers: host ? { Host: host } : undefined }, (response) => {
      let body = ''
      response.on('data', (part) => { body += part })
      response.on('end', () => resolve({ status: response.statusCode ?? 0, body, headers: response.headers }))
    }).on('error', reject).end()
  })
}

describe('desktop OAuth loopback', () => {
  it('accepts one code only on the exact loopback host and unpredictable path', async () => {
    const listener = await startOAuthLoopback(0)
    try {
      const url = new URL(listener.redirectTo)
      expect(url.hostname).toBe('127.0.0.1')
      expect(url.pathname).toMatch(/^\/auth\/callback\/[a-f0-9]{64}$/)
      expect((await get(`${url.origin}/auth/callback/other?code=wrong`)).status).toBe(404)
      expect((await get(`${listener.redirectTo}?code=wrong`, 'attacker.invalid')).status).toBe(404)
      expect((await get(listener.redirectTo)).status).toBe(400)
      expect((await get(`${listener.redirectTo}?code=one&code=two`)).status).toBe(400)
      const response = await get(`${listener.redirectTo}?code=private-auth-code`)
      expect(response.status).toBe(200)
      expect(response.body).not.toContain('private-auth-code')
      expect(response.headers['referrer-policy']).toBe('no-referrer')
      expect(response.headers['cache-control']).toBe('no-store')
      expect(response.headers['content-security-policy']).toContain("default-src 'none'")
      expect(await listener.result).toEqual({ code: 'private-auth-code' })
      await expect(get(`${listener.redirectTo}?code=replay`)).rejects.toThrow()
    } finally { listener.cancel() }
  })

  it('classifies cancellation without reflecting provider error text', async () => {
    const listener = await startOAuthLoopback(0)
    const response = await get(`${listener.redirectTo}?error=access_denied&error_description=private-provider-details`)
    expect(response.body).not.toContain('private-provider-details')
    expect(await listener.result).toEqual({ error: 'oauth-cancelled' })
  })

  it('expires and closes its listener', async () => {
    const listener = await startOAuthLoopback(0, 25)
    expect(await listener.result).toEqual({ error: 'oauth-timeout' })
    await expect(get(`${listener.redirectTo}?code=late`)).rejects.toThrow()
  })

  it('reports an occupied callback port without opening a second listener', async () => {
    const occupied = createServer()
    await new Promise<void>((resolve) => occupied.listen(0, '127.0.0.1', resolve))
    try {
      const address = occupied.address()
      if (!address || typeof address === 'string') throw new Error('missing address')
      await expect(startOAuthLoopback(address.port)).rejects.toThrow('oauth-callback-unavailable')
    } finally { occupied.close() }
  })
})
