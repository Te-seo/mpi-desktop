import { describe, expect, it } from 'vitest'
import { METRIC_COLORS, SESSION_DASH, SESSION_LABELS } from './sessionColors'

describe('dashboard chart identities', () => {
  it('assigns a distinct fixed colour to every dashboard metric', () => {
    expect(Object.keys(METRIC_COLORS)).toEqual([
      'fps',
      'temp',
      'cpu',
      'cpu_app',
      'cpu_norm',
      'gpu',
      'mem',
      'power',
      'battery',
    ])
    expect(new Set(Object.values(METRIC_COLORS)).size).toBe(9)
  })

  it('retains stable non-colour session identities', () => {
    expect(SESSION_LABELS).toEqual(['A', 'B', 'C', 'D'])
    expect(SESSION_DASH).toEqual([undefined, '6 4', '2 3', '1 4'])
  })
})
