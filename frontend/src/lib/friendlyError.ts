// Maps raw backend/CLI/main-process error strings to actionable, localized
// guidance. Patterns target stable substrings of mpi backend errors
// (mpi/ios/pmd3.py, mpi/android/adb.py), common adb/pymobiledevice3
// output, and the stable English sentinel codes emitted by ipc.ts / electron
// main.ts. The message text comes from the active-locale `errors` catalog, so
// callers pass `t.errors` (from useMessages()). Unmatched natural-language text
// passes through unchanged; unmatched developer-format strings collapse to the
// generic "unexpected" message.

import type { ErrorMessages } from './i18n/namespaces/_errors'

const RULES: Array<{ pattern: RegExp; key: keyof ErrorMessages }> = [
  { pattern: /auth-required|account-changed/i, key: 'signInRequired' },
  { pattern: /quota-exhausted/i, key: 'measurementQuotaExhausted' },
  { pattern: /quota-unavailable|^network$/i, key: 'measurementQuotaUnavailable' },
  {
    // Windows-specific: the Wi-Fi toggle succeeded. On Windows the phone only
    // reconnects wirelessly through the pymobiledevice3 tunnel, which forms over
    // Wi-Fi once the USB cable is removed — so guide the user to unplug it.
    pattern: /UNPLUG the USB cable|switching to wireless on Windows/i,
    key: 'iosWifiUnplug',
  },
  { pattern: /no wireless usbmux device appeared/i, key: 'noWirelessDevice' },
  { pattern: /require(s)? a pymobiledevice3 tunnel|Tunneld|start-tunnel/i, key: 'tunnelRequired' },
  { pattern: /pymobiledevice3 executable not found/i, key: 'pmd3NotInstalled' },
  { pattern: /DeveloperModeIsNotEnabled|developer mode/i, key: 'developerModeOff' },
  { pattern: /PasswordRequiredError|device is password protected/i, key: 'passwordRequired' },
  { pattern: /InvalidHostID|pairing record|NotTrustedError/i, key: 'pairingInvalid' },
  { pattern: /could not resolve running pid/i, key: 'pidNotFound' },
  // Focused-app measurement sentinels from mpi (backend.py
  // AppNotRunningError / AppExitedError / AppBackgroundedError): start refused
  // because the selected app isn't running, the measured app died mid-capture,
  // or it left the foreground (backgrounded/suspended) — capture ended.
  { pattern: /app-not-running/i, key: 'appNotRunning' },
  { pattern: /app-backgrounded/i, key: 'appBackgrounded' },
  { pattern: /app-exited/i, key: 'appExited' },
  { pattern: /target-app-termination-failed/i, key: 'targetAppTerminationFailed' },
  { pattern: /device unauthorized/i, key: 'deviceUnauthorized' },
  { pattern: /adb.*not found|cannot find adb/i, key: 'adbNotFound' },

  // Engine lifecycle. The specific main-process sentinels precede the broad
  // "engine not running" rule so they win.
  { pattern: /engine-start-failed-dev/i, key: 'engineStartFailedDev' },
  { pattern: /engine-start-failed/i, key: 'engineStartFailed' },
  { pattern: /engine-exited/i, key: 'engineExited' },
  {
    // data-engine sidecar missing or not running (raw backend or ipc sentinel).
    pattern: /data-engine|성능 측정 엔진|engine (is )?not running|engine[- ]not[- ]running|no-process/i,
    key: 'engineNotRunning',
  },
  { pattern: /backend timeout after/i, key: 'backendTimeout' },

  // IPC sentinel emitted by ipc.ts outside the desktop shell.
  { pattern: /desktop-only/i, key: 'desktopOnly' },

  // --- Endpoint transport (data send) --------------------------------------
  // Raw strings come from the Electron main process: `HTTP <status>`,
  // `timeout after <ms>ms`, `invalid (endpoint) url:`, `unsupported protocol:`,
  // and Node network error messages.
  { pattern: /HTTP 4\d\d\b|\b401 unauthorized/i, key: 'endpoint4xx' },
  { pattern: /HTTP 5\d\d\b/i, key: 'endpoint5xx' },
  { pattern: /timeout after \d+ ?ms/i, key: 'transportTimeout' },
  { pattern: /invalid (endpoint )?url|endpoint url required/i, key: 'invalidEndpointUrl' },
  { pattern: /unsupported protocol/i, key: 'unsupportedProtocol' },
  {
    pattern:
      /ECONNREFUSED|ENOTFOUND|EAI_AGAIN|getaddrinfo|ETIMEDOUT|ECONNRESET|socket hang ?up|network error|fetch failed/i,
    key: 'networkError',
  },
]

// Heuristic last resort: does this read like a raw developer/diagnostic string
// (English error codes, stack frames, exception class names, `cmd=`/path:line
// fragments) rather than user-facing guidance? Any Hangul means it is already
// localized guidance and passes through untouched.
// The stack-frame alternative uses bounded `[^()\n]` runs (not greedy `.+`) so it
// cannot backtrack catastrophically on long pathological inputs (ReDoS).
const DEV_ERROR_SIGNALS =
  /\bError:|\bException\b|Traceback|\bat [^()\n]+\([^()\n]*:\d+:\d+\)|node:internal|\berrno\b|\bENOENT\b|\bEACCES\b|\bEPERM\b|\bEBUSY\b|\bEINVAL\b|spawn \w+|cannot read propert|\bcmd=[\w.-]+|\.(py|tsx?|jsx?|mjs|cjs):\d+|[A-Za-z_][A-Za-z0-9_]*Error\b/

function looksLikeDevError(raw: string): boolean {
  // Hangul syllables (AC00–D7A3) or compatibility jamo (3130–318F) ⇒ localized.
  if (/[가-힣㄰-㆏]/.test(raw)) return false
  // Only a short prefix is needed to classify a dev-format string; capping the
  // tested length is a second guard against pathological inputs.
  return DEV_ERROR_SIGNALS.test(raw.slice(0, 2000))
}

/** Translate a raw error into localized guidance using the active locale's
 * `errors` catalog (pass `t.errors` from useMessages()). Returns the input
 * unchanged when no rule matches and it does not look like a raw developer
 * string. Pass the original raw text to a tooltip when you still want it for
 * debugging. */
export function friendlyError(raw: string, m: ErrorMessages): string {
  const text = (raw ?? '').toString()
  for (const { pattern, key } of RULES) {
    if (pattern.test(text)) return m[key]
  }
  // Unmatched but clearly a developer-format error/code/stack → generic guidance
  // so internals never reach the user. Natural-language text passes through.
  if (looksLikeDevError(text)) {
    return m.unexpected
  }
  return text
}
