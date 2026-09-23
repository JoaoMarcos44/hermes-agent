import { describe, expect, it } from 'vitest'

import { baseBranchAfterLoad, shouldLoadBaseBranches } from './base-branch-picker'

describe('base branch loading', () => {
  it('loads an empty result once instead of retrying forever', () => {
    expect(shouldLoadBaseBranches('/repo', false, false)).toBe(true)
    expect(shouldLoadBaseBranches('/repo', true, false)).toBe(false)
  })

  it('preserves an explicit base and only defaults when it is absent', () => {
    const branches = [
      { isDefault: true, isRemote: false, name: 'main' },
      { isDefault: false, isRemote: false, name: 'feature' }
    ]

    expect(baseBranchAfterLoad('feature', branches)).toBe('feature')
    expect(baseBranchAfterLoad('missing', branches)).toBe('main')
    expect(baseBranchAfterLoad('explicit', [])).toBe('explicit')
  })
})
