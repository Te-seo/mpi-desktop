const MIGRATION_STATE_KEY = 'mpi.localStorageMigration.v1'

type StoredValueValidator = (value: string) => boolean

interface MigrationRecord {
  legacyFingerprint: string
  currentFingerprint: string
  phase: 'pending' | 'complete'
}

type MigrationState = Record<string, MigrationRecord>

const registrations = new Map<
  string,
  { currentKey: string; isValid: StoredValueValidator }
>()
let storageListenerInstalled = false

function fingerprint(value: string): string {
  let hash = 0x811c9dc5
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return `${value.length}:${(hash >>> 0).toString(16).padStart(8, '0')}`
}

function readMigrationState(): MigrationState {
  const raw = localStorage.getItem(MIGRATION_STATE_KEY)
  if (!raw) return {}
  const parsed = JSON.parse(raw) as unknown
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
  const state: MigrationState = {}
  for (const [key, value] of Object.entries(parsed)) {
    // Upgrade the first migration-state format conservatively. Treat a legacy
    // key that is still/re-again present as authoritative even when its bytes
    // match the old fingerprint; that covers both a failed old removal and a
    // rollback that recreated the same value.
    if (typeof value === 'string') {
      state[key] = {
        legacyFingerprint: value,
        currentFingerprint: '',
        phase: 'complete',
      }
      continue
    }
    if (
      value &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      typeof (value as MigrationRecord).legacyFingerprint === 'string' &&
      typeof (value as MigrationRecord).currentFingerprint === 'string' &&
      ((value as MigrationRecord).phase === 'pending' ||
        (value as MigrationRecord).phase === 'complete')
    ) {
      state[key] = value as MigrationRecord
    }
  }
  return state
}

function persistMigrationRecord(
  legacyKey: string,
  record: MigrationRecord
): boolean {
  const state = readMigrationState()
  state[legacyKey] = record
  const serialized = JSON.stringify(state)
  localStorage.setItem(MIGRATION_STATE_KEY, serialized)
  const verified = readMigrationState()[legacyKey]
  return (
    verified?.legacyFingerprint === record.legacyFingerprint &&
    verified.currentFingerprint === record.currentFingerprint &&
    verified.phase === record.phase
  )
}

function registerCrossWindowRecovery(
  currentKey: string,
  legacyKey: string,
  isValid: StoredValueValidator
): void {
  if (typeof window === 'undefined') return
  registrations.set(legacyKey, { currentKey, isValid })
  if (storageListenerInstalled) return
  storageListenerInstalled = true
  window.addEventListener('storage', (event) => {
    if (!event.key || event.newValue === null) return
    const registration = registrations.get(event.key)
    if (!registration || !registration.isValid(event.newValue)) return
    queueMicrotask(() => {
      try {
        const legacy = localStorage.getItem(event.key!)
        if (legacy !== null) {
          migrateLegacyStorageKey(
            registration.currentKey,
            event.key!,
            registration.isValid
          )
          return
        }
        // Another window may have written immediately before this window
        // removed the legacy key. The event still carries those bytes. Import
        // them directly instead of recreating the legacy key: recreating it in
        // every renderer would generate an endless set/remove event ping-pong.
        const recovered = event.newValue!
        localStorage.setItem(registration.currentKey, recovered)
        if (localStorage.getItem(registration.currentKey) !== recovered) return
        persistMigrationRecord(event.key!, {
          legacyFingerprint: fingerprint(recovered),
          currentFingerprint: fingerprint(recovered),
          phase: 'complete',
        })
      } catch {
        // Leave recovery to the next launch when storage is unavailable.
      }
    })
  })
}

export function isStoredJsonObject(value: string): boolean {
  try {
    const parsed = JSON.parse(value) as unknown
    return !!parsed && typeof parsed === 'object' && !Array.isArray(parsed)
  } catch {
    return false
  }
}

export function isStoredJsonArray(value: string): boolean {
  try {
    return Array.isArray(JSON.parse(value))
  } catch {
    return false
  }
}

/**
 * Copy one legacy localStorage entry into its MPI key and remove the old key
 * only after both the value and a migration fingerprint can be read back.
 *
 * The fingerprint lets a later rollback write to the legacy key without losing
 * that newer value when MPI is launched again. A malformed current value also
 * falls back to a valid legacy value instead of deleting the only usable copy.
 */
export function migrateLegacyStorageKey(
  currentKey: string,
  legacyKey: string,
  isValid: StoredValueValidator = () => true
): void {
  try {
    registerCrossWindowRecovery(currentKey, legacyKey, isValid)
    let current = localStorage.getItem(currentKey)
    const legacy = localStorage.getItem(legacyKey)
    const state = readMigrationState()
    const prior = state[legacyKey]
    if (legacy === null) {
      if (prior?.phase === 'pending') {
        persistMigrationRecord(legacyKey, { ...prior, phase: 'complete' })
      }
      return
    }
    const legacyFingerprint = fingerprint(legacy)
    const rollbackWrite =
      prior?.phase === 'complete' ||
      (prior?.phase === 'pending' &&
        prior.legacyFingerprint !== legacyFingerprint)

    if (current === null || !isValid(current) || rollbackWrite) {
      if (!isValid(legacy)) return
      localStorage.setItem(currentKey, legacy)
      if (localStorage.getItem(currentKey) !== legacy) return
      current = legacy
    }
    if (!isValid(current)) return

    const pending: MigrationRecord = {
      legacyFingerprint,
      currentFingerprint: fingerprint(current),
      phase: 'pending',
    }
    if (!persistMigrationRecord(legacyKey, pending)) return
    // Do not remove a value that changed in another window while the current
    // value or migration record was being persisted.
    if (localStorage.getItem(currentKey) !== current) return
    if (localStorage.getItem(legacyKey) !== legacy) return
    localStorage.removeItem(legacyKey)
    if (localStorage.getItem(legacyKey) !== null) return
    persistMigrationRecord(legacyKey, { ...pending, phase: 'complete' })
  } catch {
    // Hardened/private renderer contexts may deny storage. Runtime state still
    // works; leave the legacy value untouched so a later launch can retry.
  }
}
