import { describe, expect, it } from 'vitest'
import { WindowOwnership } from './ownership'

describe('Electron resource ownership routing', () => {
  it('binds a created preview to its request window and removes it on end', () => {
    const ownership = new WindowOwnership()
    ownership.rememberRequest('preview-request', 7, 'create_temperature_preview')

    const request = ownership.takeRequest('preview-request')
    expect(request).toEqual({ ownerId: 7, kind: 'preview' })
    ownership.bindPreview('preview-1', request.ownerId!)

    for (const event of [
      'temperature_preview_state',
      'temperature_preview_sample',
      'temperature_preview_error',
    ]) {
      expect(ownership.routeEvent(event, null, null, 'preview-1')).toEqual({
        ownerId: 7,
        orphanedCaptureId: null,
      })
    }
    expect(
      ownership.routeEvent('temperature_preview_ended', null, null, 'preview-1')
    ).toEqual({ ownerId: 7, orphanedCaptureId: null })
    expect(ownership.ownerForPreview('preview-1')).toBeNull()
    expect(
      ownership.routeEvent('temperature_preview_sample', null, null, 'preview-1').ownerId
    ).toBeNull()
  })

  it('binds a created guard to its request window', () => {
    const ownership = new WindowOwnership()
    ownership.rememberRequest('request-1', 7, 'create_temperature_guard')

    const request = ownership.takeRequest('request-1')
    expect(request).toEqual({ ownerId: 7, kind: 'guard' })
    ownership.bindGuard('guard-1', request.ownerId!)

    expect(ownership.routeEvent('temperature_guard_sample', 'guard-1', null)).toEqual({
      ownerId: 7,
      orphanedCaptureId: null,
    })
  })

  it('atomically transfers guard ownership to the triggered capture', () => {
    const ownership = new WindowOwnership()
    ownership.bindGuard('guard-1', 7)

    expect(
      ownership.routeEvent('temperature_guard_started', 'guard-1', 'capture-1')
    ).toEqual({ ownerId: 7, orphanedCaptureId: null })
    expect(ownership.ownerForGuard('guard-1')).toBeNull()
    expect(ownership.ownerForCapture('capture-1')).toBe(7)
    expect(ownership.routeEvent('snapshot', null, 'capture-1').ownerId).toBe(7)
    expect(ownership.routeEvent('temperature_guard_sample', 'guard-1', null).ownerId).toBeNull()
  })

  it('marks a trigger as orphaned when its window already closed', () => {
    const ownership = new WindowOwnership()

    expect(
      ownership.routeEvent('temperature_guard_started', 'stale-guard', 'capture-2')
    ).toEqual({ ownerId: null, orphanedCaptureId: 'capture-2' })
    expect(ownership.ownerForCapture('capture-2')).toBeNull()
  })

  it('undoes a guard transfer when delivery loses the window race', () => {
    const ownership = new WindowOwnership()
    ownership.bindGuard('guard-1', 7)
    const route = ownership.routeEvent(
      'temperature_guard_started',
      'guard-1',
      'capture-1'
    )
    expect(route.ownerId).toBe(7)
    expect(ownership.ownerForCapture('capture-1')).toBe(7)

    expect(
      ownership.failEventDelivery('temperature_guard_started', 'capture-1')
    ).toBe('capture-1')
    expect(ownership.ownerForCapture('capture-1')).toBeNull()
  })

  it('releases previews, waiting guards, and captures on window close', () => {
    const ownership = new WindowOwnership()
    ownership.bindGuard('guard-1', 7)
    ownership.bindCapture('capture-1', 7)
    ownership.bindPreview('preview-1', 7)
    ownership.bindGuard('guard-other', 8)
    ownership.bindPreview('preview-other', 8)

    expect(ownership.releaseWindow(7)).toEqual({
      captureIds: ['capture-1'],
      guardIds: ['guard-1'],
      previewIds: ['preview-1'],
    })
    expect(ownership.ownerForGuard('guard-other')).toBe(8)
    expect(ownership.ownerForPreview('preview-other')).toBe(8)
  })

  it.each([
    ['create_temperature_guard', 'guard'],
    ['create_temperature_preview', 'preview'],
    ['start_capture', 'capture'],
  ] as const)('keeps the %s request kind after its window closes', (command, kind) => {
    const ownership = new WindowOwnership()
    ownership.rememberRequest('pending-request', 7, command)

    ownership.releaseWindow(7)

    expect(ownership.takeRequest('pending-request')).toEqual({ ownerId: null, kind })
  })
})
