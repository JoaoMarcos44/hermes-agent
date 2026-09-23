import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, beforeEach, vi } from 'vitest'

import { BaseBranchPicker } from './base-branch-picker'
import { listBaseBranches } from '@/store/projects'

vi.mock('@/store/projects', () => ({ listBaseBranches: vi.fn() }))
vi.mock('@/store/coding-status', () => ({ $repoStatus: { subscribe: () => () => undefined } }))
vi.mock('@nanostores/react', () => ({ useStore: () => ({ branch: 'feature', detached: false }) }))
vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { sidebar: { projects: { branchOff: () => ({ before: 'branch off ', after: '' }), baseBranchPlaceholder: 'Search', baseBranchNone: 'None' } } } }) }))
vi.mock('@/components/ui/button', () => ({ Button: (props: any) => <button {...props} /> }))
vi.mock('@/components/ui/codicon', () => ({ Codicon: () => null }))
vi.mock('@/components/ui/popover', () => ({
  Popover: (props: any) => <div>{props.children}</div>,
  PopoverTrigger: (props: any) => props.children,
  PopoverContent: (props: any) => <div>{props.children}</div>
}))
vi.mock('@/components/ui/command', () => ({
  Command: (props: any) => <div>{props.children}</div>,
  CommandEmpty: (props: any) => <div>{props.children}</div>,
  CommandGroup: (props: any) => <div>{props.children}</div>,
  CommandInput: (props: any) => <input {...props} />,
  CommandItem: (props: any) => <button onClick={props.onSelect}>{props.children}</button>,
  CommandList: (props: any) => <div>{props.children}</div>
}))

type Branch = { name: string; isDefault: boolean; isRemote: boolean }
const branch = (name: string, isDefault = false): Branch => ({ name, isDefault, isRemote: false })

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

describe('BaseBranchPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  it('fetches once when an empty response settles, even after reopening', async () => {
    const request = deferred<Branch[]>()
    vi.mocked(listBaseBranches).mockReturnValue(request.promise)
    const onValueChange = vi.fn()
    const view = render(<BaseBranchPicker repoPath="A" value="" onValueChange={onValueChange} />)

    await act(async () => {
      request.resolve([])
      await request.promise
    })
    view.rerender(<BaseBranchPicker repoPath="A" value="" onValueChange={onValueChange} />)
    fireEvent.click(screen.getByRole('button'))
    fireEvent.click(screen.getByRole('button'))

    expect(listBaseBranches).toHaveBeenCalledTimes(1)
    expect(onValueChange).not.toHaveBeenCalled()
  })

  it('ignores a stale response and preserves a newer explicit selection while the new repository loads', async () => {
    const a = deferred<Branch[]>()
    const b = deferred<Branch[]>()
    vi.mocked(listBaseBranches).mockImplementation(path => path === 'A' ? a.promise : b.promise)
    const onValueChange = vi.fn()
    const view = render(<BaseBranchPicker repoPath="A" value="" onValueChange={onValueChange} />)
    view.rerender(<BaseBranchPicker repoPath="B" value="" onValueChange={onValueChange} />)
    view.rerender(<BaseBranchPicker repoPath="B" value="chosen" onValueChange={onValueChange} />)

    await act(async () => {
      a.resolve([branch('a-main', true)])
      await a.promise
    })
    expect(onValueChange).not.toHaveBeenCalled()

    await act(async () => {
      b.resolve([branch('b-main', true), branch('chosen')])
      await b.promise
    })
    await waitFor(() => expect(screen.getByText('chosen')).toBeTruthy())
    expect(onValueChange).not.toHaveBeenCalled()
    expect(listBaseBranches).toHaveBeenCalledTimes(2)
  })
})
