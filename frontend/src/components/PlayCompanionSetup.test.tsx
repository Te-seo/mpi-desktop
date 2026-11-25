import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { renderWithLocale } from '../test/utils'
import { PlayCompanionSetup } from './PlayCompanionSetup'

const official = 'https://play.google.com/store/apps/details?id=com.mpi.companion'
afterEach(() => vi.unstubAllGlobals())

it('provides the local QR and fixed official listing before a device is selected', async () => {
  const invoke = vi.fn(async () => {})
  vi.stubGlobal('api', { invoke })
  const check = vi.fn(async () => {})
  renderWithLocale(<PlayCompanionSetup selected={false} status={null} check={check} locked={false} />)
  expect(screen.getByRole('img', { name: 'QR code for MPI on Google Play' })).toBeVisible()
  const officialLink = screen.getByRole('link', { name: 'Open MPI on Google Play' })
  expect(officialLink).toHaveAttribute('href', official)
  fireEvent.click(officialLink)
  await waitFor(() => expect(invoke).toHaveBeenCalledExactlyOnceWith('open-play-companion'))
  expect(check).not.toHaveBeenCalled()
})

it('only asks to start the existing bridge after the installed state and a Connect click', async () => {
  const check = vi.fn(async () => {})
  renderWithLocale(<PlayCompanionSetup selected={true} status={{ available: false, reason: 'companion_unreachable' }}
    check={check} locked={false} />)
  expect(screen.getByText('MPI is installed on this device.')).toBeVisible()
  expect(screen.queryByRole('img')).toBeNull()
  expect(check).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Connect MPI' }))
  await waitFor(() => expect(check).toHaveBeenCalledExactlyOnceWith(true))
})

it('preserves Android authorization and checks status without starting the app when USB is unauthorized', async () => {
  const check = vi.fn(async () => {})
  renderWithLocale(<PlayCompanionSetup selected={true} status={{ available: false, reason: 'usb_unauthorized' }}
    check={check} locked={false} />)
  expect(screen.getByRole('status')).toHaveTextContent('Unlock your phone and authorize USB debugging.')
  expect(screen.queryByRole('button', { name: 'Connect MPI' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Check connection' }))
  await waitFor(() => expect(check).toHaveBeenCalledExactlyOnceWith(false))
})
