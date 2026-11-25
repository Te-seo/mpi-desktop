import { vi } from 'vitest'
import { FixtureCloudSync as CloudSync } from './private-upload'
import { cloudProvider } from '../cloud-provider'

/** Synthetic browser driving the production Google PKCE flow for durability tests. */
export class GoogleTestSync extends CloudSync {
  private browserUrl: () => string
  constructor(...args: ConstructorParameters<typeof CloudSync>) {
    let url = ''
    const [directory, secure, onStatus, request, now, oauth, provider] = args
    super(directory, secure, onStatus, request, now,
      oauth ?? { callbackPort: 0, openExternal: async value => { url = value } },
      provider ?? cloudProvider({}))
    this.browserUrl = () => url
  }
  async completeGoogle() {
    await this.signInWithGoogle()
    if (this.status().signingIn) {
      const authorize = new URL(this.browserUrl())
      const callback = new URL(authorize.searchParams.get('redirect_uri')!)
      callback.searchParams.set('code', 'synthetic-google-code')
      const state = authorize.searchParams.get('client_state')
      if (state) callback.searchParams.set('state', state)
      await fetch(callback)
      await vi.waitFor(() => { if (this.status().signingIn) throw Error('waiting for Google exchange') })
      // Wait for the grant's existing serialized work, without requesting a second drain.
      await (this as unknown as { lifecycle: Promise<unknown> }).lifecycle
    }
    return this.status()
  }
}
