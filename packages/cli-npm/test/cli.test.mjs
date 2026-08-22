import test from 'node:test'
import assert from 'node:assert/strict'
import { parseArgs } from '../src/cli.mjs'

test('rejects secret values on the command line', () => {
  assert.throws(() => parseArgs(['init', '--upstream-key', 'secret']), /secret values are not accepted/)
})

test('parses safe trial dry-run options', () => {
  assert.deepEqual(parseArgs(['start', '--mode', 'trial', '--dry-run', '--json']), {
    command: 'start', positionals: [], options: { mode: 'trial', dryRun: true, json: true },
  })
})
