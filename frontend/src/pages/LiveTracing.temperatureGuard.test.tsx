import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LiveCaptureProvider } from '../components/LiveCaptureProvider'
import { LocaleProvider } from '../lib/i18n'
import type { BackendResponse } from '../lib/ipc'
import { TEMPERATURE_GUARD_STORAGE_KEY } from '../lib/temperatureGuard'
import type { DeviceInfo } from '../lib/types'
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

const device: DeviceInfo = {
  platform: 'ios',
  serial: 'ios-1',
  model: 'iPhone',
  os_version: '19.0',
  connection_type: 'usb',
}

vi.mock('../components/DeviceSelector', () => ({
  DeviceSelector({
    disabled,
    onChange,
  }: {
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
  PackageSelector({
    disabled,
    onChange,
  }: {
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
  WirelessSetup({ disabled }: { disabled?: boolean }) {
    return <button disabled={disabled}>wireless device</button>
  },
}))
vi.mock('../components/CloudPanel', () => ({ CloudPanel: () => null }))
vi.mock('../components/DeviceInfoCard', () => ({ DeviceInfoCard: () => null }))
vi.mock('../components/IosReadinessBadge', () => ({ IosReadinessBadge: () => null }))
vi.mock('../components/LiveCharts', () => ({ LiveCharts: () => <div data-testid="charts" /> }))
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

function emit(message: BackendResponse) {
  act(() => {
    dispatch(message)
  })
}

function dispatch(message: BackendResponse) {
  for (const listener of ipc.listeners) listener(message)
}

function previewCreated(previewId = 'preview-1') {
  return {
    preview_id: previewId,
    state: 'preparing',
    device,
    package: {
      bundle_id: 'com.example.game',
      label: 'Example Game',
      version_name: null,
      version_code: null,
      apk_path: null,
      uid: null,
    },
  }
}

describe('LiveTracing temperature guard UI', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mpi.locale.v1', 'ko')
    ipc.call.mockReset()
    ipc.listeners.clear()
    ipc.call.mockImplementation(async (command: string, payload?: { preview_id?: string }) => {
      if (command === 'create_temperature_preview') return previewCreated()
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
      return {}
    })
  })

  it('keeps the OFF wording and exposes the specified threshold controls', async () => {
    renderLive()
    await selectTarget()

    expect(screen.getByRole('button', { name: '측정 시작' })).toBeEnabled()
    const toggle = screen.getByRole('switch', { name: '온도 기준 자동 시작 사용' })
    expect(toggle).not.toBeChecked()
    expect(screen.queryByTestId('temperature-guard-status')).not.toBeInTheDocument()
    const thumb = toggle.querySelector('span')
    expect(thumb).toHaveClass('left-0.5', 'translate-x-0')
    fireEvent.click(toggle)
    expect(toggle).toBeChecked()
    expect(thumb).toHaveClass('translate-x-4')
    const status = screen.getByTestId('temperature-guard-status')
    expect(within(status).getByText('현재 온도')).toBeInTheDocument()
    expect(within(status).getByText('확인 중…')).toBeInTheDocument()
    expect(within(status).getByText('기준 온도')).toBeInTheDocument()
    expect(within(status).getByText('25.0°C')).toBeInTheDocument()
    expect(within(status).getByText('0/2')).toBeInTheDocument()
    const threshold = screen.getByRole('spinbutton', { name: '기준 온도' })
    expect(threshold).toHaveValue(25)
    expect(threshold).toHaveAttribute('min', '5')
    expect(threshold).toHaveAttribute('max', '60')
    expect(threshold).toHaveAttribute('step', '0.1')
    expect(threshold).toBeEnabled()
    fireEvent.click(toggle)
    expect(toggle).not.toBeChecked()
    expect(screen.queryByTestId('temperature-guard-status')).not.toBeInTheDocument()
  })

  it('shows live idle temperature and offers an inline retry without a global error', async () => {
    renderLive()
    await selectTarget()
    fireEvent.click(screen.getByRole('switch', { name: '온도 기준 자동 시작 사용' }))

    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('activate_temperature_preview', {
        preview_id: 'preview-1',
      })
    )
    emit({
      id: null,
      ok: true,
      event: 'temperature_preview_sample',
      preview_id: 'preview-1',
      temperature_c: 38.4,
      sampled_at: '2026-07-21T00:00:00Z',
      source: 'battery',
    })
    expect(screen.getByText('38.4°C')).toBeInTheDocument()

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
    expect(screen.getByText('현재 배터리 온도를 확인하지 못했습니다.')).toBeInTheDocument()
    expect(screen.queryByText('38.4°C')).not.toBeInTheDocument()
    expect(
      within(screen.getByTestId('temperature-guard-status')).getByText('측정 불가')
    ).toBeInTheDocument()
    expect(screen.queryByText('temperature_unavailable: sensor failed')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }))
    await waitFor(() =>
      expect(
        ipc.call.mock.calls.filter(([command]) => command === 'create_temperature_preview')
      ).toHaveLength(2)
    )
  })

  it('shows waiting readings, locks selectors, and leaves elapsed time at zero', async () => {
    ipc.call.mockImplementation(async (command: string, payload?: { preview_id?: string }) => {
      if (command === 'create_temperature_preview') return previewCreated()
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
          device,
          package: {
            bundle_id: 'com.example.game',
            label: 'Example Game',
            version_name: null,
            version_code: null,
            apk_path: null,
            uid: null,
          },
        }
      }
      return {}
    })
    renderLive()
    await selectTarget()

    fireEvent.click(screen.getByRole('switch', { name: '온도 기준 자동 시작 사용' }))
    expect(screen.getByRole('button', { name: '측정 준비' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '측정 준비' }))

    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('activate_temperature_guard', {
        guard_id: 'guard-1',
      })
    )
    expect(screen.getByText('준비 중')).toBeInTheDocument()
    const preparingStatus = screen.getByTestId('temperature-guard-status')
    expect(within(preparingStatus).getByText('확인 중…')).toBeInTheDocument()
    expect(within(preparingStatus).getByText('25.0°C')).toBeInTheDocument()
    expect(within(preparingStatus).getByText('0/2')).toBeInTheDocument()
    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_state',
      guard_id: 'guard-1',
      state: 'waiting',
    })
    await screen.findByTestId('temperature-guard-status')
    expect(screen.getByRole('button', { name: '대기 취소' })).toBeEnabled()
    expect(screen.getByText('00:00:00')).toBeInTheDocument()
    expect(screen.getByTestId('device-selector')).toBeDisabled()
    expect(screen.getByTestId('package-selector')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'USB' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Wireless' })).toBeDisabled()
    expect(screen.getByRole('spinbutton', { name: '기준 온도' })).toBeDisabled()
    expect(screen.getByRole('switch', { name: '온도 기준 자동 시작 사용' })).toBeDisabled()

    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_sample',
      guard_id: 'guard-1',
      temperature_c: 39.9,
      sampled_at: '2026-07-20T00:00:00Z',
      source: 'battery',
      threshold_c: 40,
      consecutive_hits: 1,
      required_hits: 2,
    })

    expect(screen.getByText('39.9°C')).toBeInTheDocument()
    expect(screen.getByText('40.0°C')).toBeInTheDocument()
    expect(screen.getByText('1/2')).toBeInTheDocument()
    expect(screen.getByText('00:00:00')).toBeInTheDocument()
    expect(screen.getByTestId('charts')).toBeInTheDocument()

    emit({
      id: null,
      ok: true,
      event: 'temperature_guard_ended',
      guard_id: 'guard-1',
      reason: 'cancelled',
    })
    const resetStatus = screen.getByTestId('temperature-guard-status')
    expect(within(resetStatus).getByText('확인 중…')).toBeInTheDocument()
    expect(within(resetStatus).getByText('25.0°C')).toBeInTheDocument()
    expect(within(resetStatus).getByText('0/2')).toBeInTheDocument()
    expect(within(resetStatus).queryByText('39.9°C')).not.toBeInTheDocument()
  })

  it('restores preferences but never auto-resumes waiting after relaunch', async () => {
    localStorage.setItem(
      TEMPERATURE_GUARD_STORAGE_KEY,
      JSON.stringify({ enabled: true, thresholdC: 41.2 })
    )
    renderLive()
    await selectTarget()

    expect(screen.getByRole('switch', { name: '온도 기준 자동 시작 사용' })).toBeChecked()
    expect(screen.getByRole('spinbutton', { name: '기준 온도' })).toHaveValue(41.2)
    expect(screen.getByRole('button', { name: '측정 준비' })).toBeEnabled()
    const status = screen.getByTestId('temperature-guard-status')
    expect(within(status).getByText('현재 온도')).toBeInTheDocument()
    expect(within(status).getByText('확인 중…')).toBeInTheDocument()
    expect(within(status).getByText('41.2°C')).toBeInTheDocument()
    expect(within(status).getByText('0/2')).toBeInTheDocument()
    expect(ipc.call).not.toHaveBeenCalledWith('create_temperature_guard', expect.anything())
  })
})
