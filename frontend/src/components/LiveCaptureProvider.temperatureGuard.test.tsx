import { act, render, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LocaleProvider } from '../lib/i18n'
import type { BackendResponse } from '../lib/ipc'
import type { LiveCaptureState } from '../lib/liveCaptureState'
import { TEMPERATURE_GUARD_STORAGE_KEY } from '../lib/temperatureGuard'
import type { DeviceInfo, PackageInfo } from '../lib/types'
import { LiveCaptureProvider } from './LiveCaptureProvider'
import { useLiveCapture } from '../lib/liveCaptureState'

const ipc = vi.hoisted(() => ({
  call: vi.fn(),
  listeners: new Set<(message: BackendResponse) => void>(),
}))

vi.mock('../lib/ipc', () => ({
  call: ipc.call,
  onEvent(listener: (message: BackendResponse) => void) {
    ipc.listeners.add(listener)
    return () => ipc.listeners.delete(listener)
  },
  saveFile: vi.fn(),
  postToEndpoint: vi.fn(),
}))

const device: DeviceInfo = {
  platform: 'ios',
  serial: 'ios-1',
  model: 'iPhone',
  os_version: '19.0',
  connection_type: 'wireless',
}

const selectedPackage: PackageInfo = {
  bundle_id: 'com.example.game',
  label: 'Example Game',
  version_name: '1.0',
  version_code: '1',
  apk_path: null,
  uid: null,
}

let capture: LiveCaptureState

function Harness() {
  const state = useLiveCapture()
  useEffect(() => {
    capture = state
  }, [state])
  return <div data-testid="phase">{state.phase}</div>
}

function renderProvider() {
  render(
    <LocaleProvider>
      <LiveCaptureProvider>
        <Harness />
      </LiveCaptureProvider>
    </LocaleProvider>
  )
}

async function selectTarget() {
  await act(async () => {
    capture.setDeviceSelection(device.serial, device)
  })
  await act(async () => {
    capture.setBundleId(selectedPackage.bundle_id)
  })
}

function emit(message: BackendResponse) {
  act(() => {
    dispatch(message)
  })
}

function dispatch(message: BackendResponse) {
  for (const listener of ipc.listeners) listener(message)
}

function previewCreated(previewId: string) {
  return {
    preview_id: previewId,
    state: 'preparing',
    device,
    package: selectedPackage,
  }
}

