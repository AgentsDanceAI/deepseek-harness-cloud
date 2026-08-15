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
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs'
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
