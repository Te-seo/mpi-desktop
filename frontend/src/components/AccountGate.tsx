import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Activity, ArrowUpRight, ChartNoAxesCombined, Loader2 } from 'lucide-react'
import appIcon from '../../build-assets/icon.png'
import { useMessages } from '../lib/i18n'
import { cloudCommand, cloudErrorMessage, LOCAL_CLOUD_STATUS, onCloudStatus, type CloudStatus } from '../lib/cloud-sync'
import { LanguageSelector } from './LanguageSelector'
import { ThemeToggle } from './ThemeToggle'

/** Auth state comes from Electron main, never from localStorage or form success.
 * Keep the previous owner's provider mounted behind the login screen so a late
 * capture-ended event can finish durable saves after expiry/logout. A different
 * owner gets a fresh provider and cannot see the previous account's live state. */
export function AccountGate({ children }: { children: (ownerId: string) => ReactNode }) {
  const t = useMessages()
  const [status, setStatus] = useState<CloudStatus>(LOCAL_CLOUD_STATUS)
  const [owner, setOwner] = useState<string | null>(null)
  const [checking, setChecking] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const revision = useRef(0)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    const update = (next: CloudStatus) => {
      revision.current += 1
      setStatus(next)
      if (next.signedIn && next.verified && next.userId) setOwner(next.userId)
    }
    const unsubscribe = onCloudStatus(update)
    const version = revision.current
    void cloudCommand('cloud-verify-session').then((next) => {
      if (mounted.current && version === revision.current) update(next)
    }).catch(() => { if (mounted.current) setError('network') })
      .finally(() => { if (mounted.current) setChecking(false) })
    return () => { mounted.current = false; unsubscribe() }
  }, [])

  async function execute(channel: string, input?: unknown) {
    setBusy(true)
    setError(null)
    try {
      const next = await cloudCommand(channel, input)
      if (!mounted.current) return
      setStatus(next)
      if (next.signedIn && next.verified && next.userId) setOwner(next.userId)
    } catch { if (mounted.current) setError('network') }
    finally { if (mounted.current) { setBusy(false); setChecking(false) } }
  }

  const permitted = !checking && status.signedIn && !status.signingIn && status.verified === true && Boolean(status.userId)
  const disabled = busy || checking || status.signingIn || !status.available
  const failureCode = error || status.error
  const failure = failureCode === 'network' ? t.cloud.loginNetworkError : cloudErrorMessage(failureCode, t.cloud)
  return <>
    {owner && <div className={permitted ? 'contents' : 'hidden'} hidden={!permitted} aria-hidden={!permitted}>
      {children(owner)}
    </div>}
    {!permitted && <main className="flex min-h-dvh flex-col bg-bg text-ink">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-line px-5 py-4 sm:px-8">
        <div className="flex items-center gap-3"><img src={appIcon} alt="" className="h-9 w-9 rounded-lg" /><span className="font-display text-lg font-semibold">MPI</span></div>
        <div className="flex items-center gap-2"><LanguageSelector /><ThemeToggle /></div>
      </header>
      <div className="mx-auto grid w-full max-w-5xl flex-1 items-center gap-10 px-5 py-10 sm:px-8 lg:grid-cols-2 lg:gap-20">
        <div className="min-w-0">
          <div className="mb-6 inline-flex rounded-[20px] border border-line bg-surface p-4">
            <img src={appIcon} alt="" className="h-14 w-14" />
          </div>
          <p className="text-sm font-semibold tracking-[0.12em] text-accent">MPI</p>
          <p className="mt-3 max-w-sm break-keep text-balance font-display text-3xl font-semibold leading-snug tracking-tight sm:text-4xl">{t.app.subtitle}</p>
          <div className="mt-8 flex flex-wrap gap-3 text-sm text-muted">
            <span className="inline-flex items-center gap-2 rounded-xl border border-line px-3 py-2.5"><Activity size={18} aria-hidden="true" className="text-accent" />{t.dashboard.nav.live}</span>
            <span className="inline-flex items-center gap-2 rounded-xl border border-line px-3 py-2.5"><ChartNoAxesCombined size={18} aria-hidden="true" className="text-accent" />{t.dashboard.nav.dashboard}</span>
          </div>
        </div>
        <section aria-labelledby="account-login-title" className="mpi-panel w-full p-6 sm:p-8">
          <h1 id="account-login-title" className="font-display text-2xl font-semibold tracking-tight">{t.cloud.signIn}</h1>
          <p className="mb-7 mt-3 text-sm leading-relaxed text-muted">{t.cloud.requiredLoginHelp}</p>
          {checking ? <p role="status" className="flex items-center gap-3 py-5 text-sm text-muted"><Loader2 size={18} aria-hidden="true" className="motion-safe:animate-spin" />{t.cloud.verifyingSession}</p> : <>
            <button type="button" disabled={disabled} onClick={() => { void execute('cloud-sign-in-google') }}
              className="mpi-press flex min-h-12 w-full items-center justify-center gap-3 rounded-xl bg-accent px-4 py-3 font-semibold text-bg disabled:opacity-50">{t.cloud.googleSignIn}<ArrowUpRight size={18} aria-hidden="true" /></button>
            {status.signingIn && <div className="mt-3 rounded-lg border border-line p-3 text-sm">
              <p role="status">{t.cloud.googleSigningIn}</p>
              <button type="button" disabled={busy} onClick={() => void execute('cloud-cancel-sign-in')} className="mt-2 min-h-10 rounded-lg px-2 text-accent">{t.cloud.cancelSignIn}</button>
            </div>}
            <p className="mt-4 text-sm leading-relaxed text-muted">{t.cloud.googleAccountHelp}</p>
            <button type="button" disabled={busy || status.signingIn} onClick={() => void execute('cloud-verify-session')}
              className="mpi-control mt-5 w-full border border-line bg-surface-hi px-4 text-sm font-medium text-ink disabled:opacity-50">{t.cloud.retryConnection}</button>
            {!status.available && <p className="mt-3 text-xs text-muted">{t.cloud.secureUnavailable}</p>}
          </>}
          {failure && <p role="alert" className="mt-4 rounded-xl border border-rec/30 bg-rec-soft p-3 text-sm leading-relaxed text-danger">{failure}</p>}
        </section>
      </div>
    </main>}
  </>
}
