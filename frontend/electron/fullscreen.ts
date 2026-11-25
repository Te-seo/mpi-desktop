export interface FullscreenSnapshot {
  fullscreen: boolean
  revision: number
}

export type FullscreenReconciliation =
  | { kind: 'publish'; state: FullscreenSnapshot }
  | { kind: 'reassert'; state: FullscreenSnapshot }

/**
 * Per-window native fullscreen intent.
 *
 * BrowserWindow transitions are asynchronous. Keeping the latest desired state
 * separate from the currently reported native state prevents a delayed
 * enter-full-screen event from winning after a rapid true -> false request.
 */
export class FullscreenIntent {
  private desiredValue: boolean
  private revisionValue = 0

  constructor(initial: boolean) {
    this.desiredValue = initial
  }

  get desired(): boolean {
    return this.desiredValue
  }

  get revision(): number {
    return this.revisionValue
  }

  snapshot(): FullscreenSnapshot {
    return {
      fullscreen: this.desiredValue,
      revision: this.revisionValue,
    }
  }

  request(fullscreen: boolean, revision: number): FullscreenSnapshot {
    if (revision >= this.revisionValue) {
      this.desiredValue = fullscreen
      this.revisionValue = revision
    }
    return this.snapshot()
  }

  reconcile(actual: boolean): FullscreenReconciliation {
    const state = this.snapshot()
    return actual === this.desiredValue
      ? { kind: 'publish', state }
      : { kind: 'reassert', state }
  }
}
