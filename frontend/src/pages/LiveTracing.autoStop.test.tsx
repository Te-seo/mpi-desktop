import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LiveCaptureProvider } from '../components/LiveCaptureProvider'
import { LocaleProvider } from '../lib/i18n'
import type { BackendResponse } from '../lib/ipc'
import type { DeviceInfo } from '../lib/types'
import { LiveTracing } from './LiveTracing'

const ipc = vi.hoisted(() => ({
  call: vi.fn(),
  listeners: new Set<(message: BackendResponse) => void>(),
  fullscreenListeners: new Set<
    (state: { fullscreen: boolean; revision: number }) => void
  >(),
  getWindowFullscreen: vi.fn(),
  setWindowFullscreen: vi.fn(),
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
  getWindowFullscreen: ipc.getWindowFullscreen,
  setWindowFullscreen: ipc.setWindowFullscreen,
  onWindowFullscreenChanged(
    listener: (state: { fullscreen: boolean; revision: number }) => void
  ) {
    ipc.fullscreenListeners.add(listener)
    return () => ipc.fullscreenListeners.delete(listener)
  },
}))

const device: DeviceInfo = {
  platform: 'ios',
  serial: 'ios-1',
  model: 'iPhone',
  os_version: '19.0',
  connection_type: 'usb',
}

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
        onClick={() => onChange(device.serial, device)}
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

vi.mock('../components/WirelessSetup', () => ({
  WirelessSetup: () => null,
}))
vi.mock('../components/CloudPanel', () => ({ CloudPanel: () => null }))
vi.mock('../components/DeviceInfoCard', () => ({ DeviceInfoCard: () => null }))
vi.mock('../components/IosReadinessBadge', () => ({ IosReadinessBadge: () => null }))
vi.mock('../components/LiveCharts', () => ({ LiveCharts: () => null }))
vi.mock('../components/MetricVisibilityMenu', () => ({ MetricVisibilityMenu: () => null }))
vi.mock('../components/SessionNameModal', () => ({ SessionNameModal: () => null }))
vi.mock('../components/StatusBar', () => ({ StatusBar: () => null }))

function renderLive() {
  render(
    <LocaleProvider>
      <LiveCaptureProvider>
        <LiveTracing />
      </LiveCaptureProvider>
    </LocaleProvider>
  )
}

async function selectTarget() {
  fireEvent.click(screen.getByTestId('device-selector'))
  await waitFor(() => expect(screen.getByTestId('package-selector')).toBeEnabled())
  fireEvent.click(screen.getByTestId('package-selector'))
}

describe('LiveTracing automatic stop UI', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mpi.locale.v1', 'en')
    ipc.call.mockReset()
    ipc.listeners.clear()
    ipc.fullscreenListeners.clear()
    ipc.getWindowFullscreen
      .mockReset()
      .mockResolvedValue({ fullscreen: false, revision: 0 })
    ipc.setWindowFullscreen.mockImplementation(
      async (fullscreen: boolean, revision: number) => ({
        fullscreen,
        revision,
      })
    )
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') {
        return {
          capture_id: 'capture-1',
          device,
          package: { bundle_id: 'com.example.game' },
          duration_sec: 1,
        }
      }
      return {}
    })
  })

  it('toggles, validates, locks, and keeps manual stop app-preserving', async () => {
    renderLive()
    await selectTarget()

    const toggle = screen.getByRole('switch', {
      name: 'Use automatic measurement stop',
    })
    const hours = screen.getByRole('textbox', { name: 'Hours' })
    const minutes = screen.getByRole('textbox', { name: 'Minutes' })
    const seconds = screen.getByRole('textbox', { name: 'Seconds' })
    const start = screen.getByRole('button', { name: 'Start measurement' })

    expect(toggle).not.toBeChecked()
    expect(hours).toBeDisabled()
    expect(minutes).toBeDisabled()
    expect(seconds).toBeDisabled()
    expect(hours).toHaveValue('00')
    expect(minutes).toHaveValue('40')
    expect(seconds).toHaveValue('00')

    fireEvent.click(toggle)
    expect(toggle).toBeChecked()
    expect(hours).toBeEnabled()
    expect(minutes).toBeEnabled()
    expect(seconds).toBeEnabled()

    fireEvent.change(minutes, { target: { value: '00' } })
    fireEvent.change(seconds, { target: { value: '00' } })
    expect(hours).toHaveAttribute('aria-invalid', 'true')
    expect(minutes).toHaveAttribute('aria-invalid', 'true')
    expect(seconds).toHaveAttribute('aria-invalid', 'true')
    expect(start).toBeDisabled()

    fireEvent.change(seconds, { target: { value: '02' } })
    expect(hours).toHaveAttribute('aria-invalid', 'false')
    expect(minutes).toHaveAttribute('aria-invalid', 'false')
    expect(seconds).toHaveAttribute('aria-invalid', 'false')
    expect(start).toBeEnabled()

    fireEvent.click(start)
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('start_capture', {
        platform: 'ios',
        serial: 'ios-1',
        package: 'com.example.game',
        duration_sec: 1,
        auto_stop_after_sec: 2,
      })
    )
    expect(toggle).toBeDisabled()
    expect(hours).toBeDisabled()
    expect(minutes).toBeDisabled()
    expect(seconds).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Stop measurement' }))
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('stop_capture', { capture_id: 'capture-1' })
    )
    expect(
      ipc.call.mock.calls.filter(([command]) => command === 'terminate_app')
    ).toHaveLength(0)
  })

  it('serializes rapid native fullscreen toggles and ignores a stale initial read', async () => {
    let resolveInitial: (value: {
      fullscreen: boolean
      revision: number
    }) => void = () => {}
    ipc.getWindowFullscreen.mockImplementation(
      () =>
        new Promise<{ fullscreen: boolean; revision: number }>((resolve) => {
          resolveInitial = resolve
        })
    )
    renderLive()

    fireEvent.keyDown(window, { key: 'Enter', code: 'Enter', altKey: true })
    fireEvent.keyDown(window, { key: 'Enter', code: 'Enter', altKey: true })

    expect(ipc.setWindowFullscreen).toHaveBeenNthCalledWith(1, true, 1)
    expect(ipc.setWindowFullscreen).toHaveBeenNthCalledWith(2, false, 2)

    await act(async () => resolveInitial({ fullscreen: true, revision: 0 }))
    expect(screen.getByRole('button', { name: 'New measurement window' })).toBeInTheDocument()

    act(() => {
      for (const listener of ipc.fullscreenListeners) {
        listener({ fullscreen: true, revision: 1 })
      }
    })
    expect(screen.getByRole('button', { name: 'New measurement window' })).toBeInTheDocument()

    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' })
    expect(ipc.setWindowFullscreen).toHaveBeenLastCalledWith(false, 3)
    act(() => {
      for (const listener of ipc.fullscreenListeners) {
        listener({ fullscreen: false, revision: 3 })
      }
    })
    expect(screen.getByRole('button', { name: 'New measurement window' })).toBeInTheDocument()
  })
})
