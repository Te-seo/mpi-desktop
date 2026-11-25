import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LocaleProvider } from '../lib/i18n'
import { LOCAL_CLOUD_STATUS, type CloudStatus } from '../lib/cloud-sync'
import { CloudPanel } from './CloudPanel'
import { SettingsButton } from './SettingsButton'

let status: CloudStatus
const invoke = vi.fn()
let listener: ((value: CloudStatus) => void) | null = null
const ownerId = '11111111-1111-4111-8111-111111111111'

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mpi.locale.v1', 'en')
  status = { ...LOCAL_CLOUD_STATUS, available: true }
  listener = null
  invoke.mockReset()
  invoke.mockImplementation(async (channel: string, input: unknown) => {
    if (channel === 'statistics-participation-get') return {
      user_id: ownerId, pending: null, error: null,
      state: { available: true, preference: 'exclude', revision: 1, current_notice: { version: 'test-v1', content: 'Test notice' } },
    }
    if (channel === 'cloud-set-enabled') status = { ...status, enabled: input === true }
    if (channel === 'cloud-sign-out') status = { ...LOCAL_CLOUD_STATUS, available: true, otherAccountPending: 2 }
    if (channel === 'cloud-sign-in-google') status = { ...status, signingIn: true, error: null }
    if (channel === 'cloud-cancel-sign-in') status = { ...status, signingIn: false, error: 'oauth-cancelled' }
    return status
  })
  Object.defineProperty(window, 'api', { configurable: true, value: {
    invoke,
    receive(channel: string, callback: (value: CloudStatus) => void) {
      if (channel === 'cloud-status-changed') listener = callback
      return () => { listener = null }
    },
  } })
})

function renderPanel() {
  return render(<LocaleProvider><SettingsButton ownerId={ownerId} /><CloudPanel /></LocaleProvider>)
}

