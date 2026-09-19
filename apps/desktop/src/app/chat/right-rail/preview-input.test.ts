import { describe, expect, it } from 'vitest'

import { type PreviewInputEvent, previewInputScale, scalePreviewInput } from './preview-input'

describe('preview input coordinate calibration', () => {
  it('derives the correction from the guest event rather than devicePixelRatio', () => {
    expect(previewInputScale({ x: 32, y: 32 }, { x: 35.555, y: 35.555 })).toMatchObject({
      x: expect.closeTo(0.9, 3),
      y: expect.closeTo(0.9, 3)
    })
  })

  it('supports independent axis corrections', () => {
    expect(previewInputScale({ x: 32, y: 32 }, { x: 40, y: 25 })).toEqual({ x: 0.8, y: 1.28 })
  })

  it('rejects unusable calibration responses', () => {
    expect(previewInputScale({ x: 32, y: 32 }, { x: 0, y: 32 })).toBeNull()
    expect(previewInputScale({ x: 32, y: 32 }, { x: Number.NaN, y: 32 })).toBeNull()
  })

  it('scales pointer coordinates while preserving event semantics', () => {
    const click: PreviewInputEvent = { button: 'left', clickCount: 1, type: 'mouseDown', x: 500, y: 310 }
    const wheel: PreviewInputEvent = { deltaX: 0, deltaY: -120, type: 'mouseWheel', x: 400, y: 300 }

    expect(scalePreviewInput(click, { x: 0.9, y: 0.9 })).toEqual({ ...click, x: 450, y: 279 })
    expect(scalePreviewInput(wheel, { x: 0.9, y: 0.9 })).toEqual({ ...wheel, x: 360, y: 270 })
  })

  it('leaves keyboard events untouched', () => {
    const event: PreviewInputEvent = { keyCode: 'Enter', type: 'keyDown' }

    expect(scalePreviewInput(event, { x: 0.9, y: 0.9 })).toBe(event)
  })
})
