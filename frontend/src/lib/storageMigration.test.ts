import { beforeEach, describe, expect, it, vi } from 'vitest'
import { migrateLegacyStorageKey } from './storageMigration'

describe('localStorage namespace migration', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('copies, verifies, and removes a valid legacy value', () => {
    localStorage.setItem('perfinspect.setting.v1', '{"value":1}')

    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      (value) => JSON.parse(value).value === 1
    )

    expect(localStorage.getItem('mpi.setting.v1')).toBe('{"value":1}')
    expect(localStorage.getItem('perfinspect.setting.v1')).toBeNull()
  })

  it('restores a valid legacy value over a damaged current value', () => {
    localStorage.setItem('mpi.setting.v1', 'damaged')
    localStorage.setItem('perfinspect.setting.v1', '{"value":2}')

    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      (value) => {
        try {
          return typeof JSON.parse(value).value === 'number'
        } catch {
          return false
        }
      }
    )

    expect(localStorage.getItem('mpi.setting.v1')).toBe('{"value":2}')
    expect(localStorage.getItem('perfinspect.setting.v1')).toBeNull()
  })

  it('imports a newer value written by a rollback after the first migration', () => {
    const validate = (value: string) => value === 'old' || value === 'rollback-new'
    localStorage.setItem('perfinspect.setting.v1', 'old')
    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      validate
    )
    localStorage.setItem('mpi.setting.v1', 'mpi-edited')
    localStorage.setItem('perfinspect.setting.v1', 'rollback-new')

    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      (value) => validate(value) || value === 'mpi-edited'
    )

    expect(localStorage.getItem('mpi.setting.v1')).toBe('rollback-new')
    expect(localStorage.getItem('perfinspect.setting.v1')).toBeNull()
  })

  it('treats a same-byte legacy reappearance as a rollback write', () => {
    const validate = (value: string) => value === 'old' || value === 'mpi-edited'
    localStorage.setItem('perfinspect.setting.v1', 'old')
    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      validate
    )
    localStorage.setItem('mpi.setting.v1', 'mpi-edited')
    localStorage.setItem('perfinspect.setting.v1', 'old')

    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      validate
    )

    expect(localStorage.getItem('mpi.setting.v1')).toBe('old')
    expect(localStorage.getItem('perfinspect.setting.v1')).toBeNull()
  })

  it('does not remove a legacy value changed while recording migration state', () => {
    const originalSetItem = localStorage.setItem.bind(localStorage)
    let injected = false
    vi.spyOn(localStorage, 'setItem').mockImplementation((key, value) => {
      originalSetItem(key, value)
      if (key === 'mpi.localStorageMigration.v1' && !injected) {
        injected = true
        originalSetItem('perfinspect.setting.v1', 'rollback-new')
      }
    })
    localStorage.setItem('perfinspect.setting.v1', 'old')

    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      (value) => value === 'old' || value === 'rollback-new'
    )

    expect(localStorage.getItem('perfinspect.setting.v1')).toBe('rollback-new')

    vi.restoreAllMocks()
    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      (value) => value === 'old' || value === 'rollback-new'
    )
    expect(localStorage.getItem('mpi.setting.v1')).toBe('rollback-new')
    expect(localStorage.getItem('perfinspect.setting.v1')).toBeNull()
  })

  it('recovers duplicate cross-window events without recreating the legacy key', async () => {
    const validate = (value: string) =>
      value === 'old' || value === 'mpi-edited' || value === 'rollback-new'
    localStorage.setItem('perfinspect.setting.v1', 'old')
    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      validate
    )
    localStorage.setItem('mpi.setting.v1', 'mpi-edited')

    const event = () =>
      new StorageEvent('storage', {
        key: 'perfinspect.setting.v1',
        newValue: 'rollback-new',
      })
    window.dispatchEvent(event())
    window.dispatchEvent(event())
    await new Promise<void>((resolve) => queueMicrotask(() => resolve()))

    expect(localStorage.getItem('mpi.setting.v1')).toBe('rollback-new')
    expect(localStorage.getItem('perfinspect.setting.v1')).toBeNull()
  })

  it('keeps an invalid legacy value when no valid destination exists', () => {
    localStorage.setItem('mpi.setting.v1', 'damaged-current')
    localStorage.setItem('perfinspect.setting.v1', 'damaged-legacy')

    migrateLegacyStorageKey(
      'mpi.setting.v1',
      'perfinspect.setting.v1',
      (value) => value.startsWith('valid:')
    )

    expect(localStorage.getItem('mpi.setting.v1')).toBe('damaged-current')
    expect(localStorage.getItem('perfinspect.setting.v1')).toBe(
      'damaged-legacy'
    )
  })
})
