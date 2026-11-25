import { useState } from 'react'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { renderWithLocale } from '../../test/utils'
import { makeListItem } from '../../test/fixtures'
import type { SessionCut } from '../../lib/types'
import { SessionCutsEditor } from './SessionCutsEditor'

const call = vi.hoisted(() => vi.fn())
vi.mock('../../lib/ipc', () => ({ call }))
const analyze = vi.fn()
function Harness() {
  const [cuts, setCuts] = useState<SessionCut[]>([])
  return <SessionCutsEditor session={makeListItem({ cuts, duration_sec: 20 })} onSaved={setCuts} onAnalyze={analyze} />
}
beforeEach(() => {
  call.mockReset()
  analyze.mockReset()
  call.mockImplementation(async (_command, payload) => ({ cuts: payload.cuts }))
})

it('adds, edits, analyzes, and deletes persistent cuts', async () => {
  renderWithLocale(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Add cut' }))
  fireEvent.change(screen.getByLabelText('Cut name'), { target: { value: 'Boss fight' } })
  fireEvent.change(screen.getByLabelText('Cut start'), { target: { value: '0:02' } })
  fireEvent.change(screen.getByLabelText('Cut end'), { target: { value: '0:12' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save cut' }))
  await screen.findByText('Boss fight')
  expect(call).toHaveBeenCalledWith('save_session_cuts', expect.objectContaining({ expected_cuts: [], cuts: [expect.objectContaining({ name: 'Boss fight', start_sec: 2, end_sec: 12 })] }))
  fireEvent.click(screen.getByRole('button', { name: 'Edit cut Boss fight' }))
  fireEvent.change(screen.getByLabelText('Cut end'), { target: { value: '0:15' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save cut' }))
  await waitFor(() => expect(screen.queryByLabelText('Cut name')).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: 'Analyze cut' }))
  expect(analyze).toHaveBeenCalledWith(expect.objectContaining({ start_sec: 2, end_sec: 15 }))
  fireEvent.click(screen.getByRole('button', { name: 'Delete cut Boss fight' }))
  await waitFor(() => expect(screen.queryByText('Boss fight')).not.toBeInTheDocument())
})

it('rejects invalid times and retains the draft after a failed save', async () => {
  renderWithLocale(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Add cut' }))
  fireEvent.change(screen.getByLabelText('Cut end'), { target: { value: '0:21' } })
  expect(screen.getByRole('button', { name: 'Save cut' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Cut end'), { target: { value: '0:10' } })
  call.mockRejectedValueOnce(new Error('disk unavailable'))
  fireEvent.click(screen.getByRole('button', { name: 'Save cut' }))
  await screen.findByRole('alert')
  expect(screen.getByLabelText('Cut end')).toHaveValue('0:10')
  fireEvent.click(screen.getByRole('button', { name: 'Save cut' }))
  await screen.findByText('Cut 1')
})
