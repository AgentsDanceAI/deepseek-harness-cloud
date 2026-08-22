import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { manifestFromRelease, validateRelease } from '../validate-release.mjs'

test('release source validates and generates the package manifest', async () => {
  const release = JSON.parse(await readFile('release/release.json', 'utf8'))
  assert.deepEqual(validateRelease(release), [])
  assert.equal(manifestFromRelease(release).harnessRuntime, '0.1.0-rc.8')
  assert.equal(manifestFromRelease(release).license, 'LicenseRef-DSH-Cloud-Community-1.0')
  assert.equal(release.desktopRuntime, '0.1.0-rc.6')
})

test('missing or misleading release license metadata is rejected', async () => {
  const release = JSON.parse(await readFile('release/release.json', 'utf8'))
  delete release.license
  assert.match(validateRelease(release).join('\n'), /license/)
  release.license = 'Apache-2.0'
  assert.match(validateRelease(release).join('\n'), /LicenseRef-DSH-Cloud-Community-1\.0/)
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
