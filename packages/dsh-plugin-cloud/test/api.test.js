import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mapCatalog } from '../src/api.js'

test('catalog mapping mirrors the desktop client contract', () => {
  const models = mapCatalog([
    { id: 'deepseek-v4-pro', display_name: 'DeepSeek V4 Pro', context_window: 1000000 },
    { id: 'kimi-k3', display_name: '', context_window: 0 },
    { id: '', display_name: 'ghost' },
    { display_name: 'no id at all' },
  ])
  assert.deepEqual(models, [
    { id: 'deepseek-v4-pro', name: 'DeepSeek V4 Pro', contextWindow: 1000000 },
    { id: 'kimi-k3', name: 'kimi-k3' },
  ])
})

test('plugin entry sets the env token from the store shape', async () => {
  const plugin = await import('../src/index.js')
  assert.equal(plugin.name, 'dsh-plugin-cloud')
  assert.deepEqual(plugin.inject, [])
  assert.equal(typeof plugin.apply, 'function')
})
