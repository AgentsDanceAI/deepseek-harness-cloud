#!/usr/bin/env node
/** Assemble the DSH Cloud desktop tree from the pinned upstream + our overlay.
 *
 *   node desktop/scripts/assemble.mjs [--dest <dir>] [--no-submodule]
 *
 * Steps: clone upstream desktop at the pinned commit -> apply patches/ ->
 * copy dsh-plugin-cloud sources and assets in -> sanity checks. The result is
 * a normal deepseek-harness-desktop working tree; build it with its own
 * commands (corepack yarn install / yarn build / yarn package:dir).
 */
import { execFileSync } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const desktopDir = resolve(here, '..')
const upstream = JSON.parse(readFileSync(join(desktopDir, 'upstream.json'), 'utf8'))

const args = process.argv.slice(2)
const flag = name => args.includes(name)
const opt = (name, fallback) => {
  const index = args.indexOf(name)
  return index >= 0 && args[index + 1] !== undefined ? args[index + 1] : fallback
}
const dest = resolve(opt('--dest', join(desktopDir, 'build', 'upstream')))
/** Packaging glob for the login-wall assets; asserted by verify-contract. */
export const CLOUD_ASSET_GLOB = 'build/cloud/**'
export const WIN_ARCHES = ['x64', 'arm64']

const run = (command, cmdArgs, cwd) => execFileSync(command, cmdArgs, { cwd, stdio: 'inherit' })
const capture = (command, cmdArgs, cwd) =>
  execFileSync(command, cmdArgs, { cwd, encoding: 'utf8' }).trim()

console.log(`assemble: upstream desktop @ ${upstream.desktopCommit.slice(0, 10)} -> ${dest}`)

// 1. fresh checkout at the pin
if (existsSync(dest)) rmSync(dest, { recursive: true, force: true })
mkdirSync(dirname(dest), { recursive: true })
run('git', ['clone', '--no-checkout', upstream.desktopRepository, dest])
run('git', ['checkout', '--quiet', upstream.desktopCommit], dest)

// 2. upstream dsh submodule (verify-layout needs it; skip for quick iterations)
if (!flag('--no-submodule')) {
  run('git', ['submodule', 'update', '--init', '--depth', '1', '--', 'deepseek-harness'], dest)
  const submoduleHead = capture('git', ['rev-parse', 'HEAD'], join(dest, 'deepseek-harness'))
  if (submoduleHead !== upstream.harnessCommit) {
    console.warn(`assemble: WARNING submodule HEAD ${submoduleHead.slice(0, 10)} differs from `
      + `upstream.json harnessCommit ${upstream.harnessCommit.slice(0, 10)} — update upstream.json`)
  }
} else {
  console.log('assemble: skipping deepseek-harness submodule (--no-submodule); yarn check will fail verify-layout')
}

// 3. apply our patches in order
const patchDir = join(desktopDir, 'patches')
const patches = readdirSync(patchDir).filter(name => name.endsWith('.patch')).sort()
for (const patch of patches) {
  const path = join(patchDir, patch)
  run('git', ['apply', '--check', path], dest)
  run('git', ['apply', path], dest)
  console.log(`assemble: applied ${patch}`)
}

// 4. copy the cloud plugin in (sources compile with main.ts; assets ship in build/)
cpSync(join(desktopDir, 'dsh-plugin-cloud', 'src'),
  join(dest, 'dsh-plugin-desktop', 'src', 'cloud'), { recursive: true })
cpSync(join(desktopDir, 'dsh-plugin-cloud', 'assets'),
  join(dest, 'dsh-plugin-desktop', 'build', 'cloud'), { recursive: true })
console.log('assemble: copied dsh-plugin-cloud sources and assets')

// 4b. electron-builder's `files` is an explicit ALLOW-LIST: upstream names its
// icons under build/ one by one, so copying assets into build/cloud/ is not
// enough — anything unlisted is silently dropped at packaging time. That is
// exactly what shipped 2.0.0 with no login.html: the login window loaded a
// missing file and rendered a blank white page. Registered programmatically
// rather than as a patch so an upstream edit to package.json cannot drop it.
{
  const manifestPath = join(dest, 'dsh-plugin-desktop', 'package.json')
  const pkg = JSON.parse(readFileSync(manifestPath, 'utf8'))
  const files = pkg.build?.files
  if (!Array.isArray(files)) {
    throw new Error('assemble: upstream build.files is not an array — the cloud '
      + 'assets can no longer be registered; re-check the packaging contract')
  }
  if (!files.includes(CLOUD_ASSET_GLOB)) {
    files.push(CLOUD_ASSET_GLOB)
    writeFileSync(manifestPath, `${JSON.stringify(pkg, null, 2)}\n`)
  }
  console.log(`assemble: registered ${CLOUD_ASSET_GLOB} in build.files`)
}

// 4c. Windows arm64. Upstream targets x64 only, which is correct for them but
// leaves every Surface / Snapdragon laptop running our x64 build under emulation
// — slower, and it cannot load native modules built for arm64. Registered here
// for the same reason as build.files above: an upstream edit to package.json
// must not be able to silently drop it.
{
  const manifestPath = join(dest, 'dsh-plugin-desktop', 'package.json')
  const pkg = JSON.parse(readFileSync(manifestPath, 'utf8'))
  const targets = pkg.build?.win?.target
  if (!Array.isArray(targets)) {
    throw new Error('assemble: upstream build.win.target is not an array — the '
      + 'arm64 target can no longer be registered; re-check the packaging contract')
  }
  let changed = false
  for (const entry of targets) {
    if (entry?.target !== 'nsis' || !Array.isArray(entry.arch)) continue
    for (const arch of WIN_ARCHES) {
      if (!entry.arch.includes(arch)) {
        entry.arch.push(arch)
        changed = true
      }
    }
  }
  if (changed) writeFileSync(manifestPath, `${JSON.stringify(pkg, null, 2)}\n`)
  console.log(`assemble: windows targets ${JSON.stringify(targets)}`)
}

// 5. redistribution guard: the identity-scoped @anthropic-ai/claude-agent-sdk
// authorization does not extend to us; the desktop tree must not depend on it.
const manifest = JSON.parse(readFileSync(join(dest, 'dsh-plugin-desktop', 'package.json'), 'utf8'))
for (const field of ['dependencies', 'devDependencies', 'optionalDependencies']) {
  for (const name of Object.keys(manifest[field] ?? {})) {
    if (name.includes('subagent-claude-code') || name.includes('claude-agent-sdk')) {
      throw new Error(`assemble: ${field}.${name} must not ship in DSH Cloud Desktop `
        + '(identity-scoped redistribution authorization); remove it before packaging')
    }
  }
}

// 6. runtime version pin still consistent?
const pinned = JSON.parse(readFileSync(join(dest, 'upstream.json'), 'utf8'))
if (pinned.runtimePackageVersion !== upstream.runtimePackageVersion) {
  console.warn(`assemble: WARNING upstream runtimePackageVersion ${pinned.runtimePackageVersion} `
    + `differs from ours (${upstream.runtimePackageVersion}) — update desktop/upstream.json`)
}

console.log(`
assemble: done. Next steps (network + yarn 4 via corepack required):
  cd ${dest}
  corepack enable && yarn install
  node ${join(desktopDir, 'scripts', 'verify-contract.mjs')} ${dest}
  cd dsh-plugin-desktop && yarn build && yarn package:dir   # or dist:mac / dist:win
`)
