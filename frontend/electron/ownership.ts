export type OwnedRequestKind = 'capture' | 'guard' | 'preview' | null

export interface TakenRequest {
  ownerId: number | null
  kind: OwnedRequestKind
}

export interface RoutedEvent {
  ownerId: number | null
  /** A guard triggered after its window disappeared. The caller must stop this
   * capture immediately so the sidecar cannot keep the device reserved. */
  orphanedCaptureId: string | null
}

export interface ReleasedWindowResources {
  captureIds: string[]
  guardIds: string[]
  previewIds: string[]
}

/** Pure ownership registry shared by Electron routing and unit tests.
 *
 * A temperature guard owns a physical device before a capture exists. The
 * `temperature_guard_started` transition deletes that ownership and installs
 * capture ownership in one synchronous operation, leaving no routing gap in
 * which an immediate first snapshot could be broadcast to the wrong window. */
export class WindowOwnership {
  private readonly requestOwners = new Map<string, number>()
  private readonly captureRequests = new Set<string>()
  private readonly guardRequests = new Set<string>()
  private readonly previewRequests = new Set<string>()
  private readonly captureOwners = new Map<string, number>()
  private readonly guardOwners = new Map<string, number>()
  private readonly previewOwners = new Map<string, number>()

  rememberRequest(requestId: string, ownerId: number, command: unknown): void {
    this.requestOwners.set(requestId, ownerId)
    if (command === 'start_capture') this.captureRequests.add(requestId)
    if (command === 'create_temperature_guard') this.guardRequests.add(requestId)
    if (command === 'create_temperature_preview') this.previewRequests.add(requestId)
  }

  takeRequest(requestId: string): TakenRequest {
    const ownerId = this.requestOwners.get(requestId) ?? null
    this.requestOwners.delete(requestId)
    let kind: OwnedRequestKind = null
    if (this.captureRequests.delete(requestId)) kind = 'capture'
    if (this.guardRequests.delete(requestId)) kind = 'guard'
    if (this.previewRequests.delete(requestId)) kind = 'preview'
    return { ownerId, kind }
  }

  bindCapture(captureId: string, ownerId: number): void {
    this.captureOwners.set(captureId, ownerId)
  }

  bindGuard(guardId: string, ownerId: number): void {
    this.guardOwners.set(guardId, ownerId)
  }

  bindPreview(previewId: string, ownerId: number): void {
    this.previewOwners.set(previewId, ownerId)
  }

  unbindCapture(captureId: string): void {
    this.captureOwners.delete(captureId)
  }

  unbindGuard(guardId: string): void {
    this.guardOwners.delete(guardId)
  }

  unbindPreview(previewId: string): void {
    this.previewOwners.delete(previewId)
  }

  ownerForCapture(captureId: string): number | null {
    return this.captureOwners.get(captureId) ?? null
  }

  ownerForGuard(guardId: string): number | null {
    return this.guardOwners.get(guardId) ?? null
  }

  ownerForPreview(previewId: string): number | null {
    return this.previewOwners.get(previewId) ?? null
  }

  routeEvent(
    event: unknown,
    guardId: string | null,
    captureId: string | null,
    previewId: string | null = null
  ): RoutedEvent {
    if (previewId && typeof event === 'string' && event.startsWith('temperature_preview_')) {
      const ownerId = this.previewOwners.get(previewId) ?? null
      if (event === 'temperature_preview_ended') this.previewOwners.delete(previewId)
      return { ownerId, orphanedCaptureId: null }
    }

    if (event === 'temperature_guard_started' && guardId && captureId) {
      const ownerId = this.guardOwners.get(guardId) ?? null
      this.guardOwners.delete(guardId)
      if (ownerId !== null) this.captureOwners.set(captureId, ownerId)
      return {
        ownerId,
        orphanedCaptureId: ownerId === null ? captureId : null,
      }
    }

    if (guardId && typeof event === 'string' && event.startsWith('temperature_guard_')) {
      const ownerId = this.guardOwners.get(guardId) ?? null
      if (event === 'temperature_guard_ended') this.guardOwners.delete(guardId)
      return { ownerId, orphanedCaptureId: null }
    }

    if (captureId) {
      const ownerId = this.captureOwners.get(captureId) ?? null
      if (event === 'capture_ended') this.captureOwners.delete(captureId)
      return { ownerId, orphanedCaptureId: null }
    }

    return { ownerId: null, orphanedCaptureId: null }
  }

  /** Undo the guard->capture transfer if the target webContents disappears
   * between routing lookup and send. The caller uses the returned id to stop the
   * now-orphaned backend capture. */
  failEventDelivery(event: unknown, captureId: string | null): string | null {
    if (event !== 'temperature_guard_started' || !captureId) return null
    this.captureOwners.delete(captureId)
    return captureId
  }

  releaseWindow(ownerId: number): ReleasedWindowResources {
    const captureIds: string[] = []
    const guardIds: string[] = []
    const previewIds: string[] = []
    for (const [captureId, owner] of this.captureOwners) {
      if (owner === ownerId) {
        this.captureOwners.delete(captureId)
        captureIds.push(captureId)
      }
    }
    for (const [guardId, owner] of this.guardOwners) {
      if (owner === ownerId) {
        this.guardOwners.delete(guardId)
        guardIds.push(guardId)
      }
    }
    for (const [previewId, owner] of this.previewOwners) {
      if (owner === ownerId) {
        this.previewOwners.delete(previewId)
        previewIds.push(previewId)
      }
    }
    for (const [requestId, owner] of this.requestOwners) {
      if (owner === ownerId) {
        this.requestOwners.delete(requestId)
        // Preserve the request-kind tombstone. If the backend finishes this
        // create/open after the window closes, takeRequest() must still know
        // which stop command to send for the returned orphan resource.
      }
    }
    return { captureIds, guardIds, previewIds }
  }
}
