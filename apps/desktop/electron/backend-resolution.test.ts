import assert from 'node:assert/strict'

import { test } from 'vitest'

import { shouldUseActiveBackend, shouldUseSystemPythonBackend } from './backend-resolution'

test('ignore-existing skips a usable active runtime', () => {
  assert.equal(
    shouldUseActiveBackend({
      activeRuntimeUsable: true,
      bootstrapRepairRequested: false,
      ignoreExisting: true
    }),
    false
  )
})

test('without ignore-existing, active runtime precedence is preserved', () => {
  assert.equal(
    shouldUseActiveBackend({
      activeRuntimeUsable: true,
      bootstrapRepairRequested: false,
      ignoreExisting: false
    }),
    true
  )
})

test('bootstrap repair still bypasses the active runtime', () => {
  assert.equal(
    shouldUseActiveBackend({
      activeRuntimeUsable: true,
      bootstrapRepairRequested: true,
      ignoreExisting: false
    }),
    false
  )
})

test('system-Python fallback is skipped only when ignore-existing is enabled', () => {
  assert.equal(shouldUseSystemPythonBackend(true), false)
  assert.equal(shouldUseSystemPythonBackend(false), true)
})
