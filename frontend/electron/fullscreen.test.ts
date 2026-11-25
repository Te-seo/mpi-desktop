import { describe, expect, it } from 'vitest'
import { FullscreenIntent } from './fullscreen'

describe('FullscreenIntent', () => {
  it('reasserts the newest request when a superseded enter event arrives', () => {
    const intent = new FullscreenIntent(false)
    intent.request(true, 1)
    intent.request(false, 2)

    expect(intent.reconcile(true)).toEqual({
      kind: 'reassert',
      state: { fullscreen: false, revision: 2 },
    })
    expect(intent.reconcile(false)).toEqual({
      kind: 'publish',
      state: { fullscreen: false, revision: 2 },
    })
  })

  it('ignores an older request revision', () => {
    const intent = new FullscreenIntent(false)
    intent.request(true, 4)

    expect(intent.request(false, 3)).toEqual({
      fullscreen: true,
      revision: 4,
    })
  })
})
