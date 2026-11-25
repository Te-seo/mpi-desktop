import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LocaleProvider } from '../lib/i18n'
import { LOCAL_CLOUD_STATUS, type CloudStatus } from '../lib/cloud-sync'
import { AccountGate } from './AccountGate'

vi.mock('./ThemeToggle', () => ({ ThemeToggle: () => null }))
const invoke = vi.fn()
let listener: ((status: CloudStatus) => void) | undefined
let status: CloudStatus
const owner = '11111111-1111-4111-8111-111111111111'
beforeEach(() => {
  localStorage.clear(); localStorage.setItem('mpi.locale.v1', 'en')
  status = { ...LOCAL_CLOUD_STATUS, available: true, verified: false }
  invoke.mockReset(); invoke.mockImplementation(async () => status)
  Object.defineProperty(window, 'api', { configurable: true, value: { invoke,
    receive: (_channel: string, callback: typeof listener) => { listener = callback; return () => { listener = undefined } },
  } })
})
function view() { return render(<LocaleProvider><AccountGate>{id => <div key={id} data-testid="workspace">Workspace {id}<input aria-label="Private filter" /></div>}</AccountGate></LocaleProvider>) }

describe('required desktop sign-in', () => {
  it('never mounts measurement/dashboard before online verification, including cached signedIn state', async () => {
    status = { ...status, signedIn: true, userId: owner, verified: false }
    view()
    expect(screen.queryByTestId('workspace')).not.toBeInTheDocument()
    await screen.findByRole('button', { name: 'Continue with Google' })
    expect(screen.queryByTestId('workspace')).not.toBeInTheDocument()
    expect(invoke).toHaveBeenCalledWith('cloud-verify-session', undefined)
    act(() => listener?.({ ...status, verified: true }))
    expect(screen.getByTestId('workspace')).toBeVisible()
  })
  it('offers only Google and only enters from verified main-process success', async () => {
    const user = userEvent.setup(); view()
    await screen.findByRole('button', { name: 'Continue with Google' })
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    invoke.mockImplementationOnce(async () => ({ ...status, error: 'oauth-failed' }))
    await user.click(screen.getByRole('button', { name: 'Continue with Google' }))
    expect(invoke).toHaveBeenLastCalledWith('cloud-sign-in-google', undefined)
    expect(screen.getByRole('alert')).toHaveTextContent('Google sign-in failed')
    expect(screen.queryByTestId('workspace')).not.toBeInTheDocument()
  })
  it('hides expired account state while allowing final persistence, then remounts for another owner', async () => {
    status = { ...status, signedIn: true, verified: true, userId: owner }
    view(); const workspace = await screen.findByTestId('workspace')
    await waitFor(() => expect(workspace).toBeVisible())
    await userEvent.type(screen.getByLabelText('Private filter'), 'old account state')
    act(() => listener?.({ ...status, verified: false }))
    expect(workspace).not.toBeVisible()
    expect(screen.getByLabelText('Private filter')).toHaveValue('old account state')
    act(() => listener?.({ ...status, userId: '22222222-2222-4222-8222-222222222222', verified: true }))
    expect(screen.getByLabelText('Private filter')).toHaveValue('')
    expect(screen.getByTestId('workspace')).toHaveTextContent('22222222')
  })
  it('fails closed when verification is unavailable and exposes retry', async () => {
    invoke.mockRejectedValueOnce(new Error('offline'))
    view()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not verify your sign-in')
    expect(screen.queryByTestId('workspace')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check connection again' })).toBeEnabled()
  })
})
