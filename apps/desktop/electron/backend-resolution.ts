export interface ActiveBackendResolutionOptions {
  activeRuntimeUsable: boolean
  bootstrapRepairRequested: boolean
  ignoreExisting: boolean
}

/** Whether the usable managed runtime may win backend resolution. */
export function shouldUseActiveBackend({
  activeRuntimeUsable,
  bootstrapRepairRequested,
  ignoreExisting
}: ActiveBackendResolutionOptions): boolean {
  return activeRuntimeUsable && !bootstrapRepairRequested && !ignoreExisting
}

/** Whether the system-Python fallback may win backend resolution. */
export function shouldUseSystemPythonBackend(ignoreExisting: boolean): boolean {
  return !ignoreExisting
}