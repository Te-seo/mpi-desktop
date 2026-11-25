import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMessages } from '../lib/i18n'

interface Props {
  /** Prefilled suggestion (app label + capture time). */
  defaultName: string
  /** True while the transfer is in flight (disables inputs, shows a spinner label). */
  saving: boolean
  onConfirm(name: string): void
  onSkip(): void
}

/** Post-capture modal that names a session before its endpoint transfer.
 * Rendered only while a save is pending, so it fresh-mounts per session and the
 * name state seeds from `defaultName` without a sync effect. */
export function SessionNameModal({ defaultName, saving, onConfirm, onSkip }: Props) {
  const t = useMessages()
  const [name, setName] = useState(defaultName)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const titleId = useId()
  const descriptionId = useId()
  const inputId = useId()

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const dialog = dialogRef.current
    dialog?.showModal()
    inputRef.current?.focus()
    inputRef.current?.select()
    return () => {
      dialog?.close()
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  return createPortal(
    <dialog ref={dialogRef} aria-labelledby={titleId} aria-describedby={descriptionId}
      onCancel={(event) => { event.preventDefault(); if (!saving) onSkip() }}
      className="mpi-panel m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-md overflow-y-auto overscroll-contain p-5 text-ink shadow-xl backdrop:bg-black/60">
      <form onSubmit={(event) => { event.preventDefault(); if (!saving) onConfirm(name) }}>
        <h2 id={titleId} className="text-base font-semibold text-ink">{t.sessionName.title}</h2>
        <p id={descriptionId} className="mt-1 text-sm leading-relaxed text-muted">{t.sessionName.description}</p>
        <label htmlFor={inputId} className="mt-4 block text-xs font-medium text-muted">{t.sessionName.placeholder}</label>
        <input
          ref={inputRef}
          id={inputId}
          name="session-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={saving}
          maxLength={120}
          placeholder={t.sessionName.placeholder}
          className="mpi-control mt-2 w-full border border-line bg-bg px-3 py-2 text-sm text-ink disabled:opacity-60"
        />
        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <button
            type="button"
            onClick={onSkip}
            disabled={saving}
            className="mpi-control border border-line bg-surface px-3.5 py-2 text-sm text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-60"
          >
            {t.sessionName.skip}
          </button>
          <button
            type="submit"
            disabled={saving}
            className="mpi-control bg-accent px-3.5 py-2 text-sm font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-60"
          >
            {saving ? t.sessionName.saving : t.sessionName.save}
          </button>
        </div>
      </form>
    </dialog>,
    document.body
  )
}
