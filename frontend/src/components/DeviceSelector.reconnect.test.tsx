import { act, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const callMock = vi.hoisted(() => vi.fn())
vi.mock('../lib/ipc', () => ({ call: callMock }))
vi.mock('../lib/i18n', () => ({
  useMessages: () => ({
    deviceSelector: { defaultLabel: 'Device', refreshDevices: 'Refresh', emptyUsb: 'No device', emptyAny: 'No device', emptyWireless: 'No device', selectDevice: 'Choose', standby: 'Standby' },
    common: { connected: 'Connected' }, errors: {},
  }),
}))

import { DeviceSelector } from './DeviceSelector'

describe('DeviceSelector returning device', () => {
  it('signals a same-serial return after a tolerated empty scan without clearing selection', async () => {
    const device = { serial: 'device-1', platform: 'android', model: 'Test phone', os_version: '16', connection_type: 'usb' }
    callMock.mockReset()
    callMock.mockResolvedValueOnce({ devices: [device] })
      .mockResolvedValueOnce({ devices: [] })
      .mockResolvedValue({ devices: [device] })
    const onChange = vi.fn()
    const onReconnect = vi.fn()
    render(<DeviceSelector value="device-1" onChange={onChange} onReconnect={onReconnect} connectionType="usb" />)
    await waitFor(() => expect(screen.getByText('Test phone')).toBeInTheDocument())
    await act(async () => document.dispatchEvent(new Event('visibilitychange')))
    await waitFor(() => expect(callMock).toHaveBeenCalledTimes(2))
    expect(onChange).not.toHaveBeenCalled()
    expect(onReconnect).not.toHaveBeenCalled()
    await act(async () => document.dispatchEvent(new Event('visibilitychange')))
    await waitFor(() => expect(onReconnect).toHaveBeenCalledWith('device-1'))
    expect(onReconnect).toHaveBeenCalledTimes(1)
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByText('Test phone')).toBeInTheDocument()
  })
})
