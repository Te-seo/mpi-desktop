import { useCallback, useEffect, useState } from 'react'
import { Settings } from 'lucide-react'
import { useMessages } from '../lib/i18n'
import { cloudCommand, cloudErrorMessage, LOCAL_CLOUD_STATUS, onCloudStatus } from '../lib/cloud-sync'
import { SettingsDialog } from './SettingsDialog'

/** App-wide settings and one persistent subscription to cloud account status. */
export function SettingsButton({ ownerId }: { ownerId: string }) {
  const t = useMessages()
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState(LOCAL_CLOUD_STATUS)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const close = useCallback(() => setOpen(false), [])

  useEffect(() => {
    const unsubscribe = onCloudStatus(setStatus)
    void cloudCommand('cloud-status').then(setStatus).catch(() => setActionError('local-storage'))
    const online = () => { void cloudCommand('cloud-retry').then(setStatus).catch(() => {}) }
    window.addEventListener('online', online)
    return () => {
      unsubscribe()
      window.removeEventListener('online', online)
    }
  }, [])

  async function execute(channel: string, input?: unknown) {
    setBusy(true)
    setActionError(null)
    try {
      setStatus(await cloudCommand(channel, input))
    } catch {
      setActionError('local-storage')
    } finally {
      setBusy(false)
    }
  }

  const errorMessage = cloudErrorMessage(actionError || status.error, t.cloud)
  return <>
    <button type="button" aria-label={t.common.settings} title={t.common.settings} aria-haspopup="dialog"
      aria-expanded={open} onClick={() => setOpen(true)}
      className="mpi-icon-button relative text-muted transition-colors hover:border-accent-line hover:text-ink focus-visible:outline-2 focus-visible:outline-accent">
      <Settings size={17} aria-hidden="true" />
      {(status.signingIn || errorMessage) && <span aria-hidden="true"
        className={`absolute right-1 top-1 h-1.5 w-1.5 rounded-full ${errorMessage ? 'bg-red-400' : 'bg-accent'}`} />}
    </button>
    {!open && status.signingIn && <span role="status" className="sr-only">{t.cloud.googleSigningIn}</span>}
    {open && <SettingsDialog ownerId={ownerId} status={status} busy={busy} errorMessage={errorMessage} onExecute={execute} onClose={close} />}
  </>
}
