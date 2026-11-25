// Persisted settings for sending saved measurement data to a user-configured
// HTTP(S) endpoint. Both settings are local-only. Existing endpoint users are
// migrated to enabled once so an upgrade does not silently stop delivery.

import { migrateLegacyStorageKey } from './storageMigration'

const ENDPOINT_URL_KEY = 'mpi.endpoint.url.v1'
const SEND_ON_SAVE_KEY = 'mpi.endpoint.sendOnSave.v1'
const LEGACY_ENDPOINT_URL_KEY = 'perfinspect.endpoint.url.v1'
const LEGACY_SEND_ON_SAVE_KEY = 'perfinspect.endpoint.sendOnSave.v1'

function migrateEndpointSettings(): void {
  // An empty URL is a valid "not configured" value. Migrate it as well so a
  // cleared pre-MPI setting does not leave the legacy namespace behind forever.
  migrateLegacyStorageKey(ENDPOINT_URL_KEY, LEGACY_ENDPOINT_URL_KEY)
  migrateLegacyStorageKey(
    SEND_ON_SAVE_KEY,
    LEGACY_SEND_ON_SAVE_KEY,
    (value) => value === '0' || value === '1'
  )
}

/** Read the configured endpoint URL, or '' when unset/unavailable. */
export function getEndpointUrl(): string {
  if (typeof window === 'undefined' || !window.localStorage) return ''
  try {
    migrateEndpointSettings()
    return window.localStorage.getItem(ENDPOINT_URL_KEY)?.trim() ?? ''
  } catch {
    return ''
  }
}

/** Persist the endpoint URL (trimmed). Empty string clears it. */
export function setEndpointUrl(url: string): void {
  if (typeof window === 'undefined' || !window.localStorage) return
  try {
    const trimmed = url.trim()
    if (trimmed) window.localStorage.setItem(ENDPOINT_URL_KEY, trimmed)
    else window.localStorage.removeItem(ENDPOINT_URL_KEY)
  } catch {
    /* localStorage may be unavailable */
  }
}

/** Whether saved data should be POSTed to the configured endpoint. */
export function getSendOnSave(): boolean {
  if (typeof window === 'undefined' || !window.localStorage) return false
  try {
    migrateEndpointSettings()
    const stored = window.localStorage.getItem(SEND_ON_SAVE_KEY)
    if (stored === '1') return true
    if (stored === '0') return false

    // Before this toggle existed, a configured endpoint was always used on
    // save. Preserve that behavior once, then persist an explicit state so a
    // later user opt-out is never re-enabled merely because the URL remains.
    const migrateEnabled = Boolean(window.localStorage.getItem(ENDPOINT_URL_KEY)?.trim())
    if (migrateEnabled) {
      try {
        window.localStorage.setItem(SEND_ON_SAVE_KEY, '1')
      } catch {
        /* still honor the existing URL for this session */
      }
    }
    return migrateEnabled
  } catch {
    return false
  }
}

export function setSendOnSave(enabled: boolean): void {
  if (typeof window === 'undefined' || !window.localStorage) return
  try {
    window.localStorage.setItem(SEND_ON_SAVE_KEY, enabled ? '1' : '0')
  } catch {
    /* localStorage may be unavailable */
  }
}
