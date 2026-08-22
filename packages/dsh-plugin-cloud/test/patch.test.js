import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import YAML from 'yaml'
import { providerRow, pluginRow, stripManagedRows, upsertManagedOps, writeManagedRows } from '../src/patch.js'

const MODELS = [
  { id: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash', contextWindow: 1000000 },
  { id: 'kimi-k3', name: 'Kimi K3' },
]

test('provider row instantiates pi-ai under its own id, never llm-pi-ai', () => {
  const row = providerRow(MODELS)
  assert.equal(row.id, 'dsh-cloud-models')
  assert.notEqual(row.id, 'llm-pi-ai')
  assert.equal(row.name, '@deepseek-ai/dsh-llm-pi-ai')
  const provider = row.config.providers['dsh-cloud']
  assert.equal(provider.api, 'openai-completions')
  assert.equal(provider.apiKeyEnv, 'DSH_CLOUD_TOKEN')
  assert.ok(provider.baseURL.endsWith('/llm/v1'))
  assert.deepEqual(provider.models, MODELS)
})

test('foreign rows and ops pass through untouched', () => {
  const foreign = [
    { insert: [{ id: 'llm-pi-ai', config: { providers: { mine: {} } } }] },
    { insert: [{ id: 'dsh-cloud-models', name: 'old' }, { id: 'their-plugin', name: 'x' }] },
    { remove: ['something'] },
  ]
  const next = upsertManagedOps(foreign, [pluginRow(), providerRow(MODELS)])
  assert.deepEqual(next[0], foreign[0])
  assert.deepEqual(next[1].insert, [{ id: 'their-plugin', name: 'x' }])
  assert.deepEqual(next[2], { remove: ['something'] })
  assert.deepEqual(next[3].insert.map((r) => r.id), ['dsh-plugin-cloud', 'dsh-cloud-models'])
})

test('idempotent: running twice leaves exactly one managed insert', () => {
  const once = upsertManagedOps([], [pluginRow(), providerRow(MODELS)])
  const twice = upsertManagedOps(once, [pluginRow(), providerRow(MODELS)])
  assert.deepEqual(twice, once)
})

test('stripManagedRows drops ops that become empty', () => {
  const ops = [{ insert: [{ id: 'dsh-plugin-cloud' }] }]
  assert.deepEqual(stripManagedRows(ops), [])
})

test('writeManagedRows round-trips and backs up an existing file', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-plugin-cloud-'))
  const path = join(dir, 'cordis.patch.yml')

  const first = writeManagedRows(path, [pluginRow(), providerRow(MODELS)])
  assert.equal(first, undefined)
  const parsed = YAML.parse(readFileSync(path, 'utf8'))
  assert.equal(parsed.length, 1)
  assert.equal(parsed[0].insert[1].config.providers['dsh-cloud'].models.length, 2)

  writeFileSync(path, `# user comment\n${readFileSync(path, 'utf8')}`)
  const backup = writeManagedRows(path, [pluginRow(), providerRow(MODELS.slice(0, 1))])
  assert.ok(backup, 'existing file must be backed up')
  assert.match(readFileSync(backup, 'utf8'), /user comment/)
  const refreshed = YAML.parse(readFileSync(path, 'utf8'))
  assert.equal(refreshed[0].insert[1].config.providers['dsh-cloud'].models.length, 1)
})

test('refuses to rewrite a file that is not a patch list', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-plugin-cloud-'))
  const path = join(dir, 'cordis.patch.yml')
  writeFileSync(path, 'not: a list\n')
  assert.throws(() => writeManagedRows(path, [pluginRow()]), /not a patch list/)
})
