import { Check, Loader2, Pencil, Wifi } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { call } from '../lib/ipc'
import { friendlyError } from '../lib/friendlyError'
import { useMessages } from '../lib/i18n'
import { DeviceSelector } from './DeviceSelector'
import type { DeviceInfo, Platform } from '../lib/types'

interface PrepareWirelessResult {
  serial: string
  ip?: string
  port?: number
  device: DeviceInfo
  message?: string
}

interface Props {
  disabled?: boolean
  serial: string | null
  device: DeviceInfo | null
  onChange(serial: string | null, device: DeviceInfo | null): void
  /** Reported whenever an auto/retry wireless switch is in flight, so the parent
   *  can lock the USB/Wireless toggle for the (up-to-90 s) duration — clicking it
   *  mid-switch otherwise wipes the selection and lets a stale result clobber it. */
  onPreparingChange?(preparing: boolean): void
}

type Mode = 'prepare' | 'direct'

function isWireless(serial: string | null, device: DeviceInfo | null): boolean {
  if (!serial) return false
  return (device?.connection_type ?? (serial.includes(':') ? 'wireless' : 'usb')) === 'wireless'
}

/**
 * Wireless connection flow. Selecting "Wireless" auto-attempts the switch on the
 * detected USB device — no separate button press — so any connected device is
 * converted to a wireless target as soon as it is recognized. The platform is
 * inferred from the selected device, so the user never picks Android/iOS. A
 * secondary mode lets users re-select a device that is already wireless.
 */