function endPreview(previewId: string) {
  dispatch({
    id: null,
    ok: true,
    event: 'temperature_preview_ended',
    preview_id: previewId,
    reason: 'cancelled',
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('LiveCaptureProvider temperature guard', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mpi.locale.v1', 'en')
    ipc.call.mockReset()
    ipc.listeners.clear()
  })

  it('preserves the manual start command when the feature is OFF', async () => {
    ipc.call.mockResolvedValue({
      capture_id: 'manual-capture',
      device,
      package: selectedPackage,
      duration_sec: 1,
    })
    renderProvider()
    await selectTarget()

    await act(async () => capture.start())

    expect(ipc.call).toHaveBeenCalledWith('start_capture', {
      platform: 'ios',
      serial: 'ios-1',
      package: 'com.example.game',
      duration_sec: 1,
    })
    expect(capture.phase).toBe('running')
    expect(capture.captureId).toBe('manual-capture')
    expect(capture.temperatureGuardEnabled).toBe(false)
  })

  it('streams an idle preview, rejects stale IDs, and does not restart for threshold edits', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 40 })
    )
    ipc.call.mockImplementation(async (command: string, payload?: { preview_id?: string }) => {
      if (command === 'create_temperature_preview') return previewCreated('preview-current')
      if (command === 'stop_temperature_preview') {
        endPreview(payload?.preview_id ?? '')
        return { status: 'stopping' }
      }
      return {}
    })
    renderProvider()
    await selectTarget()

    await waitFor(() => expect(capture.temperaturePreviewId).toBe('preview-current'))
    expect(ipc.call).toHaveBeenCalledWith(
      'create_temperature_preview',
      { platform: 'ios', serial: 'ios-1', package: 'com.example.game' },
      120_000
    )
    expect(ipc.call).toHaveBeenCalledWith('activate_temperature_preview', {
      preview_id: 'preview-current',
    })

    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_sample',
      preview_id: 'preview-stale',
      temperature_c: 55,
      sampled_at: '2026-07-21T00:00:00Z',
      source: 'battery',
    })
    expect(capture.temperaturePreviewSample).toBeNull()

    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_sample',
      preview_id: 'preview-current',
      temperature_c: 39.2,
      sampled_at: '2026-07-21T00:00:01Z',
      source: 'battery',
    })
    expect(capture.temperaturePreviewSample?.temperatureC).toBe(39.2)
    expect(capture.temperaturePreviewLoading).toBe(false)

    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_ended',
      preview_id: 'preview-stale',
      reason: 'cancelled',
    })
    expect(capture.temperaturePreviewId).toBe('preview-current')
    expect(capture.temperaturePreviewSample?.temperatureC).toBe(39.2)

    await act(async () => capture.setTemperatureGuardThresholdC(41.5))

    expect(capture.temperatureGuardThresholdC).toBe(41.5)
    expect(capture.temperaturePreviewSample?.temperatureC).toBe(39.2)
    expect(
      ipc.call.mock.calls.filter(([command]) => command === 'create_temperature_preview')
    ).toHaveLength(1)
    expect(
      ipc.call.mock.calls.filter(([command]) => command === 'stop_temperature_preview')
    ).toHaveLength(0)
  })

  it('waits for preview_ended before creating the temperature guard', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 40 })
    )
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'create_temperature_preview') return previewCreated('preview-1')
      if (command === 'stop_temperature_preview') return { status: 'stopping' }
      if (command === 'create_temperature_guard') {
        return { guard_id: 'guard-1', device, package: selectedPackage, duration_sec: 1 }
      }
      return {}
    })
    renderProvider()
    await selectTarget()
    await waitFor(() => expect(capture.temperaturePreviewId).toBe('preview-1'))

    let startPromise!: Promise<void>
    act(() => {
      startPromise = capture.start()
    })
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('stop_temperature_preview', {
        preview_id: 'preview-1',
      })
    )
    expect(ipc.call).not.toHaveBeenCalledWith('create_temperature_guard', expect.anything())

    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_ended',
      preview_id: 'preview-1',
      reason: 'cancelled',
    })
    await act(async () => startPromise)

    const stopIndex = ipc.call.mock.calls.findIndex(
      ([command]) => command === 'stop_temperature_preview'
    )
    const guardIndex = ipc.call.mock.calls.findIndex(
      ([command]) => command === 'create_temperature_guard'
    )
    expect(stopIndex).toBeGreaterThanOrEqual(0)
    expect(guardIndex).toBeGreaterThan(stopIndex)
    expect(capture.guardId).toBe('guard-1')
  })

  it('stops a late preview create response without activating it', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 40 })
    )
    const pendingPreview = deferred<ReturnType<typeof previewCreated>>()
    ipc.call.mockImplementation((command: string, payload?: { preview_id?: string }) => {
      if (command === 'create_temperature_preview') return pendingPreview.promise
      if (command === 'stop_temperature_preview') {
        endPreview(payload?.preview_id ?? '')
        return Promise.resolve({ status: 'stopping' })
      }
      return Promise.resolve({})
    })
    renderProvider()
    await selectTarget()
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith(
        'create_temperature_preview',
        expect.anything(),
        120_000
      )
    )

    await act(async () => capture.setTemperatureGuardEnabled(false))
    await act(async () => pendingPreview.resolve(previewCreated('preview-late')))

    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('stop_temperature_preview', {
        preview_id: 'preview-late',
      })
    )
    expect(ipc.call).not.toHaveBeenCalledWith('activate_temperature_preview', {
      preview_id: 'preview-late',
    })
    expect(capture.temperaturePreviewId).toBeNull()
    expect(capture.temperaturePreviewSample).toBeNull()
  })

  it('keeps preview errors separate and retries only on demand', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 40 })
    )
    let createCount = 0
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'create_temperature_preview') {
        createCount += 1
        return previewCreated(`preview-${createCount}`)
      }
      return {}
    })
    renderProvider()
    await selectTarget()
    await waitFor(() => expect(capture.temperaturePreviewId).toBe('preview-1'))

    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_sample',
      preview_id: 'preview-1',
      temperature_c: 39.2,
      sampled_at: '2026-07-21T00:00:00Z',
      source: 'battery',
    })
    expect(capture.temperaturePreviewSample?.temperatureC).toBe(39.2)

    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_error',
      preview_id: 'preview-1',
      code: 'temperature_unavailable',
      error: 'sensor failed',
    })
    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_ended',
      preview_id: 'preview-1',
      reason: 'error',
    })

    expect(capture.error).toBeNull()
    expect(capture.phase).toBe('idle')
    expect(capture.temperaturePreviewId).toBeNull()
    expect(capture.temperaturePreviewSample).toBeNull()
    expect(capture.temperaturePreviewError).toContain('temperature_unavailable')
    expect(createCount).toBe(1)

    await act(async () => capture.retryTemperaturePreview())
    await waitFor(() => expect(capture.temperaturePreviewId).toBe('preview-2'))
    expect(createCount).toBe(2)
    expect(capture.temperaturePreviewError).toBeNull()
  })

  it('keeps waiting samples out of capture data/time and rejects stale IDs', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 40 })
    )
    ipc.call.mockImplementation(async (command: string, payload?: { preview_id?: string }) => {
      if (command === 'create_temperature_preview') return previewCreated('preview-before-guard')
      if (command === 'stop_temperature_preview') {
        endPreview(payload?.preview_id ?? '')
        return { status: 'stopping' }
      }
      if (command === 'create_temperature_guard') {
        return { guard_id: 'guard-current', device, package: selectedPackage, duration_sec: 1 }
      }
      return {}
    })
    renderProvider()
    await selectTarget()

    await act(async () => capture.start())

    expect(ipc.call).toHaveBeenCalledWith(
      'create_temperature_guard',
      {
        platform: 'ios',
        serial: 'ios-1',
        package: 'com.example.game',
        threshold_c: 40,
        required_hits: 2,
        cold_start: true,
        duration_sec: 1,
      },
      120_000
    )
    expect(ipc.call).toHaveBeenCalledWith('activate_temperature_guard', {
      guard_id: 'guard-current',
    })
    // The create response only reserves the device. Do not claim waiting until
    // the backend confirms that slow monitor preparation actually completed.
    expect(capture.phase).toBe('preparing')
    expect(capture.starting).toBe(true)
    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_state',
      guard_id: 'guard-current',
      state: 'waiting',
    })
    expect(capture.phase).toBe('waiting')
    expect(capture.starting).toBe(false)
    expect(capture.selectionLocked).toBe(true)
    expect(capture.snapshots).toHaveLength(0)
    expect(capture.elapsedSec).toBe(0)
    expect(Object.isFrozen(capture.guardSnapshot)).toBe(true)

    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_sample',
      guard_id: 'guard-stale',
      temperature_c: 55,
      sampled_at: '2026-07-20T00:00:00Z',
      source: 'battery',
      threshold_c: 40,
      consecutive_hits: 2,
      required_hits: 2,
    })
    expect(capture.guardSample).toBeNull()

    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_sample',
      guard_id: 'guard-current',
      temperature_c: 40,
      sampled_at: '2026-07-20T00:00:01Z',
      source: 'battery',
      threshold_c: 40,
      consecutive_hits: 1,
      required_hits: 2,
    })
    expect(capture.guardSample?.temperatureC).toBe(40)
    expect(capture.snapshots).toHaveLength(0)
    expect(capture.elapsedSec).toBe(0)

    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_started',
      guard_id: 'guard-stale',
      capture_id: 'capture-stale',
    })
    expect(capture.captureId).toBeNull()

    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_started',
      guard_id: 'guard-current',
      capture_id: 'capture-current',
      trigger_temperature_c: 40.1,
      device,
      package: selectedPackage,
      duration_sec: 1,
    })
    expect(capture.phase).toBe('running')
    expect(capture.guardId).toBeNull()
    expect(capture.captureId).toBe('capture-current')

    emit({
      id: null,
      ok: true,
      event: 'snapshot',
      capture_id: 'capture-stale',
      data: { captured_at: 'stale' },
    })
    expect(capture.snapshots).toHaveLength(0)
    emit({
      id: null,
      ok: true,
      event: 'snapshot',
      capture_id: 'capture-current',
      data: { captured_at: 'current' },
    })
    expect(capture.snapshots).toHaveLength(1)
  })

  it('stops a created guard when activation fails', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 40 })
    )
    ipc.call.mockImplementation(async (command: string, payload?: { preview_id?: string }) => {
      if (command === 'create_temperature_preview') return previewCreated('preview-before-guard')
      if (command === 'stop_temperature_preview') {
        endPreview(payload?.preview_id ?? '')
        return { status: 'stopping' }
      }
      if (command === 'create_temperature_guard') {
        return { guard_id: 'guard-1', device, package: selectedPackage }
      }
      if (command === 'activate_temperature_guard') throw new Error('activation failed')
      return {}
    })
    renderProvider()
    await selectTarget()

    await act(async () => capture.start())

    expect(ipc.call).toHaveBeenCalledWith('stop_temperature_guard', {
      guard_id: 'guard-1',
    })
    expect(capture.guardId).toBeNull()
    expect(capture.phase).toBe('error')
  })

  it.each([false, true])(
    'settles a cancelled %s start when the pending create/open rejects',
    async (guardEnabled) => {
      if (guardEnabled) {
        localStorage.setItem(
          TEMPERATURE_GUARD_STORAGE_KEY,
          JSON.stringify({ enabled: true, thresholdC: 40 })
        )
      }
      const pending = deferred<never>()
      ipc.call.mockImplementation((command: string, payload?: { preview_id?: string }) => {
        if (command === 'create_temperature_preview') {
          return Promise.resolve(previewCreated('preview-before-start'))
        }
        if (command === 'stop_temperature_preview') {
          endPreview(payload?.preview_id ?? '')
          return Promise.resolve({ status: 'stopping' })
        }
        if (command === 'create_temperature_guard' || command === 'start_capture') {
          return pending.promise
        }
        return Promise.resolve({})
      })
      renderProvider()
      await selectTarget()

      let startPromise!: Promise<void>
      act(() => {
        startPromise = capture.start()
      })
      expect(capture.phase).toBe('preparing')
      await waitFor(() =>
        expect(ipc.call).toHaveBeenCalledWith(
          guardEnabled ? 'create_temperature_guard' : 'start_capture',
          expect.anything(),
          ...(guardEnabled ? [120_000] : [])
        )
      )
      await act(async () => capture.stop())
      expect(capture.phase).toBe('stopping')

      await act(async () => {
        pending.reject(new Error('open failed after cancel'))
        await startPromise
      })

      expect(capture.phase).toBe('idle')
      expect(capture.starting).toBe(false)
      expect(capture.stopping).toBe(false)
      expect(capture.selectionLocked).toBe(false)
    }
  )
})
