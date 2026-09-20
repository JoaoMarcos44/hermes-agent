import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { createQuitFinalization } from './quit-finalization'

test('does not arm a finalization fallback outside Windows', () => {
  const schedule = vi.fn()
  const hardExit = vi.fn()
  const finalization = createQuitFinalization({ isWindows: false, schedule, hardExit })

  finalization.arm()

  assert.equal(schedule.mock.calls.length, 0)
  assert.equal(hardExit.mock.calls.length, 0)
})

test('forces a Windows exit once the admitted quit exceeds its deadline', () => {
  let onTimeout: (() => void) | undefined
  const hardExit = vi.fn()

  const finalization = createQuitFinalization({
    isWindows: true,
    schedule: callback => {
      onTimeout = callback

      return 'timer'
    },
    hardExit
  })

  finalization.arm()
  finalization.arm()

  assert.ok(onTimeout)
  onTimeout()
  onTimeout()

  assert.deepEqual(hardExit.mock.calls, [[0]])
})

test('cancels the fallback when Electron reports a completed quit', () => {
  let onTimeout: (() => void) | undefined
  const cancel = vi.fn()
  const hardExit = vi.fn()

  const finalization = createQuitFinalization({
    isWindows: true,
    schedule: callback => {
      onTimeout = callback

      return 'timer'
    },
    cancel,
    hardExit
  })

  finalization.arm()
  finalization.cancel()
  onTimeout?.()

  assert.deepEqual(cancel.mock.calls, [['timer']])
  assert.equal(hardExit.mock.calls.length, 0)
})

test('does not re-arm after finalization has been cancelled', () => {
  const schedule = vi.fn(() => 'timer')
  const hardExit = vi.fn()
  const finalization = createQuitFinalization({ isWindows: true, schedule, hardExit })

  finalization.arm()
  finalization.cancel()
  finalization.arm()

  assert.equal(schedule.mock.calls.length, 1)
})
