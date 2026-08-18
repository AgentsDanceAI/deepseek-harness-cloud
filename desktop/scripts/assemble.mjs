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

// 4d. 跨平台打包必需: sharp / koffi / ripgrep / node-addon-require-builtin 都是
// **按平台分包**的 optional dependency。yarn 默认只装宿主平台那一份, 于是在
// Linux 上打出来的 mac/Windows 包里塞的是 Linux ELF —— 构建全绿、装到目标系统
// 上一调就崩 (2026-08-17 实测: 已发布的 win x64/arm64 两个包全中)。
// electron-builder 对此只打一行 "missing optional dependencies" 的普通提示,
// 不算错误。这里把目标架构写进装配树的 .yarnrc.yml, 让 yarn 把各平台二进制
// 都下下来。写在这里而不是手改文件: assemble 每次 rmSync 整棵树重建, 手改活不过一轮。
{
  const yarnrc = join(dest, '.yarnrc.yml')
  const existing = existsSync(yarnrc) ? readFileSync(yarnrc, 'utf8') : ''
  if (!existing.includes('supportedArchitectures')) {
    writeFileSync(yarnrc, existing.replace(/\n*$/, '\n') +
      '\n# 由 assemble.mjs 写入 — 跨平台打包必需, 详见本文件 4d 段\n' +
      'supportedArchitectures:\n  os:\n    - darwin\n    - linux\n    - win32\n' +
      '  cpu:\n    - x64\n    - arm64\n')
    console.log('assemble: registered supportedArchitectures in .yarnrc.yml')
  }
}

// 4e. (2026-08-18) 曾在 build.files 里做"只打目标平台原生变体"的裁剪, 已回退。
//
// 动机是合理的: 装了全架构依赖 (4d) 后 node_modules 里同时存在 linux/win32/darwin
// 三套 sharp/koffi/ripgrep/node-pty, 每个安装包因此多背 60-125MB。
// 但两次尝试都出了事故, 且都不是"配置写错"而是 electron-builder 的行为不透明:
//   · "!node_modules/@vscode/ripgrep-!(${os})*/**" (extglob 取反) 会把**目标平台
//     自己那份也排掉** → 包里原生模块全空 → 应用启动即闪退;
//   · 想按 ${os} 条件展开生成三条独立规则, 又依赖模板求值的具体语义, 同样在猜。
//
// 多背几十 MB 是可承受的; 发一个装上去打不开的包不是。所以这里**不做任何裁剪**,
// 由 verify-package.mjs 保证"包里目标平台的二进制齐全且无外来平台二进制"这条底线。
// 将来若要重做裁剪, 必须先在真机上验证包能启动, 再看体积。

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
