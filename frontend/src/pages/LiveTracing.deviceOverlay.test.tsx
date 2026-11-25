import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LiveCaptureProvider } from '../components/LiveCaptureProvider'
import { DEVICE_OVERLAY_STORAGE_KEY } from '../lib/deviceOverlay'
import type { BackendResponse } from '../lib/ipc'
import { TEMPERATURE_GUARD_STORAGE_KEY } from '../lib/temperatureGuard'
import type { DeviceInfo } from '../lib/types'
import { renderWithLocale } from '../test/utils'
import { LiveTracing } from './LiveTracing'

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
  openFiles: vi.fn().mockResolvedValue([]),
  openMeasurementWindow: vi.fn(),
  saveFile: vi.fn(),
  postToEndpoint: vi.fn(),
  getWindowFullscreen: vi.fn().mockResolvedValue({ fullscreen: false, revision: 0 }),
  setWindowFullscreen: vi.fn(
    async (fullscreen: boolean, revision: number) => ({ fullscreen, revision })
  ),
  onWindowFullscreenChanged: vi.fn(() => () => {}),
}))

const androidDevice: DeviceInfo = {
  platform: 'android',
  serial: 'android-1',
  model: 'Pixel',
  os_version: '16',
  connection_type: 'usb',
}

const secondAndroidDevice: DeviceInfo = {
  platform: 'android',
  serial: 'android-2',
  model: 'Galaxy',
  os_version: '15',
  connection_type: 'usb',
}

const iosDevice: DeviceInfo = {
  platform: 'ios',
  serial: 'ios-1',
  model: 'iPhone',
  os_version: '19.0',
  connection_type: 'usb',
}

// Selected per test, read when the mocked selector fires its onChange.
let selectedDevice: DeviceInfo = androidDevice

vi.mock('../components/DeviceSelector', () => ({
  DeviceSelector({ disabled, onChange }: {
    disabled?: boolean
    onChange(serial: string | null, selected: DeviceInfo | null): void
  }) {
    return (
      <button
        type="button"
        data-testid="device-selector"
        disabled={disabled}
        onClick={() => onChange(selectedDevice.serial, selectedDevice)}
      >
        device
      </button>
    )
  },
}))

vi.mock('../components/PackageSelector', () => ({
  PackageSelector({ disabled, onChange }: {
    disabled?: boolean
    onChange(bundleId: string | null): void
  }) {
    return (
      <button
        type="button"
        data-testid="package-selector"
        disabled={disabled}
        onClick={() => onChange('com.example.game')}
      >
        package
      </button>
    )
  },
}))

vi.mock('../components/WirelessSetup', () => ({ WirelessSetup: () => null }))
vi.mock('../components/CloudPanel', () => ({ CloudPanel: () => null }))
vi.mock('../components/DeviceInfoCard', () => ({ DeviceInfoCard: () => null }))
vi.mock('../components/IosReadinessBadge', () => ({ IosReadinessBadge: () => null }))
vi.mock('../components/LiveCharts', () => ({ LiveCharts: () => null }))
vi.mock('../components/MetricVisibilityMenu', () => ({ MetricVisibilityMenu: () => null }))
vi.mock('../components/SessionNameModal', () => ({ SessionNameModal: () => null }))
vi.mock('../components/StatusBar', () => ({ StatusBar: () => null }))

function renderLive() {
  renderWithLocale(
    <LiveCaptureProvider>
      <LiveTracing />
    </LiveCaptureProvider>
  )
}

async function selectTarget() {
  fireEvent.click(screen.getByTestId('device-selector'))
  await waitFor(() => expect(screen.getByTestId('package-selector')).toBeEnabled())
  fireEvent.click(screen.getByTestId('package-selector'))
}

function storedOverlay() {
  return JSON.parse(localStorage.getItem(DEVICE_OVERLAY_STORAGE_KEY) ?? '{}')
}

function dispatch(message: BackendResponse) {
  for (const listener of ipc.listeners) listener(message)
}

function emit(message: BackendResponse) {
  act(() => {
    dispatch(message)
  })
}

/** Drain the microtask queue so a resolved IPC promise's `.then` has actually
 *  run before asserting that it did nothing. */
async function settlePromises() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

const ALL_OVERLAY_METRICS = ['elapsed', 'fps', 'cpu', 'gpu', 'skin', 'net']

