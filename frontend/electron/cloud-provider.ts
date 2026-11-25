import { createHash } from 'node:crypto'

export type CloudProvider = { kind: 'cloudflare'; apiOrigin: string; storageNamespace: string }

/** Production defaults are embedded in main; build-process env is not packaged.
 * Never selected by a renderer or delegated token. No alternative backend is supported. */
export function cloudProvider(env: Record<string, string | undefined> = process.env): CloudProvider {
  const kind = env.MPI_BACKEND ?? 'cloudflare'
  if (kind !== 'cloudflare') throw new Error('invalid-cloud-provider')
  const origin = env.MPI_CF_API_ORIGIN ?? 'https://mpinspect.com'
  let url: URL
  try { url = new URL(origin) } catch { throw new Error('invalid-cloud-origin') }
  if (url.protocol !== 'https:' || url.origin !== origin || url.username || url.password || url.hostname.endsWith('.')) throw new Error('invalid-cloud-origin')
  return { kind: 'cloudflare', apiOrigin: origin, storageNamespace: `cloudflare-${createHash('sha256').update(origin).digest('hex').slice(0, 24)}` }
}
