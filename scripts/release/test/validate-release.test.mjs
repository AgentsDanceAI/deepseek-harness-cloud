import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { manifestFromRelease, validateRelease } from '../validate-release.mjs'

test('release source validates and generates the package manifest', async () => {
  const release = JSON.parse(await readFile('release/release.json', 'utf8'))
  assert.deepEqual(validateRelease(release), [])
  assert.equal(manifestFromRelease(release).harnessRuntime, '0.1.0-rc.8')
  assert.equal(release.desktopRuntime, '0.1.0-rc.6')
})

test('floating base images are rejected', async () => {
  const release = JSON.parse(await readFile('release/release.json', 'utf8'))
  release.baseImages.caddy = 'caddy:2'
  assert.match(validateRelease(release).join('\n'), /baseImages\.caddy/)
})

test('unexpected fields and legacy path drift are rejected', async () => {
  const release = JSON.parse(await readFile('release/release.json', 'utf8'))
  release.unreviewed = true
  release.legacyCompatibility.paths = [...release.legacyCompatibility.paths].reverse()
  const errors = validateRelease(release).join('\n')
  assert.match(errors, /unexpected release field: unreviewed/)
  assert.match(errors, /legacyCompatibility\.paths/)
})
