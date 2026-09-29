import assert from 'node:assert/strict'
import test from 'node:test'

import {
  sourceCompletionHandoffRoot,
  SOURCE_COMPLETION_HANDOFF_EXIT,
  SOURCE_COMPLETION_HANDOFF_SENTINEL
} from './source-completion-handoff'

test('accepts only the Windows tempfail carrying a structured source root', () => {
  assert.equal(
    sourceCompletionHandoffRoot({
      isWindows: true,
      code: SOURCE_COMPLETION_HANDOFF_EXIT,
      signal: null,
      output: `before\n${SOURCE_COMPLETION_HANDOFF_SENTINEL}=${JSON.stringify('C:\\Hermes Root\\hermes-agent')}\nafter`
    }),
    'C:\\Hermes Root\\hermes-agent'
  )
})

test('rejects ordinary tempfail, malformed roots, signals, and non-Windows exits', () => {
  for (const candidate of [
    { isWindows: true, code: SOURCE_COMPLETION_HANDOFF_EXIT, signal: null, output: 'retry later' },
    {
      isWindows: false,
      code: SOURCE_COMPLETION_HANDOFF_EXIT,
      signal: null,
      output: `${SOURCE_COMPLETION_HANDOFF_SENTINEL}=${JSON.stringify('C:\\Hermes')}`
    },
    {
      isWindows: true,
      code: 1,
      signal: null,
      output: `${SOURCE_COMPLETION_HANDOFF_SENTINEL}=${JSON.stringify('C:\\Hermes')}`
    },
    {
      isWindows: true,
      code: SOURCE_COMPLETION_HANDOFF_EXIT,
      signal: 'SIGTERM' as NodeJS.Signals,
      output: `${SOURCE_COMPLETION_HANDOFF_SENTINEL}=${JSON.stringify('C:\\Hermes')}`
    },
    {
      isWindows: true,
      code: SOURCE_COMPLETION_HANDOFF_EXIT,
      signal: null,
      output: `${SOURCE_COMPLETION_HANDOFF_SENTINEL}=not-json`
    }
  ]) {
    assert.equal(sourceCompletionHandoffRoot(candidate), null)
  }
})
