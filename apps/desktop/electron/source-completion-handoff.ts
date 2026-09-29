import type { SpawnOptions } from 'node:child_process'

import { readLiveUpdateMarker, writeUpdateMarker } from './update-marker'
import {
  observeUpdaterHandoff,
  resolveUpdateScriptHandoff,
  spawnUpdaterProcess,
  wrapHandoffForDetachedConsole,
  type UpdaterChild,
  type UpdaterHandoffOutcome,
  type UpdateScriptHandoff
} from './updater-process'

export const SOURCE_COMPLETION_HANDOFF_EXIT = 75
export const SOURCE_COMPLETION_HANDOFF_SENTINEL = 'HERMES_DESKTOP_COMPLETION_HANDOFF_REQUIRED'

export function sourceCompletionHandoffRoot({
  isWindows,
  code,
  signal,
  output
}: {
  isWindows: boolean
  code: number | null
  signal: NodeJS.Signals | null
  output: string
}): string | null {
  if (!isWindows || code !== SOURCE_COMPLETION_HANDOFF_EXIT || signal !== null) {
    return null
  }

  const prefix = `${SOURCE_COMPLETION_HANDOFF_SENTINEL}=`
  const line = output.split(/\r?\n/).find(candidate => candidate.startsWith(prefix))

  if (!line) {
    return null
  }

  try {
    const root = JSON.parse(line.slice(prefix.length))

    return typeof root === 'string' && root.length > 0 ? root : null
  } catch {
    return null
  }
}

export interface SourceCompletionHandoffShell {
  isWindows: boolean
  isQuittingForHandoff: () => boolean
  markQuittingForHandoff: () => void
  hermesHome: string
  updateHandoffDwellMs: number
  rememberLog: (message: string) => void
  quit: () => void
}

export interface SourceCompletionHandoffRuntime {
  processPid: number
  processExecPath: string
  processEnv: NodeJS.ProcessEnv
  now: () => number
  setTimeoutFn: typeof setTimeout
  resolveHandoff: (updateRoot: string) => UpdateScriptHandoff | null
  readMarker: (hermesHome: string) => { pid: number; ageMs: number } | null
  wrapHandoff: (handoff: UpdateScriptHandoff, extraArgs: string[]) => {
    command: string
    args: string[]
    detached: false
  }
  spawnHandoff: (command: string, args: string[], options: SpawnOptions) => UpdaterChild
  writeMarker: (hermesHome: string, pid: number, options: { startedAt: number }) => void
  observeHandoff: (child: UpdaterChild, settleMs: number) => Promise<UpdaterHandoffOutcome>
}

function productionRuntime(): SourceCompletionHandoffRuntime {
  return {
    processPid: process.pid,
    processExecPath: process.execPath,
    processEnv: process.env,
    now: Date.now,
    setTimeoutFn: setTimeout,
    resolveHandoff: resolveUpdateScriptHandoff,
    readMarker: readLiveUpdateMarker,
    wrapHandoff: wrapHandoffForDetachedConsole,
    spawnHandoff: (command, args, options) => spawnUpdaterProcess(command, args, options),
    writeMarker: (hermesHome, pid, options) => writeUpdateMarker(hermesHome, pid, options),
    observeHandoff: (child, settleMs) => observeUpdaterHandoff(child, settleMs)
  }
}

export async function handoffPendingSourceCompletion(
  updateRoot: string,
  shell: SourceCompletionHandoffShell,
  runtime: SourceCompletionHandoffRuntime = productionRuntime()
): Promise<boolean> {
  const alreadyQuitting = shell.isQuittingForHandoff()

  if (!shell.isWindows || alreadyQuitting) {
    return alreadyQuitting
  }

  const handoff = runtime.resolveHandoff(updateRoot)

  if (!handoff) {
    shell.rememberLog('[source-completion] no repo-owned Windows hand-off script; keeping in-process recovery')
    return false
  }

  const lockOwner = runtime.readMarker(shell.hermesHome)
  if (!lockOwner || lockOwner.pid !== runtime.processPid) {
    shell.rememberLog(
      `[source-completion] refusing hand-off: update marker is not owned by this Desktop (owner=${lockOwner?.pid ?? 'none'})`
    )
    return false
  }

  const startedAt = Math.floor((runtime.now() - lockOwner.ageMs) / 1000)
  const wrapped = runtime.wrapHandoff(handoff, [
    '-InstallRoot',
    updateRoot,
    '-DesktopPid',
    String(runtime.processPid),
    '-RelaunchExe',
    runtime.processExecPath,
    '-FinishPendingSourceCompletion'
  ])

  let child: UpdaterChild
  try {
    child = runtime.spawnHandoff(wrapped.command, wrapped.args, {
      cwd: shell.hermesHome,
      env: {
        ...runtime.processEnv,
        HERMES_HOME: shell.hermesHome,
        HERMES_INSTALL_ROOT: updateRoot,
        HERMES_UPDATE_STARTED_AT: String(startedAt)
      },
      detached: wrapped.detached,
      stdio: 'ignore'
    })
  } catch (error) {
    shell.rememberLog(`[source-completion] detached hand-off spawn failed: ${String(error)}`)
    return false
  }

  if (Number.isInteger(child.pid)) {
    runtime.writeMarker(shell.hermesHome, child.pid as number, { startedAt })
  }

  const dwellStartedAt = runtime.now()
  const outcome = await runtime.observeHandoff(child, shell.updateHandoffDwellMs)

  if (!outcome.ok) {
    shell.rememberLog(
      `[source-completion] detached hand-off not viable: ${outcome.message ?? outcome.reason ?? 'unknown failure'}`
    )
    return false
  }

  shell.rememberLog(`[source-completion] handed pending tail to ${handoff.scriptPath}; quitting Desktop for rebuild`)
  shell.markQuittingForHandoff()
  runtime.setTimeoutFn(
    shell.quit,
    Math.max(0, shell.updateHandoffDwellMs - (runtime.now() - dwellStartedAt))
  )
  return true
}
