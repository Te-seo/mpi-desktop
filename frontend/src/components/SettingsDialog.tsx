import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { useMessages } from '../lib/i18n'
import type { CloudStatus } from '../lib/cloud-sync'
import { StatisticsParticipation } from './StatisticsParticipation'

interface Props {
  ownerId: string
  status: CloudStatus
  busy: boolean
  errorMessage: string | null
  onExecute(channel: string, input?: unknown): Promise<void>
  onClose(): void
}

/** Google sign-in stays in the system browser; settings never collect credentials. */
export function SettingsDialog({ ownerId, status, busy, errorMessage, onExecute, onClose }: Props) {
  const t = useMessages()
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const signingIn = status.signingIn === true
  const disabled = busy || signingIn || !status.available

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()

    const keydown = (event: KeyboardEvent) => {
      const dialog = dialogRef.current
      if (!dialog) return
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onClose()
      } else if (event.key === 'Tab') {
        const controls = [...dialog.querySelectorAll<HTMLElement>(
          ':is(button, input, [tabindex="0"]):not(:disabled)',
        )]
        const first = controls[0]
        const last = controls[controls.length - 1]
        if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
          event.preventDefault()
          last?.focus()
        } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
          event.preventDefault()
          first?.focus()
        }
      }
    }
    const focusin = (event: FocusEvent) => {
      if (event.target instanceof Node && !dialogRef.current?.contains(event.target)) closeRef.current?.focus()
    }
    document.addEventListener('keydown', keydown, true)
    document.addEventListener('focusin', focusin)
    return () => {
      document.removeEventListener('keydown', keydown, true)
      document.removeEventListener('focusin', focusin)
      document.body.style.overflow = previousOverflow
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [onClose])

  useEffect(() => {
    // A completed sign-in replaces the form, including its focused control.
    if (!dialogRef.current?.contains(document.activeElement)) closeRef.current?.focus()
  }, [status.signedIn])

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4"
      onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="settings-title"
        aria-describedby="cloud-settings-help"
        className="mpi-panel max-h-[calc(100dvh-2rem)] w-full max-w-md overflow-y-auto overscroll-contain p-5 shadow-xl sm:p-6">
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 id="settings-title" className="text-xl font-semibold tracking-tight text-ink">{t.common.settings}</h2>
          <button ref={closeRef} type="button" aria-label={t.common.close} onClick={onClose}
            className="mpi-icon-button text-muted hover:bg-hover hover:text-ink">
            <X size={18} aria-hidden="true" />
          </button>
        </div>
        <StatisticsParticipation ownerId={ownerId} />
        <section aria-labelledby="settings-cloud-title" className="mt-5 flex flex-col gap-4 border-t border-line pt-5">
          <h3 id="settings-cloud-title" className="text-sm font-semibold text-ink">{t.cloud.cloudTitle}</h3>
          <p className="text-xs font-medium text-muted">{t.cloud.storageTitle}</p>
          <p id="cloud-settings-help" className="text-sm leading-relaxed text-muted">{t.cloud.loginHelp}</p>
          {status.signedIn ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-bg p-3 text-sm">
                <span className="break-all text-ink">{status.email || status.userId}</span>
                <button type="button" disabled={busy} onClick={() => void onExecute('cloud-sign-out')}
                  className="mpi-control shrink-0 border border-line px-3 text-muted disabled:opacity-50">{t.cloud.signOut}</button>
              </div>
              <label className="flex min-h-11 items-center justify-between gap-3 text-sm text-ink">
                {t.cloud.syncEnabled}
                <input type="checkbox" checked={status.enabled} disabled={busy}
                  onChange={(event) => void onExecute('cloud-set-enabled', event.target.checked)}
                  className="h-4 w-4 accent-accent" />
              </label>
              <p role="status" className="text-[12px] text-muted">{status.syncing ? t.cloud.syncing : t.cloud.pending(status.pending)}</p>
              {status.lastSyncedAt && <p className="text-[11px] text-faint">{t.cloud.lastSynced}: {new Date(status.lastSyncedAt).toLocaleString()}</p>}
              <button type="button" disabled={busy || status.syncing || !status.enabled}
                onClick={() => void onExecute('cloud-retry')}
                className="mpi-control border border-line bg-surface-hi px-3 text-sm text-ink disabled:opacity-50">{t.cloud.retry}</button>
            </>
          ) : (
            <>
              <button type="button" disabled={disabled} onClick={() => {
                void onExecute('cloud-sign-in-google')
              }} className="flex items-center justify-center gap-2 rounded-lg border border-line bg-bg px-3 py-2.5 text-sm font-semibold text-ink hover:border-accent disabled:opacity-50">
                <span aria-hidden="true" className="text-base font-bold text-[#4285f4]">G</span>
                {t.cloud.googleSignIn}
              </button>
              {signingIn && <div className="flex flex-col gap-2 rounded-lg border border-line bg-bg p-3">
                <p role="status" className="text-[12px] text-muted">{t.cloud.googleSigningIn}</p>
                <button type="button" disabled={busy} onClick={() => void onExecute('cloud-cancel-sign-in')}
                  className="self-start rounded border border-line px-2.5 py-1.5 text-[12px] text-ink disabled:opacity-50">{t.cloud.cancelSignIn}</button>
              </div>}
              <p className="text-sm leading-relaxed text-muted">{t.cloud.googleAccountHelp}</p>
              <p className="text-[11px] text-faint">{status.available ? t.cloud.localOnly : t.cloud.secureUnavailable}</p>
            </>
          )}
          {status.otherAccountPending > 0 && <p className="text-[11px] text-faint">{t.cloud.otherPending(status.otherAccountPending)}</p>}
          {errorMessage && <p role="alert" className="rounded-xl border border-rec/30 bg-rec-soft p-3 text-sm leading-relaxed text-danger">{errorMessage}</p>}
        </section>
      </div>
    </div>,
    document.body,
  )
}
