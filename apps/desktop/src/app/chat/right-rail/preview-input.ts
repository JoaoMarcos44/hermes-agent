/**
 * PREVIEW INPUT REGISTRY — real input into the preview pane's guest page, the
 * difference between the agent DRIVING the browser and merely poking its DOM.
 *
 * `executeJavaScript` can only ever dispatch synthetic events: `isTrusted` is
 * false, the browser's own hover target never moves, `:hover` rules never
 * match, and hover-gated menus never open — so a click lands on a dropdown item
 * that was never rendered. `sendInputEvent` goes in through Chromium's input
 * pipeline instead, producing the same events a hand on the mouse would.
 *
 * It has to be called on the `<webview>` ELEMENT. Sending to the embedder's
 * webContents does not reach a guest (electron/electron#20333), which is why
 * this is a per-pane registry rather than something main could do.
 *
 * Coordinates are relative to the webview. The guest page reports target
 * positions in CSS pixels, while Chromium's input router can apply the page's
 * persisted zoom before dispatching the real event. The pane therefore supplies
 * a measured correction at the input funnel; this module only owns the event
 * shape and the pure coordinate transform.
 */

import { $rightRailActiveTabId } from '@/store/layout'
import { $previewTabs } from '@/store/preview'

/** The subset of Electron's input events the agent needs to drive a page. */
export type PreviewInputEvent =
  | { button: 'left'; clickCount: number; type: 'mouseDown' | 'mouseUp'; x: number; y: number }
  | { deltaX: number; deltaY: number; type: 'mouseWheel'; x: number; y: number }
  | { keyCode: string; modifiers?: string[]; type: 'char' | 'keyDown' | 'keyUp' }
  | { type: 'mouseMove'; x: number; y: number }

export interface PreviewInputScale {
  x: number
  y: number
}

/**
 * Convert guest CSS-pixel coordinates into the units expected by Chromium's
 * real-input router. The scale is measured by the pane from a trusted pointer
 * event, rather than inferred from devicePixelRatio: display scaling and page
 * zoom are independent factors on the way through a webview.
 */
export function scalePreviewInput(event: PreviewInputEvent, scale: PreviewInputScale): PreviewInputEvent {
  if (!('x' in event) || (scale.x === 1 && scale.y === 1)) {
    return event
  }

  return { ...event, x: Math.round(event.x * scale.x), y: Math.round(event.y * scale.y) }
}

/** Derive CSS-to-input correction from one probe sent at a known input point. */
export function previewInputScale(sent: PreviewInputScale, received: PreviewInputScale): PreviewInputScale | null {
  const x = sent.x / received.x
  const y = sent.y / received.y

  if (![x, y].every(value => Number.isFinite(value) && value > 0 && value >= 0.1 && value <= 10)) {
    return null
  }

  return { x, y }
}

export interface PreviewInputHandle {
  /** Give the guest keyboard focus, so key events reach its active element. */
  focus: () => void
  /** Establish the current page's input-coordinate mapping before pointer input. */
  prepare?: () => Promise<boolean>
  send: (event: PreviewInputEvent) => void
}

const handles = new Map<string, PreviewInputHandle>()

/** Register a live pane's input channel; returns an idempotent unregister. */
export function registerPreviewInput(tabId: string, handle: PreviewInputHandle): () => void {
  handles.set(tabId, handle)

  return () => {
    if (handles.get(tabId) === handle) {
      handles.delete(tabId)
    }
  }
}

/** The ACTIVE preview tab's input channel. Null = nothing real to drive, and
 *  the caller falls back to synthesizing events inside the page. */
export function activePreviewInput(): PreviewInputHandle | null {
  const tabs = $previewTabs.get()
  const tab = tabs.find(t => t.id === $rightRailActiveTabId.get()) ?? tabs[0]

  return (tab && handles.get(tab.id)) || null
}