export function WirelessSetup({ disabled = false, serial, device, onChange, onPreparingChange }: Props) {
  const t = useMessages()
  const [mode, setMode] = useState<Mode>('prepare')
  const [usbSerial, setUsbSerial] = useState<string | null>(null)
  const [usbDevice, setUsbDevice] = useState<DeviceInfo | null>(null)
  const [preparing, setPreparing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rawError, setRawError] = useState<string | null>(null)
  // The USB serial we have already auto-attempted, so detection doesn't loop.
  const attemptedRef = useRef<string | null>(null)
  // Guards async prepare_wireless results. prepare_wireless can take up to 90 s;
  // if this panel unmounts before it resolves (mode toggle, the key= remount on
  // success, or reset), writing the stale result back into capture state would
  // clobber a newer selection and make the switch look broken. Skip any write
  // once unmounted.
  const isMountedRef = useRef(true)
  useEffect(() => {
    isMountedRef.current = true
    return () => {
      isMountedRef.current = false
    }
  }, [])

  // Mirror the in-flight state up so the parent can disable the USB/Wireless
  // toggle while a switch is running. Reset to false on unmount so a remount
  // mid-prepare never leaves the parent stuck thinking a switch is still active.
  useEffect(() => {
    onPreparingChange?.(preparing)
    return () => onPreparingChange?.(false)
  }, [preparing, onPreparingChange])

  const ready = isWireless(serial, device)
  const preparePlatform: Platform = usbDevice?.platform ?? device?.platform ?? 'android'

  const prepare = useCallback(
    async (targetSerial: string, targetPlatform: Platform) => {
      setPreparing(true)
      setError(null)
      setRawError(null)
      try {
        const payload: { platform: Platform; serial: string; port?: number } = {
          platform: targetPlatform,
          serial: targetSerial,
        }
        if (targetPlatform === 'android') payload.port = 5555
        const result = await call<PrepareWirelessResult>('prepare_wireless', payload, 90_000)
        // The panel was torn down while the (slow) switch was running — dropping
        // the result here is correct; writing it would overwrite a newer choice.
        if (!isMountedRef.current) return
        onChange(result.serial, result.device)
      } catch (err) {
        if (!isMountedRef.current) return
        const raw = err instanceof Error ? err.message : String(err)
        setRawError(raw)
        setError(friendlyError(raw, t.errors))
      } finally {
        if (isMountedRef.current) setPreparing(false)
      }
    },
    [onChange, t.errors]
  )

  // Auto-attempt the wireless switch the moment a USB device is detected/selected
  // while this (Wireless) panel is showing — once per device serial.
  useEffect(() => {
    if (disabled || preparing || mode !== 'prepare') return
    if (!usbSerial) return
    if (attemptedRef.current === usbSerial) return
    attemptedRef.current = usbSerial
    void prepare(usbSerial, preparePlatform)
  }, [usbSerial, preparePlatform, disabled, preparing, mode, prepare])

  function retry() {
    if (!usbSerial) return
    attemptedRef.current = usbSerial
    void prepare(usbSerial, preparePlatform)
  }

  function reset() {
    setError(null)
    setRawError(null)
    setMode('prepare')
    attemptedRef.current = null
    onChange(null, null)
  }

  if (ready) {
    return (
      <div className="inline-flex items-center gap-1.5 rounded-md border border-emerald-300 bg-emerald-50 px-2 py-1 text-xs text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
        <Check size={14} aria-hidden />
        <span className="whitespace-nowrap" title={serial ?? undefined}>
          {t.wireless.connectedLabel(
            device?.display_name || device?.model || serial || ''
          )}
        </span>
        <button
          type="button"
          onClick={reset}
          disabled={disabled}
          className="inline-flex items-center gap-1 rounded border border-emerald-300 px-1.5 py-0.5 hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-emerald-700 dark:hover:bg-emerald-900"
          title={t.wireless.changeDeviceTitle}
        >
          <Pencil size={11} /> {t.wireless.change}
        </button>
      </div>
    )
  }

  const prepareHint =
    preparePlatform === 'ios' ? t.wireless.prepareHintIos : t.wireless.prepareHintAndroid

  return (
    <>
      {mode === 'prepare' ? (
        <>
          <DeviceSelector
            platform="all"
            label={t.wireless.usbDeviceLabel}
            value={usbSerial}
            onChange={(nextSerial, nextDevice) => {
              // A different USB device was (re)selected — re-arm the one-shot
              // auto-attempt so reconnecting or swapping a device switches again
              // instead of silently doing nothing (attemptedRef would still hold
              // the previous serial).
              if (nextSerial !== usbSerial) attemptedRef.current = null
              setUsbSerial(nextSerial)
              setUsbDevice(nextDevice)
            }}
            disabled={disabled || preparing}
            connectionType="usb"
          />
          <div
            className="inline-flex items-center gap-1.5 text-sm text-muted"
            title={prepareHint}
          >
            {preparing ? (
              <>
                <Loader2 size={16} className="animate-spin" /> {t.wireless.switching}
              </>
            ) : usbSerial ? (
              <button
                type="button"
                onClick={retry}
                disabled={disabled}
                className="inline-flex items-center gap-1.5 rounded-md border border-neutral-300 px-2.5 py-1 hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
              >
                <Wifi size={16} /> {t.wireless.retrySwitch}
              </button>
            ) : (
              <span className="inline-flex items-center gap-1.5">
                <Wifi size={16} /> {t.wireless.autoSwitchHint}
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={() => setMode('direct')}
            disabled={disabled || preparing}
            className="text-xs text-neutral-500 underline-offset-2 hover:underline disabled:opacity-50 dark:text-neutral-400"
            title={t.wireless.directSelectTitle}
          >
            {t.wireless.directSelect}
          </button>
        </>
      ) : (
        <>
          <DeviceSelector
            platform="all"
            label={t.wireless.wirelessDeviceLabel}
            value={serial}
            onChange={onChange}
            disabled={disabled}
            connectionType="wireless"
          />
          <button
            type="button"
            onClick={() => setMode('prepare')}
            disabled={disabled}
            className="text-xs text-neutral-500 underline-offset-2 hover:underline disabled:opacity-50 dark:text-neutral-400"
          >
            {t.wireless.prepareUsbSwitch}
          </button>
        </>
      )}
      {error && (
        <span className="max-w-md truncate text-xs text-red-600" title={rawError ?? error}>
          {error}
        </span>
      )}
    </>
  )
}
