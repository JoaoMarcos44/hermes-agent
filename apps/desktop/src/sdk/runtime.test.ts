import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockSdk = vi.hoisted(() => ({
  $accentOverride: () => 'accent',
  Button: () => 'ButtonComponent',
  SkillsView: () => 'SkillsViewComponent'
}))

vi.mock('./index', () => mockSdk)

import { installPluginSdk, sdkImportMap } from './runtime'

describe('sdk/runtime', () => {
  beforeEach(() => {
    delete (globalThis as Record<string, unknown>).__HERMES_PLUGIN_SDK__
    delete (globalThis as Record<string, unknown>).__HERMES_REACT__
    delete (globalThis as Record<string, unknown>).__HERMES_REACT_JSX__
    delete (globalThis as Record<string, unknown>).__HERMES_REACT_JSX_DEV__
  })

  it('installs all runtime SDK namespaces onto globalThis lazily at call time', () => {
    installPluginSdk()

    const g = globalThis as Record<string, unknown>
    expect(g.__HERMES_PLUGIN_SDK__).toBeDefined()
    expect(g.__HERMES_REACT__).toBeDefined()
    expect(g.__HERMES_REACT_JSX__).toBeDefined()
    expect(g.__HERMES_REACT_JSX_DEV__).toBeDefined()

    const installed = g.__HERMES_PLUGIN_SDK__ as Record<string, unknown>
    expect(installed.Button).toBeDefined()
    expect(installed.SkillsView).toBeDefined()
    expect(installed.$accentOverride).toBeDefined()
  })

  it('provides the runtime import map for all expected specifiers', () => {
    const map = sdkImportMap()

    expect(map['@hermes/plugin-sdk']).toBeDefined()
    expect(map['react/jsx-dev-runtime']).toBeDefined()
    expect(map['react/jsx-runtime']).toBeDefined()
    expect(map.react).toBeDefined()

    expect(typeof map['@hermes/plugin-sdk']).toBe('string')
    expect(map['@hermes/plugin-sdk']).toMatch(/^(blob:|data:)/)
  })

  it('does not throw when namespaces are accessed or evaluated at call time', () => {
    // Calling installPluginSdk and sdkImportMap repeatedly is idempotent and does not throw
    expect(() => installPluginSdk()).not.toThrow()
    expect(() => sdkImportMap()).not.toThrow()
  })
})
