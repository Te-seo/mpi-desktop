import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// Node may expose an incomplete localStorage shim when no --localstorage-file
// path is configured. Install a deterministic in-memory Storage implementation
// for preference/state tests instead of relying on that process-level shim.
const storage = new Map<string, string>()
const localStorageStub: Storage = {
  get length() {
    return storage.size
  },
  clear() {
    storage.clear()
  },
  getItem(key) {
    return storage.get(key) ?? null
  },
  key(index) {
    return Array.from(storage.keys())[index] ?? null
  },
  removeItem(key) {
    storage.delete(key)
  },
  setItem(key, value) {
    storage.set(key, String(value))
  },
}
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: localStorageStub,
})

// Unmount React trees between tests so DOM state never leaks across cases.
afterEach(() => cleanup())
