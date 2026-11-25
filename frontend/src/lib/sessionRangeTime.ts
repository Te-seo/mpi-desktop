const MICROSECONDS_PER_SECOND = 1_000_000
const SECONDS_PER_MINUTE = 60

/** Format elapsed seconds as minutes:seconds, keeping microsecond precision. */
export function formatSessionRangeTime(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return '0:00'

  // Truncate rather than round so a formatted session duration never parses
  // back above the backend's exact upper bound.
  const totalMicroseconds = Math.floor((totalSeconds + Number.EPSILON) * MICROSECONDS_PER_SECOND)
  const minutes = Math.floor(totalMicroseconds / (SECONDS_PER_MINUTE * MICROSECONDS_PER_SECOND))
  const remainingMicroseconds = totalMicroseconds - minutes * SECONDS_PER_MINUTE * MICROSECONDS_PER_SECOND
  const seconds = Math.floor(remainingMicroseconds / MICROSECONDS_PER_SECOND)
  const microseconds = remainingMicroseconds % MICROSECONDS_PER_SECOND
  const fraction = microseconds === 0 ? '' : `.${String(microseconds).padStart(6, '0').replace(/0+$/, '')}`

  return `${minutes}:${String(seconds).padStart(2, '0')}${fraction}`
}

/** Parse a strict minutes:seconds value. Seconds must be two digits in 00-59. */
export function parseSessionRangeTime(value: string): number | null {
  const match = value.trim().match(/^(\d+):([0-5]\d(?:\.\d+)?)$/)
  if (!match) return null

  const minutes = Number(match[1])
  const seconds = Number(match[2])
  const totalSeconds = minutes * SECONDS_PER_MINUTE + seconds
  if (!Number.isSafeInteger(minutes) || !Number.isFinite(totalSeconds) || totalSeconds > Number.MAX_SAFE_INTEGER) {
    return null
  }
  return totalSeconds
}