describe('App settings cloud account controls', () => {
  it('keeps the statistics switch inside settings and preserves the saved preference across reopens', async () => {
    const user = userEvent.setup()
    renderPanel()
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    expect(invoke.mock.calls.some(([channel]) => channel.startsWith('statistics-participation-'))).toBe(false)
    const settings = screen.getByRole('button', { name: 'Settings' })
    await user.click(settings)
    const toggle = within(screen.getByRole('dialog', { name: 'Settings' })).getByRole('switch', { name: 'Performance statistics' })
    await waitFor(() => expect(toggle).toBeEnabled())
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(invoke).toHaveBeenCalledWith('statistics-participation-get', { user_id: ownerId })
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    expect(settings).toHaveFocus()
    await user.click(settings)
    const reopened = within(screen.getByRole('dialog')).getByRole('switch', { name: 'Performance statistics' })
    await waitFor(() => expect(reopened).toBeEnabled())
    expect(reopened).toHaveAttribute('aria-checked', 'false')
    expect(invoke.mock.calls.filter(([channel]) => channel === 'statistics-participation-get')).toHaveLength(2)
    expect(invoke.mock.calls.some(([channel]) => channel === 'statistics-participation-set')).toBe(false)
  })

  it('keeps login inside app settings while endpoint controls remain on the cloud card', async () => {
    const user = userEvent.setup()
    renderPanel()
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Data transfer endpoint'), 'https://example.com/ingest')
    await user.click(screen.getByRole('checkbox', { name: 'Send to custom endpoint on save' }))
    expect(localStorage.getItem('mpi.endpoint.url.v1')).toBe('https://example.com/ingest')
    expect(localStorage.getItem('mpi.endpoint.sendOnSave.v1')).toBe('1')
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'MPI Cloud' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })

  it('signs in, shows pending sessions, retries, pauses sync and signs out through IPC', async () => {
    const user = userEvent.setup()
    renderPanel()
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.click(screen.getByRole('button', { name: 'Continue with Google' }))
    expect(invoke).toHaveBeenCalledWith('cloud-sign-in-google', undefined)
    act(() => listener?.(status = { ...status, signingIn: false, signedIn: true, enabled: true, userId: '11111111-1111-4111-8111-111111111111', email: 'owner@example.com', pending: 2 }))
    expect(await screen.findByText('owner@example.com')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('2 sessions pending for this account')
    await user.click(screen.getByRole('button', { name: 'Retry pending sessions' }))
    expect(invoke).toHaveBeenCalledWith('cloud-retry', undefined)
    await user.click(screen.getByRole('checkbox', { name: 'Automatically sync sessions' }))
    expect(invoke).toHaveBeenCalledWith('cloud-set-enabled', false)
    expect(screen.getByRole('button', { name: 'Retry pending sessions' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('button', { name: 'Continue with Google' })).toBeEnabled()
    expect(screen.getByText('2 sessions await their original account sign-in')).toBeInTheDocument()
    expect(JSON.stringify([...Array(localStorage.length)].map((_, i) => localStorage.getItem(localStorage.key(i)!)))).not.toContain('test-password')
  })

  it('shows live pending/network status and disables sign-in without secure storage', async () => {
    const user = userEvent.setup()
    status = { ...status, signedIn: true, enabled: true, email: 'owner@example.com', pending: 1 }
    renderPanel()
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await screen.findByText('owner@example.com')
    act(() => listener?.({ ...status, error: 'network', otherAccountPending: 3 }))
    expect(screen.getByRole('alert')).toHaveTextContent('Pending sessions are kept and retried automatically.')
    expect(screen.getByText('3 sessions await their original account sign-in')).toBeInTheDocument()
    act(() => listener?.({ ...LOCAL_CLOUD_STATUS, available: false }))
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeDisabled()
    expect(screen.getByText('Secure credential storage is unavailable on this device, so cloud sign-in is disabled.')).toBeInTheDocument()
  })

  it('traps keyboard focus, closes on Escape/backdrop and never collects passwords', async () => {
    const user = userEvent.setup()
    renderPanel()
    const settings = screen.getByRole('button', { name: 'Settings' })
    await user.click(settings)
    const close = screen.getByRole('button', { name: 'Close' })
    expect(close).toHaveFocus()
    await user.tab({ shift: true })
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toHaveFocus()
    await user.tab()
    expect(close).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(settings).toHaveFocus()
    await user.click(settings)
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    await user.click(screen.getByRole('dialog').parentElement!)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(settings).toHaveFocus()
  })

  it('starts Google login without renderer credentials, prevents duplicates and supports cancellation', async () => {
    const user = userEvent.setup()
    renderPanel()
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.click(screen.getByRole('button', { name: 'Continue with Google' }))
    expect(invoke).toHaveBeenCalledWith('cloud-sign-in-google', undefined)
    expect(screen.getByRole('status')).toHaveTextContent('Complete Google sign-in in your browser.')
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Cancel sign-in' }))
    expect(invoke).toHaveBeenCalledWith('cloud-cancel-sign-in', undefined)
    expect(screen.getByRole('alert')).toHaveTextContent('Google sign-in was cancelled.')
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeEnabled()
  })

  it('receives Google success after settings close and keeps one status subscription across reopens', async () => {
    const user = userEvent.setup()
    const { unmount } = renderPanel()
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.click(screen.getByRole('button', { name: 'Continue with Google' }))
    const activeListener = listener
    await user.keyboard('{Escape}')
    act(() => listener?.({ ...status, signingIn: false, signedIn: true, enabled: true, email: 'google@example.com', pending: 1 }))
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    expect(await screen.findByText('google@example.com')).toBeInTheDocument()
    expect(listener).toBe(activeListener)
    expect(screen.queryByRole('button', { name: 'Continue with Google' })).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('1 sessions pending for this account')
    unmount()
    expect(listener).toBeNull()
  })

  it.each([
    ['oauth-unavailable', 'Google sign-in is not enabled.'],
    ['oauth-timeout', 'Google sign-in timed out.'],
    ['oauth-failed', 'Google sign-in failed.'],
    ['browser-open-failed', 'Could not open the sign-in browser.'],
    ['oauth-callback-unavailable', 'Could not start the Google sign-in connection.'],
  ])('shows actionable %s errors and allows retry', async (error, message) => {
    const user = userEvent.setup()
    renderPanel()
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.click(screen.getByRole('button', { name: 'Continue with Google' }))
    act(() => listener?.({ ...status, signingIn: false, error }))
    expect(screen.getByRole('alert')).toHaveTextContent(message)
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeEnabled()
  })
})