function enableOverlayInStorage() {
  localStorage.setItem(
    DEVICE_OVERLAY_STORAGE_KEY,
    JSON.stringify({
      enabled: true,
      metrics: { elapsed: true, fps: true, cpu: true, gpu: true, temp: true, net: true },
    })
  )
}

describe('LiveTracing on-device overlay card', () => {
  beforeEach(() => {
    localStorage.clear()
    selectedDevice = androidDevice
    ipc.call.mockReset()
    ipc.listeners.clear()
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') {
        return {
          capture_id: 'capture-1',
          device: selectedDevice,
          package: { bundle_id: 'com.example.game' },
          duration_sec: 0.5,
        }
      }
      return {}
    })
  })

  it('can be configured before an Android device is connected', async () => {
    renderLive()

    const toggle = screen.getByRole('switch', { name: 'Use the on-device metrics overlay' })
    const network = screen.getByRole('checkbox', { name: 'Network' })
    expect(toggle).not.toBeChecked()
    expect(network).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Skin temperature' })).toBeChecked()

    fireEvent.click(toggle)
    fireEvent.click(network)

    expect(toggle).toBeChecked()
    expect(network).not.toBeChecked()
    await waitFor(() =>
      expect(storedOverlay()).toEqual({
        enabled: true,
        metrics: { elapsed: true, fps: true, cpu: true, gpu: true, skin: true, net: false },
      })
    )
    expect(ipc.call).not.toHaveBeenCalledWith(
      'companion_overlay_status',
      expect.anything(),
      expect.anything()
    )
  })

  it('stays hidden for an iOS device', async () => {
    selectedDevice = iosDevice
    renderLive()
    await selectTarget()

    expect(
      screen.queryByRole('switch', { name: 'Use the on-device metrics overlay' })
    ).toBeNull()
  })

  it('toggles metrics for an Android device and persists them', async () => {
    renderLive()
    await selectTarget()

    const toggle = screen.getByRole('switch', { name: 'Use the on-device metrics overlay' })
    const network = screen.getByRole('checkbox', { name: 'Network' })

    expect(toggle).not.toBeChecked()
    expect(network).toBeChecked()
    expect(network).toBeDisabled()

    fireEvent.click(toggle)
    expect(toggle).toBeChecked()
    expect(network).toBeEnabled()

    fireEvent.click(network)
    expect(network).not.toBeChecked()
    await waitFor(() =>
      expect(storedOverlay()).toEqual({
        enabled: true,
        metrics: { elapsed: true, fps: true, cpu: true, gpu: true, skin: true, net: false },
      })
    )
  })

  it('explains why the HUD cannot show, but only once it is asked for', async () => {
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'companion_overlay_status') {
        return {
          serial: 'android-1',
          installed: true,
          supported: true,
          can_draw_overlays: false,
          reason: 'overlay_permission_denied',
        }
      }
      return {}
    })
    renderLive()
    await selectTarget()

    // The probe answers on device selection, but a switched-off HUD is not a
    // problem worth reporting.
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith(
        'companion_overlay_status',
        { serial: 'android-1' },
        30_000
      )
    )
    expect(screen.queryByRole('status')).toBeNull()

    fireEvent.click(screen.getByRole('switch', { name: 'Use the on-device metrics overlay' }))
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/Display over other apps/)
    )
  })

  it('reports a missing companion as unavailable', async () => {
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'companion_overlay_status') {
        return { serial: 'android-1', installed: false, reason: 'companion_not_installed' }
      }
      return {}
    })
    localStorage.setItem(
      DEVICE_OVERLAY_STORAGE_KEY,
      JSON.stringify({
        enabled: true,
        metrics: { elapsed: true, fps: true, cpu: true, gpu: true, temp: true, net: true },
      })
    )
    renderLive()
    await selectTarget()

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/Install MPI from Google Play/i)
    )
  })

  it('refreshes a newly started capture until desktop login is ready and ignores the old selection probe', async () => {
    let releaseSelection!: (status: object) => void
    const selection = new Promise<object>((resolve) => { releaseSelection = resolve })
    const status = vi.fn()
      .mockReturnValueOnce(selection)
      .mockResolvedValueOnce({ installed: true, reason: 'companion_sign_in_required' })
      .mockResolvedValue({ installed: true, supported: true, can_draw_overlays: true })
    const original = ipc.call.getMockImplementation()!
    ipc.call.mockImplementation((command, ...args) =>
      command === 'companion_overlay_status' ? status(...args) : original(command, ...args))
    enableOverlayInStorage()
    renderLive()
    await selectTarget()
    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Start measurement' }))
      await settlePromises()
      expect(screen.getByRole('status')).toHaveTextContent(/Sign in/i)
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000) })
      expect(screen.getByRole('status')).toHaveTextContent(/overlay is available/i)
      releaseSelection({ installed: false, reason: 'companion_not_installed' })
      await settlePromises()
      expect(screen.getByRole('status')).toHaveTextContent(/overlay is available/i)
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
      expect(status.mock.calls).toEqual(Array(3).fill([{ serial: 'android-1' }, 30_000]))
    } finally {
      vi.useRealTimers()
    }
  })

  it('starts the helper only on explicit Connect and ends warm login checks on permission denial', async () => {
    const status = vi.fn()
      .mockResolvedValueOnce({ installed: true, reason: 'companion_unreachable' })
      .mockResolvedValueOnce({ installed: true, reason: 'companion_sign_in_required' })
      .mockResolvedValue({ installed: true, supported: true, reason: 'overlay_permission_denied' })
    ipc.call.mockImplementation((command, ...args) =>
      command === 'companion_overlay_status' ? status(...args) : Promise.resolve({}))
    enableOverlayInStorage()
    renderLive()
    await selectTarget()
    const connect = await screen.findByRole('button', { name: 'Connect MPI' })
    vi.useFakeTimers()
    try {
      fireEvent.click(connect)
      await settlePromises()
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(screen.getByRole('status')).toHaveTextContent(/Display over other apps/)
      expect(status.mock.calls).toEqual([
        [{ serial: 'android-1' }, 30_000],
        [{ serial: 'android-1', connect: true }, 30_000],
        [{ serial: 'android-1' }, 30_000],
      ])
    } finally {
      vi.useRealTimers()
    }
  })

  it('sends the selection with start_capture and live-updates a running capture', async () => {
    localStorage.setItem(
      DEVICE_OVERLAY_STORAGE_KEY,
      JSON.stringify({
        enabled: true,
        metrics: { elapsed: true, fps: true, cpu: false, gpu: true, temp: true, net: true },
      })
    )
    renderLive()
    await selectTarget()

    fireEvent.click(screen.getByRole('button', { name: 'Start measurement' }))
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('start_capture', {
        platform: 'android',
        serial: 'android-1',
        package: 'com.example.game',
        duration_sec: 0.5,
        overlay: { enabled: true, metrics: ['elapsed', 'fps', 'gpu', 'skin', 'net'] },
      })
    )

    // The master switch stays live while the capture holds the device.
    const toggle = screen.getByRole('switch', { name: 'Use the on-device metrics overlay' })
    expect(toggle).toBeEnabled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'CPU' }))

    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('set_capture_overlay', {
        capture_id: 'capture-1',
        enabled: true,
        metrics: ['elapsed', 'fps', 'cpu', 'gpu', 'skin', 'net'],
      })
    )
  })

  it('drops the previous phone warning on an Android-to-Android switch', async () => {
    ipc.call.mockImplementation(async (command: string, payload?: { serial?: string }) => {
      if (command === 'companion_overlay_status') {
        if (payload?.serial === androidDevice.serial) {
          return { serial: androidDevice.serial, installed: false, reason: 'companion_not_installed' }
        }
        // The second phone is unreachable, not missing the Play app.
        throw new Error('probe failed')
      }
      return {}
    })
    enableOverlayInStorage()
    renderLive()
    await selectTarget()

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/Install MPI from Google Play/i)
    )

    selectedDevice = secondAndroidDevice
    fireEvent.click(screen.getByTestId('device-selector'))
    expect(screen.queryByRole('status')).toBeNull()

    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith(
        'companion_overlay_status',
        { serial: 'android-2' },
        30_000
      )
    )
    await settlePromises()
    expect(screen.getByRole('status')).toHaveTextContent('Device not reachable. Check the USB connection.')
  })

  it('ignores a probe that lands after the user moved to another phone', async () => {
    let releaseProbe: () => void = () => {}
    const probeGate = new Promise<void>((resolve) => {
      releaseProbe = resolve
    })
    ipc.call.mockImplementation(async (command: string, payload?: { serial?: string }) => {
      if (command === 'companion_overlay_status') {
        if (payload?.serial === androidDevice.serial) {
          // The first phone's read returns after the second phone already answered.
          await probeGate
          return { serial: androidDevice.serial, installed: false, reason: 'companion_not_installed' }
        }
        return {
          serial: payload?.serial,
          installed: true,
          supported: true,
          can_draw_overlays: false,
          reason: 'overlay_permission_denied',
        }
      }
      return {}
    })
    enableOverlayInStorage()
    renderLive()
    await selectTarget()

    selectedDevice = secondAndroidDevice
    fireEvent.click(screen.getByTestId('device-selector'))
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/Display over other apps/)
    )

    releaseProbe()
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith(
        'companion_overlay_status',
        { serial: 'android-1' },
        30_000
      )
    )
    await settlePromises()
    // Still the selected phone's verdict, not the late answer for the old one.
    expect(screen.getByRole('status')).toHaveTextContent(/Display over other apps/)
  })

  it('applies a HUD toggle made while start_capture was still in flight', async () => {
    let releaseStart: () => void = () => {}
    const startGate = new Promise<void>((resolve) => {
      releaseStart = resolve
    })
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') {
        await startGate
        return {
          capture_id: 'capture-1',
          device: androidDevice,
          package: { bundle_id: 'com.example.game' },
          duration_sec: 0.5,
        }
      }
      return {}
    })
    renderLive()
    await selectTarget()

    fireEvent.click(screen.getByRole('button', { name: 'Start measurement' }))
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('start_capture', {
        platform: 'android',
        serial: 'android-1',
        package: 'com.example.game',
        duration_sec: 0.5,
        overlay: { enabled: false, metrics: ALL_OVERLAY_METRICS },
      })
    )

    // The card is live during the round trip, but there is no capture id to
    // target yet.
    fireEvent.click(screen.getByRole('switch', { name: 'Use the on-device metrics overlay' }))
    expect(ipc.call).not.toHaveBeenCalledWith('set_capture_overlay', expect.anything())

    releaseStart()
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('set_capture_overlay', {
        capture_id: 'capture-1',
        enabled: true,
        metrics: ALL_OVERLAY_METRICS,
      })
    )
  })

  it('applies a HUD toggle made while the temperature guard was waiting', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 25 })
    )
    let bridgeReady = false
    ipc.call.mockImplementation(async (command: string, payload?: { preview_id?: string }) => {
      if (command === 'companion_overlay_status') {
        return bridgeReady ? { installed: true, supported: true, can_draw_overlays: true }
          : { installed: true, reason: 'companion_unreachable' }
      }
      if (command === 'create_temperature_preview') {
        return {
          preview_id: 'preview-1',
          state: 'preparing',
          device: androidDevice,
          package: { bundle_id: 'com.example.game' },
        }
      }
      if (command === 'stop_temperature_preview') {
        dispatch({
          id: null,
          ok: true,
          event: 'temperature_preview_ended',
          preview_id: payload?.preview_id,
          reason: 'cancelled',
        })
        return { status: 'stopping' }
      }
      if (command === 'create_temperature_guard') {
        return {
          guard_id: 'guard-1',
          device: androidDevice,
          package: { bundle_id: 'com.example.game' },
        }
      }
      return {}
    })
    renderLive()
    await selectTarget()
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('activate_temperature_preview', {
        preview_id: 'preview-1',
      })
    )

    fireEvent.click(screen.getByRole('button', { name: 'Prepare measurement' }))
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('activate_temperature_guard', { guard_id: 'guard-1' })
    )
    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_state',
      guard_id: 'guard-1',
      state: 'waiting',
    })

    // The guard sits here until the phone cools; the HUD set now must reach the
    // capture it eventually launches, not the config frozen at create time.
    fireEvent.click(screen.getByRole('switch', { name: 'Use the on-device metrics overlay' }))
    expect(ipc.call).not.toHaveBeenCalledWith('set_capture_overlay', expect.anything())

    bridgeReady = true
    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_started',
      guard_id: 'guard-1',
      capture_id: 'capture-1',
      device: androidDevice,
      package: { bundle_id: 'com.example.game' },
    })

    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('set_capture_overlay', {
        capture_id: 'capture-1',
        enabled: true,
        metrics: ALL_OVERLAY_METRICS,
      })
    )
    expect(screen.getByRole('status')).toHaveTextContent(/overlay is available/i)
  })
})
