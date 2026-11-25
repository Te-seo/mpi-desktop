import type { CloudMessages } from './i18n/namespaces/cloud'

export interface CloudStatus {
  available: boolean
  signedIn: boolean
  verified?: boolean
  signingIn?: boolean
  userId: string | null
  email: string | null
  enabled: boolean
  pending: number
  otherAccountPending: number
  syncing: boolean
  lastSyncedAt: string | null
  error: string | null
}

export function cloudErrorMessage(error: string | null, messages: CloudMessages): string | null {
  if (!error) return null
  switch (error) {
    case 'secure-storage-unavailable': return messages.secureUnavailable
    case 'auth-failed': return messages.authFailed
    case 'session-replaced': return messages.sessionReplaced
    case 'auth-required': return messages.authRequired
    case 'network': return messages.networkError
    case 'local-storage': return messages.storageError
    case 'account-changed': return messages.accountChanged
    case 'cloud-storage-full': return messages.storageFull
    case 'cloud-storage-quota-unknown': return messages.storageQuotaUnknown
    case 'cloud-payload-too-large': return messages.payloadTooLarge
    case 'oauth-unavailable': return messages.googleUnavailable
    case 'oauth-cancelled': return messages.googleCancelled
    case 'oauth-timeout': return messages.googleTimeout
    case 'oauth-failed': return messages.googleFailed
    case 'browser-open-failed': return messages.browserOpenFailed
    case 'oauth-callback-unavailable': return messages.googleCallbackUnavailable
    default: return messages.uploadError
  }
}

export const LOCAL_CLOUD_STATUS: CloudStatus = {
  available: false, signedIn: false, signingIn: false, userId: null, email: null, enabled: false,
  pending: 0, otherAccountPending: 0, syncing: false, lastSyncedAt: null, error: null,
}

export async function cloudCommand(channel: string, input?: unknown): Promise<CloudStatus> {
  if (!window.api) return LOCAL_CLOUD_STATUS
  return window.api.invoke<CloudStatus>(channel, input)
}

export function onCloudStatus(listener: (status: CloudStatus) => void): () => void {
  if (!window.api) return () => {}
  return window.api.receive('cloud-status-changed', (status) => listener(status as CloudStatus))
}

export async function enqueueCloudSession(input: {
  ownerId: string | null
  sessionId: string
  name: string
  payload: unknown
}): Promise<{ ok: boolean; skipped?: boolean; status: CloudStatus }> {
  if (!window.api) return { ok: true, skipped: true, status: LOCAL_CLOUD_STATUS }
  return window.api.invoke('cloud-enqueue', input)
}
