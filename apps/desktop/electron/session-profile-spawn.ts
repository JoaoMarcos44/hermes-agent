export type LocalBackendKind = 'local' | 'remote' | 'url'

export function sessionProfileSpawnSpec({
  selectedProfile,
  hermesHome,
  backendKind = 'local'
}: {
  selectedProfile: string
  hermesHome: string | null
  backendKind?: LocalBackendKind
}) {
  if (backendKind !== 'local' || !hermesHome) {
    return null
  }

  return {
    argvProfileFlag: ['--profile', selectedProfile],
    envOverlay: { HERMES_HOME: hermesHome }
  }
}
