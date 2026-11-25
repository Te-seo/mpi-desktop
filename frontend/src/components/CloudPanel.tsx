import { useState } from 'react'
import {
  getEndpointUrl,
  getSendOnSave,
  setEndpointUrl,
  setSendOnSave,
} from '../lib/endpointSettings'
import { useMessages } from '../lib/i18n'

/** Local endpoint transfer settings. Enabled saves are POSTed directly to this URL. */
export function CloudPanel() {
  const t = useMessages()
  const [sendOnSave, setSendOnSaveState] = useState(getSendOnSave)
  const [endpoint, setEndpoint] = useState(getEndpointUrl)

  return (
    <div className="rounded-[13px] border border-line bg-surface p-4 shadow-[0_1px_2px_rgba(0,0,0,0.2)]">
      <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">
        {t.cloud.cloudTitle}
      </div>

      <label className="flex items-center justify-between gap-3">
        <span className="text-[13px] text-muted">{t.cloud.sendToCloudOnSave}</span>
        <input
          type="checkbox"
          checked={sendOnSave}
          onChange={(event) => {
            const enabled = event.target.checked
            setSendOnSaveState(enabled)
            setSendOnSave(enabled)
          }}
          className="h-4 w-4 accent-accent"
        />
      </label>

      <div className="mt-3 flex flex-col gap-1.5 border-t border-line pt-3">
        <label
          htmlFor="endpoint-url"
          className="text-[11px] font-semibold uppercase tracking-[0.12em] text-faint"
        >
          {t.cloud.endpointLabel}
        </label>
        <input
          id="endpoint-url"
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder={t.cloud.endpointPlaceholder}
          value={endpoint}
          onChange={(event) => {
            const value = event.target.value
            setEndpoint(value)
            setEndpointUrl(value)
          }}
          className="w-full rounded-[9px] border border-line bg-bg px-2.5 py-1.5 font-mono text-[12px] text-ink placeholder:text-faint focus:border-accent focus:outline-none"
        />
        <p className="text-[11px] leading-snug text-faint">{t.cloud.endpointHelp}</p>
      </div>
    </div>
  )
}
