import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { expect, it, vi } from 'vitest'
import { OPEN_PLAY_COMPANION, openOfficialCompanionPage } from './play-companion'

it('opens only the exact public MPI Play listing for the owned top-level renderer', async () => {
  const open = vi.fn(async (_url: string) => {})
  expect(OPEN_PLAY_COMPANION).toBe('open-play-companion')
  await expect(openOfficialCompanionPage(false, true, open)).rejects.toThrow('Command unavailable')
  await expect(openOfficialCompanionPage(true, false, open)).rejects.toThrow('Command unavailable')
  expect(open).not.toHaveBeenCalled()
  await openOfficialCompanionPage(true, true, open)
  expect(open).toHaveBeenCalledExactlyOnceWith('https://play.google.com/store/apps/details?id=com.mpi.companion')
})

it('pins the locally encoded QR independently decoded as the exact Play URL', () => {
  const png = readFileSync('src/assets/mpi-google-play-qr.png')
  expect(createHash('sha256').update(png).digest('hex'))
    .toBe('848cae7530d39b3a9340cc7f03f623cbb5c8371a24823a11213cf709515a01f5')
})
