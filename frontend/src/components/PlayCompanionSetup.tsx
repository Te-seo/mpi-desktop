import { useState } from 'react'
import { OPEN_PLAY_COMPANION, PLAY_COMPANION_URL } from '../../electron/play-companion'
import playQr from '../assets/mpi-google-play-qr.png'
import type { DeviceOverlayStatus } from '../lib/deviceOverlay'
import { useMessages } from '../lib/i18n'

export function PlayCompanionSetup({ selected, status, locked, check }: {
  selected: boolean
  status: DeviceOverlayStatus | null
  locked: boolean
  check: (connect: boolean) => Promise<void>
}) {
  const { liveTracing: m } = useMessages()
  const [openingFailed, setOpeningFailed] = useState(false)
  const [checking, setChecking] = useState(false)
  const reason = status?.reason
  const needsStore = !selected || reason === 'companion_not_installed' || reason === 'companion_too_old'
  const canStart = selected && reason === 'companion_unreachable'
  const message = status?.available ? m.playCompanionAvailable
    : reason === 'usb_unauthorized' ? m.deviceOverlayUsbUnauthorized
    : reason === 'device_unreachable' ? m.deviceOverlayDeviceUnreachable
    : reason === 'companion_sign_in_required' ? m.deviceOverlaySignInRequired
    : reason === 'companion_not_installed' ? m.deviceOverlayUnavailableCompanionNotInstalled
    : reason === 'companion_too_old' ? m.deviceOverlayUnavailableCompanionTooOld
    : canStart || reason === 'overlay_permission_denied' || reason === 'overlay_unsupported_os' ? m.playCompanionInstalled
    : null

  return (
    <section className="mpi-panel">
      <h2 className="text-[13px] font-semibold text-ink">{m.playCompanionTitle}</h2>
      <p className="mt-1 text-[11px] leading-relaxed text-muted">{m.playCompanionUsbHint}</p>
      {message && <p role={reason === 'overlay_permission_denied' || reason === 'overlay_unsupported_os' ? undefined : 'status'} className="mt-2 text-[11px] leading-relaxed text-ink">{message}</p>}
      {needsStore && (
        <div className="mt-2">
          <p className="text-[11px] text-ink">{m.playCompanionInstall}</p>
          <img src={playQr} alt={m.playCompanionQrAlt} width={180} height={180}
            className="mt-2 mx-auto block max-w-full h-auto rounded border border-line bg-white"
            style={{ imageRendering: 'pixelated' }} />
          <a href={PLAY_COMPANION_URL} target="_blank" rel="noopener noreferrer"
            className="mt-2 inline-block text-[12px] font-semibold text-accent underline"
            onClick={(event) => {
              if (!window.api) return
              event.preventDefault()
              setOpeningFailed(false)
              void window.api.invoke(OPEN_PLAY_COMPANION).catch(() => setOpeningFailed(true))
            }}>
            {m.playCompanionOpen}
          </a>
          {openingFailed && (
            <div role="status" className="mt-1 break-all text-[11px] text-muted">
              {m.playCompanionOpenFailed} {PLAY_COMPANION_URL}
            </div>
          )}
        </div>
      )}
      {selected && !status?.available && (
        <button type="button" className="mpi-btn mt-2 text-[12px]" disabled={locked || checking}
          onClick={() => {
            setChecking(true)
            void check(canStart).finally(() => setChecking(false))
          }}>
          {checking ? m.playCompanionChecking : canStart ? m.playCompanionConnect : m.playCompanionCheck}
        </button>
      )}
    </section>
  )
}
