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
