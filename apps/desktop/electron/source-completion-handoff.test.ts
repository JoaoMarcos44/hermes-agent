import assert from 'node:assert/strict'
import test from 'node:test'

import {
  handoffPendingSourceCompletion,
  sourceCompletionHandoffRoot,
  SOURCE_COMPLETION_HANDOFF_EXIT,
  SOURCE_COMPLETION_HANDOFF_SENTINEL,
  type SourceCompletionHandoffRuntime,
  type SourceCompletionHandoffShell
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

function shellFixture(overrides: Partial<SourceCompletionHandoffShell> = {}) {
  let quitting = false
  let quits = 0
  const logs: string[] = []
  const shell: SourceCompletionHandoffShell = {
    isWindows: true,
    isQuittingForHandoff: () => quitting,
    markQuittingForHandoff: () => {
      quitting = true
    },
    hermesHome: String.raw`C:\Hermes Home`,
    updateHandoffDwellMs: 1000,
    rememberLog: message => logs.push(message),
    quit: () => {
      quits += 1
    },
    ...overrides
  }

  return {
    shell,
    logs,
    isQuitting: () => quitting,
    quitCount: () => quits
  }
}

function runtimeFixture(overrides: Partial<SourceCompletionHandoffRuntime> = {}) {
  const writes: Array<{ home: string; pid: number; startedAt: number }> = []
  const spawns: Array<{ command: string; args: string[]; options: Record<string, unknown> }> = []
  const scheduled: Array<{ callback: () => void; delay: number }> = []

  const runtime: SourceCompletionHandoffRuntime = {
    processPid: 42,
    processExecPath: String.raw`C:\Hermes\Hermes.exe`,
    processEnv: { KEEP_ME: '1' },
    now: () => 20_000,
    setTimeoutFn: ((callback: () => void, delay: number) => {
      scheduled.push({ callback, delay })
      return 0 as unknown as ReturnType<typeof setTimeout>
    }) as typeof setTimeout,
    resolveHandoff: () => ({
      command: 'powershell',
      args: ['-NoProfile', '-File', String.raw`C:\repo\scripts\desktop-update\windows.ps1`],
      scriptPath: String.raw`C:\repo\scripts\desktop-update\windows.ps1`
    }),
    readMarker: () => ({ pid: 42, ageMs: 5000 }),
    wrapHandoff: (_handoff, extraArgs) => ({
      command: 'cmd.exe',
      args: ['/d', '/s', '/c', ...extraArgs],
      detached: false
    }),
    spawnHandoff: (command, args, options) => {
      spawns.push({ command, args, options: options as Record<string, unknown> })
      return { pid: 77, unref: () => undefined }
    },
    writeMarker: (home, pid, { startedAt }) => {
      writes.push({ home, pid, startedAt })
    },
    observeHandoff: async () => ({ ok: true }),
    ...overrides
  }

  return { runtime, writes, spawns, scheduled }
}

test('refuses the coordinator when the update marker is not owned by this Desktop', async () => {
  const { shell, quitCount } = shellFixture()
  const { runtime, spawns } = runtimeFixture({
    readMarker: () => ({ pid: 99, ageMs: 5000 })
  })

  assert.equal(await handoffPendingSourceCompletion('C:\\repo', shell, runtime), false)
  assert.equal(spawns.length, 0)
  assert.equal(quitCount(), 0)
})

test('a synchronous spawn failure keeps the Desktop running', async () => {
  const { shell, quitCount, isQuitting } = shellFixture()
  const { runtime, writes } = runtimeFixture({
    spawnHandoff: () => {
      throw new Error('spawn denied')
    }
  })

  assert.equal(await handoffPendingSourceCompletion('C:\\repo', shell, runtime), false)
  assert.equal(writes.length, 0)
  assert.equal(isQuitting(), false)
  assert.equal(quitCount(), 0)
})

test('an observed handoff failure rewrites the bridge marker but never quits', async () => {
  const { shell, quitCount, isQuitting } = shellFixture()
  const { runtime, writes } = runtimeFixture({
    observeHandoff: async () => ({ ok: false, reason: 'spawn-error', message: 'ENOENT' })
  })

  assert.equal(await handoffPendingSourceCompletion('C:\\repo', shell, runtime), false)
  assert.deepEqual(writes, [{ home: String.raw`C:\Hermes Home`, pid: 77, startedAt: 15 }])
  assert.equal(isQuitting(), false)
  assert.equal(quitCount(), 0)
})

test('successful handoff preserves acquisition time and delays quit only for the remaining dwell', async () => {
  const times = [20_000, 20_000, 20_250]
  const { shell, quitCount, isQuitting } = shellFixture()
  const { runtime, writes, spawns, scheduled } = runtimeFixture({
    now: () => times.shift() ?? 20_250
  })

  assert.equal(await handoffPendingSourceCompletion('C:\\repo with spaces', shell, runtime), true)
  assert.deepEqual(writes, [{ home: String.raw`C:\Hermes Home`, pid: 77, startedAt: 15 }])
  assert.equal(spawns.length, 1)
  assert.equal(spawns[0].command, 'cmd.exe')
  assert.ok(spawns[0].args.includes('-FinishPendingSourceCompletion'))
  assert.equal((spawns[0].options.env as NodeJS.ProcessEnv).HERMES_UPDATE_STARTED_AT, '15')
  assert.equal((spawns[0].options.env as NodeJS.ProcessEnv).KEEP_ME, '1')
  assert.equal(isQuitting(), true)
  assert.equal(quitCount(), 0)
  assert.equal(scheduled.length, 1)
  assert.equal(scheduled[0].delay, 750)

  scheduled[0].callback()
  assert.equal(quitCount(), 1)
})
