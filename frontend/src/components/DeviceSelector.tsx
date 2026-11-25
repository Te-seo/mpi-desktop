import { ChevronDown, Loader2, RefreshCcw } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { call } from '../lib/ipc'
import { friendlyError } from '../lib/friendlyError'
import { useMessages } from '../lib/i18n'
import type { DeviceInfo, Platform } from '../lib/types'
import { useAnchoredPanel } from './useAnchoredPanel'

type ConnectionFilter = 'usb' | 'wireless' | 'all'

interface Props {
  /** 'all' (default) enumerates Android + iOS together so any connected device
   *  is auto-detected without the user picking a platform first. */
  platform?: Platform | 'all'
  value: string | null
  onChange(serial: string | null, device: DeviceInfo | null): void
  onReconnect?(serial: string): void
  disabled?: boolean
  label?: string
  connectionType?: ConnectionFilter
}

function deviceConnectionType(device: DeviceInfo): 'usb' | 'wireless' {
  return device.connection_type ?? (device.serial.includes(':') ? 'wireless' : 'usb')
}

function matchesConnectionType(device: DeviceInfo, filter: ConnectionFilter): boolean {
  return filter === 'all' || deviceConnectionType(device) === filter
}

export function DeviceSelector({
  platform = 'all',
  value,
  onChange,
  onReconnect,
  disabled = false,
  label,
  connectionType = 'all',
}: Props) {
  const t = useMessages()
  const [devices, setDevices] = useState<DeviceInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const { triggerRef, panelRef } = useAnchoredPanel(open, () => setOpen(false))
  const onChangeRef = useRef(onChange)
  const onReconnectRef = useRef(onReconnect)
  const missingSerialRef = useRef<string | null>(null)
  const valueRef = useRef(value)
  const inFlightRef = useRef<Promise<void> | null>(null)
  // Include disabled state so an in-flight poll cannot commit a late device
  // selection after capture startup has locked the selector.
  const scope = `${platform}:${connectionType}:${disabled ? 'disabled' : 'active'}`
  const scopeRef = useRef(scope)
  const emptyPollStreakRef = useRef(0)

  useLayoutEffect(() => {
    onChangeRef.current = onChange
    onReconnectRef.current = onReconnect
  }, [onChange, onReconnect])

  useLayoutEffect(() => {
    valueRef.current = value
  }, [value])

  useLayoutEffect(() => {
    scopeRef.current = scope
  }, [scope])

  const refresh = useCallback(async (manual = false) => {
    if (manual) {
      setLoading(true)
      setError(null)
    }

    // A manual click that lands during a silent poll waits for it, then performs
    // its own authoritative refresh. Automatic polls never overlap each other.
    while (inFlightRef.current) await inFlightRef.current
    const requestScope = scope
    const request = (async () => {
      try {
        const result = await call<{ devices: DeviceInfo[] }>('list_devices', { platform })
        if (scopeRef.current !== requestScope) return
        const selectable = result.devices.filter((d) => matchesConnectionType(d, connectionType))
        const selectedSerial = valueRef.current
        if (selectedSerial && !selectable.some((device) => device.serial === selectedSerial)) {
          missingSerialRef.current = selectedSerial
        } else if (selectedSerial && missingSerialRef.current === selectedSerial) {
          // The first empty poll intentionally retains the selection. Still
          // notify consumers when that same serial returns so app metadata is refreshed.
          missingSerialRef.current = null
          onReconnectRef.current?.(selectedSerial)
        }

        // One empty automatic response is often just an adb/usbmux reconnect
        // transition. Keep the last rendered list for that poll; a second empty
        // response is authoritative and may clear the stale capture selection.
        if (!manual && selectable.length === 0 && valueRef.current) {
          emptyPollStreakRef.current += 1
          if (emptyPollStreakRef.current < 2) return
        } else {
          emptyPollStreakRef.current = 0
        }

        setDevices(result.devices)
        setError(null)
        const current = valueRef.current
        if (selectable.length === 0) {
          if (current) {
            valueRef.current = null
            onChangeRef.current(null, null)
          }
        } else if (!selectable.some((device) => device.serial === current)) {
          const first = selectable[0] ?? null
          if (first) {
            valueRef.current = first.serial
            onChangeRef.current(first.serial, first)
          }
        }
      } catch (err) {
        emptyPollStreakRef.current = 0
        // Poll failures are silent and retain the last good list/selection. A
        // user-requested refresh still surfaces the actionable backend error.
        if (manual && scopeRef.current === requestScope) {
          setError(err instanceof Error ? err.message : String(err))
        }
      }
    })()
    inFlightRef.current = request
    try {
      await request
    } finally {
      if (inFlightRef.current === request) inFlightRef.current = null
      if (manual) setLoading(false)
    }
  }, [platform, connectionType, scope])

  useEffect(() => {
    if (disabled) return
    let cancelled = false
    let timer: number | null = null

    const schedule = () => {
      if (cancelled) return
      const delay = valueRef.current ? 3000 : 1000
      timer = window.setTimeout(async () => {
        if (!cancelled && document.visibilityState === 'visible') {
          await refresh(false)
        }
        schedule()
      }, delay)
    }
    const onVisibilityChange = () => {
      if (!cancelled && document.visibilityState === 'visible') void refresh(false)
    }

    document.addEventListener('visibilitychange', onVisibilityChange)
    if (document.visibilityState === 'visible') {
      timer = window.setTimeout(async () => {
        if (cancelled) return
        await refresh(true)
        schedule()
      }, 0)
    } else {
      schedule()
    }
    return () => {
      cancelled = true
      if (timer !== null) window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [refresh, disabled])

  const visibleDevices = devices.filter((d) => matchesConnectionType(d, connectionType))
  const selected = visibleDevices.find((d) => d.serial === value) ?? null
  const emptyLabel =
    connectionType === 'wireless'
      ? t.deviceSelector.emptyWireless
      : connectionType === 'usb'
        ? t.deviceSelector.emptyUsb
        : t.deviceSelector.emptyAny

  const noDevices = visibleDevices.length === 0

  return (
    <div className="relative">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">
          {label ?? t.deviceSelector.defaultLabel}
        </span>
        <button
          type="button"
          onClick={() => void refresh(true)}
          disabled={disabled || loading}
          className="rounded-md p-1 text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-50"
          aria-label={t.deviceSelector.refreshDevices}
          title={t.deviceSelector.refreshDevices}
        >
          {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCcw size={14} />}
        </button>
      </div>

      <div ref={triggerRef}>
        <button
          type="button"
          onClick={() => !disabled && !noDevices && setOpen((v) => !v)}
          disabled={disabled || noDevices}
          className="flex w-full items-center gap-3 rounded-[11px] border border-line bg-surface px-3.5 py-3 text-left shadow-[0_1px_2px_rgba(0,0,0,0.2)] transition-colors hover:border-accent-line disabled:opacity-60"
        >
          <span
            className="h-[9px] w-[9px] flex-none rounded-full"
            style={{
              backgroundColor: selected ? 'var(--color-ok)' : 'var(--color-faint)',
              boxShadow: selected ? '0 0 0 4px rgba(34,179,120,0.18)' : 'none',
            }}
          />
          <span className="min-w-0 flex-1 truncate text-[15px] font-medium text-ink">
            {selected
              ? selected.display_name || selected.model
              : noDevices
                ? emptyLabel
                : t.deviceSelector.selectDevice}
          </span>
          <ChevronDown
            size={14}
            className={`shrink-0 text-muted transition-transform ${open ? 'rotate-180' : ''}`}
          />
        </button>
      </div>

      {open && !noDevices &&
        createPortal(
          <div
            ref={panelRef}
            className="z-[60] overflow-y-auto rounded-[11px] border border-line bg-surface-hi shadow-[0_14px_40px_rgba(0,0,0,0.5)]"
          >
            {visibleDevices.map((d) => {
              const active = d.serial === value
              return (
                <button
                  key={`${d.serial}-${deviceConnectionType(d)}`}
                  type="button"
                  onClick={() => {
                    onChange(d.serial, d)
                    setOpen(false)
                  }}
                  className={`flex w-full items-center gap-2.5 border-b border-grid px-3 py-2.5 text-left last:border-0 ${
                    active ? 'bg-accent-soft' : 'hover:bg-hover'
                  }`}
                >
                <span
                  className="h-[7px] w-[7px] flex-none rounded-full"
                  style={{ backgroundColor: active ? 'var(--color-ok)' : 'var(--color-faint)' }}
                />
                <span className="min-w-0 flex-1">
                  <span className={`block truncate text-sm ${active ? 'font-semibold text-ink' : 'text-ink'}`}>
                    {d.display_name || d.model}
                  </span>
                  <span className="block truncate font-mono text-[11px] text-muted">
                    {d.hw_serial || d.serial} · {d.os_version}
                  </span>
                </span>
                <span className="flex flex-none flex-col items-end gap-1">
                  <span className="rounded border border-line px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                    {d.platform}
                  </span>
                  <span
                    className="text-[11px] font-semibold"
                    style={{ color: active ? 'var(--color-ok)' : 'var(--color-muted)' }}
                  >
                    {active ? t.common.connected : t.deviceSelector.standby}
                  </span>
                </span>
              </button>
            )
          })}
          </div>,
          document.body
        )}

      {error && (
        <p className="mt-1.5 text-xs text-rec" title={error}>
          {friendlyError(error, t.errors)}
        </p>
      )}
    </div>
  )
}
