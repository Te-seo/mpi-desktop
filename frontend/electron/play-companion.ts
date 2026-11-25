/** Exact official Play listing; intentionally no locale, campaign or account parameters. */
export const PLAY_COMPANION_URL = 'https://play.google.com/store/apps/details?id=com.mpi.companion'

export const OPEN_PLAY_COMPANION = 'open-play-companion' as const

/** Own the destination in main. Any unrecognized window or frame is refused. */
export async function openOfficialCompanionPage(
  isTrustedWindow: boolean,
  isMainFrame: boolean,
  open: (url: string) => Promise<void>,
): Promise<void> {
  if (!isTrustedWindow || !isMainFrame) throw new Error('Command unavailable')
  await open(PLAY_COMPANION_URL)
}
