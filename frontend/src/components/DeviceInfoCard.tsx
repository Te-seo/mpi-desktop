import type { DeviceInfo, Platform } from '../lib/types'
import { useMessages } from '../lib/i18n'

interface Props {
  device: DeviceInfo | null
  platform: Platform
  connectionMode: 'usb' | 'wireless'
  appLabel: string | null
  bundleId: string | null
  intervalSec: number
}

export function DeviceInfoCard({
  device,
  platform,
  connectionMode,
  appLabel,
  bundleId,
  intervalSec,
}: Props) {
  const t = useMessages()
  const rows: { k: string; v: string }[] = device
    ? [
        {
          k: t.deviceInfo.labelName,
          v: device.display_name || device.model || '—',
        },
        { k: t.deviceInfo.labelModel, v: device.model || '—' },
        ...(platform === 'ios' ? [
          { k: t.deviceInfo.labelModelNumber, v: device.model_number || '—' },
          { k: t.deviceInfo.labelModelIdentifier, v: device.model_identifier || '—' },
        ] : []),
        { k: t.deviceInfo.labelPlatform, v: platform === 'ios' ? 'iOS' : 'Android' },
        { k: t.deviceInfo.labelOsVersion, v: device.os_version || '—' },
        { k: t.deviceInfo.labelBuildVersion, v: device.build_version || '—' },
        { k: t.deviceInfo.labelBuildName, v: device.build_name || '—' },
        {
          k: t.deviceInfo.labelConnection,
          v:
            (device.connection_type ?? connectionMode) === 'wireless'
              ? t.deviceInfo.connectionWireless
              : 'USB',
        },
        // For wireless adb, `serial` is the ip:port transport id; show the real
        // hardware serial (hw_serial) when the backend resolved it.
        { k: t.deviceInfo.labelSerial, v: device.hw_serial || device.serial },
        { k: t.deviceInfo.labelMeasureApp, v: appLabel || bundleId || '—' },
        { k: t.deviceInfo.labelSampleInterval, v: `${intervalSec}s` },
      ]
    : []

  return (
    <div className="mpi-panel p-4">
      <div className="mpi-eyebrow mb-4">
        {t.deviceInfo.header}
      </div>
      {device ? (
        <div className="flex flex-col gap-2.5">
          {rows.map((r) => (
            <div key={r.k} className="flex items-baseline justify-between gap-3">
              <span className="flex-none text-[13px] text-muted">{r.k}</span>
              <span title={r.v} className="min-w-0 break-all text-right font-mono text-[12px] font-medium text-ink">
                {r.v}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[13px] text-muted">{t.deviceInfo.emptyState}</p>
      )}

    </div>
  )
}
