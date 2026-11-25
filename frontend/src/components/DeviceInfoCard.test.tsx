import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderWithLocale } from '../test/utils'
import { DeviceInfoCard } from './DeviceInfoCard'

const device = { serial: 'test-device', platform: 'android' as const, model: 'Galaxy', os_version: '14', connection_type: 'usb' as const }

describe('DeviceInfoCard firmware identity', () => {
  it('shows the iOS marketing name, actual model number and product identifier separately', () => {
    renderWithLocale(<DeviceInfoCard device={{ ...device, platform: 'ios', model: 'iPhone 18 Pro Max', display_name: 'Lab phone', model_identifier: 'iPhone19,3', model_number: 'A3473' }}
      platform="ios" connectionMode="usb" appLabel="Game" bundleId="com.example.game" intervalSec={1} />)
    expect(screen.getByText('Lab phone')).toBeInTheDocument()
    expect(screen.getByText('iPhone 18 Pro Max')).toBeInTheDocument()
    expect(screen.getByText('A3473')).toBeInTheDocument()
    expect(screen.getByText('iPhone19,3')).toBeInTheDocument()
  })
  it('does not invent an A-number when only a known iOS name is available', () => {
    renderWithLocale(<DeviceInfoCard device={{ ...device, platform: 'ios', model: 'iPhone 18 Pro Max' }}
      platform="ios" connectionMode="usb" appLabel="Game" bundleId="com.example.game" intervalSec={1} />)
    expect(screen.getByText('Model Number').nextElementSibling).toHaveTextContent('—')
    expect(screen.queryByText('A3473')).not.toBeInTheDocument()
  })
  it('shows firmware version and its full build name', () => {
    renderWithLocale(<DeviceInfoCard device={{ ...device, build_version: 'AXJ1', build_name: 'UP1A.231005.007.AXJ1' }}
      platform="android" connectionMode="usb" appLabel="Game" bundleId="com.example.game" intervalSec={1} />)
    expect(screen.getByText('Build Version')).toBeInTheDocument()
    expect(screen.getByText('AXJ1')).toBeInTheDocument()
    expect(screen.getByTitle('UP1A.231005.007.AXJ1')).toBeInTheDocument()
  })
  it('keeps unavailable build values empty instead of reusing the OS version', () => {
    renderWithLocale(<DeviceInfoCard device={device} platform="android" connectionMode="usb" appLabel="Game" bundleId="com.example.game" intervalSec={1} />)
    expect(screen.getAllByText('—')).toHaveLength(2)
    expect(screen.getAllByText('14')).toHaveLength(1)
  })
})
