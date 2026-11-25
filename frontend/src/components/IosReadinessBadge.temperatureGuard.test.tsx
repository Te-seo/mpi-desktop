import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LocaleProvider } from '../lib/i18n'
import { IosReadinessBadge } from './IosReadinessBadge'

const ipc = vi.hoisted(() => ({
  call: vi.fn(),
  iosTunnelStatus: vi.fn(),
  iosTunnelInstall: vi.fn(),
}))

vi.mock('../lib/ipc', () => ({
  call: ipc.call,
  iosTunnelStatus: ipc.iosTunnelStatus,
  iosTunnelInstall: ipc.iosTunnelInstall,
}))

function renderBadge(temperatureGuardEnabled: boolean) {
  return render(
    <LocaleProvider>
      <IosReadinessBadge
        connectionMode="usb"
        deviceConnectionType="usb"
        deviceSerial="ios-1"
        temperatureGuardEnabled={temperatureGuardEnabled}
      />
    </LocaleProvider>
  )
}

describe('IosReadinessBadge temperature guard tooling', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mpi.locale.v1', 'en')
    ipc.call.mockReset()
    ipc.iosTunnelStatus.mockReset().mockResolvedValue({
      supported: false,
      tunneldRunning: false,
      tunnels: 0,
      taskInstalled: false,
    })
    ipc.iosTunnelInstall.mockReset().mockResolvedValue({ ok: true })
  })

  it('keeps ordinary macOS capture on the existing xcrun preflight', async () => {
    ipc.call.mockResolvedValue({
      platform: 'darwin',
      tool: 'xcrun',
      available: true,
      version: '16.0',
    })

    const { container } = renderBadge(false)

    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith('ios_preflight', {}, 45_000)
    )
    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })

  it('uses guard-specific pmd3 preflight and install commands on macOS', async () => {
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'ios_temperature_guard_install_tool') {
        return {
          platform: 'darwin',
          tool: 'pymobiledevice3',
          available: true,
          version: '9.4.0',
        }
      }
      return {
        platform: 'darwin',
        tool: 'pymobiledevice3',
        available: false,
        installable: true,
        error: 'pmd3 9.x required',
      }
    })

    renderBadge(true)

    expect(await screen.findByText('pymobiledevice3 missing')).toBeInTheDocument()
    expect(ipc.call).toHaveBeenCalledWith(
      'ios_temperature_guard_preflight',
      {},
      45_000
    )
    fireEvent.click(screen.getByRole('button', { name: 'Auto Install' }))
    await waitFor(() =>
      expect(ipc.call).toHaveBeenCalledWith(
        'ios_temperature_guard_install_tool',
        {},
        900_000
      )
    )
  })
})
