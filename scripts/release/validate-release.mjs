#!/usr/bin/env node
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

const SEMVER = /^\d+\.\d+\.\d+$/
const RUNTIME = /^\d+\.\d+\.\d+-rc\.\d+$/
const DIGEST_REF = /^[^@\s]+@sha256:[0-9a-f]{64}$/
const BASE_NAMES = ['python', 'uv', 'node', 'caddy', 'postgres', 'socketProxy']
const RELEASE_FIELDS = ['$schema', 'version', 'stackSchema', 'databaseSchema', 'minCliVersion', 'minUpgradeFrom', 'legacyCompatibility', 'harnessRuntime', 'desktopRuntime', 'productImages', 'baseImages']

function rejectUnexpected(errors, value, allowed, prefix) {
  for (const name of Object.keys(value ?? {})) {
    if (!allowed.includes(name)) errors.push(`unexpected ${prefix} field: ${name}`)
  }
}

export function validateRelease(value) {
  const errors = []
  rejectUnexpected(errors, value, RELEASE_FIELDS, 'release')
  for (const name of ['version', 'minCliVersion', 'minUpgradeFrom']) {
    if (!SEMVER.test(value?.[name] ?? '')) errors.push(`${name} must be stable SemVer`)
  }
  if (!RUNTIME.test(value?.harnessRuntime ?? '')) errors.push('harnessRuntime must be x.y.z-rc.N')
  if (!RUNTIME.test(value?.desktopRuntime ?? '')) errors.push('desktopRuntime must be x.y.z-rc.N')
  for (const name of ['stackSchema', 'databaseSchema']) {
    if (!Number.isInteger(value?.[name]) || value[name] < 1) errors.push(`${name} must be a positive integer`)
  }
  for (const name of BASE_NAMES) {
    if (!DIGEST_REF.test(value?.baseImages?.[name] ?? '')) errors.push(`baseImages.${name} must contain an immutable @sha256 digest`)
  }
  rejectUnexpected(errors, value?.baseImages, BASE_NAMES, 'baseImages')
  rejectUnexpected(errors, value?.productImages, ['server', 'workspace'], 'productImages')
  rejectUnexpected(errors, value?.legacyCompatibility, ['introduced', 'supportedThrough', 'paths'], 'legacyCompatibility')
  const expectedTag = `:${value?.version ?? ''}`
  for (const name of ['server', 'workspace']) {
    if (!(value?.productImages?.[name] ?? '').endsWith(expectedTag)) errors.push(`productImages.${name} must use the release version tag`)
  }
  if (value?.legacyCompatibility?.supportedThrough !== '0.4.0') errors.push('legacyCompatibility.supportedThrough must remain 0.4.0')
  if (!SEMVER.test(value?.legacyCompatibility?.introduced ?? '')) errors.push('legacyCompatibility.introduced must be stable SemVer')
  if (JSON.stringify(value?.legacyCompatibility?.paths) !== JSON.stringify(['scripts/quickstart.sh', 'deploy/docker-compose.yml'])) {
    errors.push('legacyCompatibility.paths must preserve the two approved paths in order')
  }
  return errors
}

export function manifestFromRelease(value) {
  const errors = validateRelease(value)
  if (errors.length) throw new Error(errors.join('\n'))
  return {
    schemaVersion: 1,
    version: value.version,
    stackSchema: value.stackSchema,
    databaseSchema: value.databaseSchema,
    minCliVersion: value.minCliVersion,
    minUpgradeFrom: value.minUpgradeFrom,
    harnessRuntime: value.harnessRuntime,
    images: value.productImages,
    baseImages: value.baseImages,
  }
}

async function main(argv) {
  if (argv.includes('--self-test-floating')) {
    const release = JSON.parse(await readFile('release/release.json', 'utf8'))
    release.baseImages.python = 'python:3.11-slim-bookworm'
    if (!validateRelease(release).some(error => error.includes('baseImages.python'))) throw new Error('validator accepted mutable image reference')
    process.stdout.write('rejected mutable image reference\n')
    return
  }
  const release = JSON.parse(await readFile('release/release.json', 'utf8'))
  const errors = validateRelease(release)
  if (errors.length) throw new Error(errors.join('\n'))
  process.stdout.write(`${release.version}\n`)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).catch(error => {
    process.stderr.write(`${error.message}\n`)
    process.exitCode = 1
  })
}
