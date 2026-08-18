/**
 * Post-packaging guard: assert the login-wall assets actually shipped inside
 * the packaged app.
 *
 * Why this exists as its own step: electron-builder's `files` is an explicit
 * ALLOW-LIST, and anything unlisted is dropped SILENTLY — no warning, no error,
 * a green build. Version 2.0.0 shipped exactly that way: `build/cloud/login.html`
 * was present in the assembled tree (so verify-contract passed) but absent from
 * the DMG, so the login window loaded a missing file and every user saw a blank
 * white window. Checking the source tree cannot catch that; only the artifact can.
 *
 *   node desktop/scripts/verify-package.mjs <packaging-output-dir>
 *
 * Passes when each required asset is found either unpacked on disk (asarUnpack
 * copies `build/**` out) or listed in an app.asar header.
 */
import { closeSync, existsSync, openSync, readFileSync, readSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const REQUIRED = ['login.html', 'login-preload.cjs']
const outDir = process.argv[2]
if (!outDir || !existsSync(outDir)) {
  console.error(`verify-package: output dir not found: ${outDir}`)
  process.exit(1)
}

/** Every file path under `dir`, skipping nothing — packaged trees are small enough. */
function* walk(dir) {
  let entries
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) yield* walk(path)
    else if (entry.isFile()) yield path
  }
}

const asars = []
const unpacked = new Set()
for (const path of walk(outDir)) {
  if (path.endsWith('app.asar')) asars.push(path)
  const marker = path.replace(/\\/g, '/')
  if (marker.includes('/build/cloud/')) unpacked.add(marker.split('/build/cloud/')[1])
}

/** Asset names named in any app.asar header (the header is plain JSON text). */
const inAsar = new Set()
for (const asar of asars) {
  const head = readFileSync(asar).subarray(0, Math.min(statSync(asar).size, 1 << 22)).toString('latin1')
  for (const name of REQUIRED) if (head.includes(name)) inAsar.add(name)
}

const missing = REQUIRED.filter(name => !unpacked.has(name) && !inAsar.has(name))
console.log(`verify-package: scanned ${asars.length} asar(s) under ${outDir}`)
console.log(`  unpacked build/cloud: ${[...unpacked].join(', ') || '(none)'}`)
console.log(`  named inside asar:    ${[...inAsar].join(', ') || '(none)'}`)
if (missing.length > 0) {
  console.error('verify-package: FAILED — these login-wall assets did not ship: '
    + missing.join(', '))
  console.error('  electron-builder `files` is an allow-list; ensure build/cloud/** is listed '
    + '(assemble.mjs registers it) and rerun packaging.')
  process.exit(1)
}
console.log('verify-package: login-wall assets present in the packaged app')

// ── 原生二进制平台校验 (2026-08-18 事故后) ────────────────────────────────
// sharp / koffi / ripgrep / node-addon-require-builtin 是按平台分包的 optional
// dependency。yarn 只装宿主平台那份时, Linux 上打出的 mac/Windows 包里塞的是
// Linux ELF —— 构建全绿、装到目标机上一调就崩。已发布的 win x64/arm64 两版全中,
// 而当时的两道检查 (本文件只查登录墙资源、build-win.sh 只查 node-pty) 都没拦住。
//
// 这里按魔数判断每个原生二进制的实际格式, 与产物目录推断出的目标平台比对。
// 只读文件头 4 字节, 不依赖外部 `file` 命令 (CI 镜像未必有)。
const MAGIC = [
  { name: 'ELF',    test: b => b[0] === 0x7f && b[1] === 0x45 && b[2] === 0x4c && b[3] === 0x46, os: 'linux' },
  { name: 'PE',     test: b => b[0] === 0x4d && b[1] === 0x5a,                                    os: 'win32' },
  // Mach-O: 32/64 位、大小端、以及 universal fat 的四种魔数
  { name: 'Mach-O', test: b => { const m = b.readUInt32BE(0)
      return [0xfeedface, 0xfeedfacf, 0xcefaedfe, 0xcffaedfe, 0xcafebabe, 0xbebafeca].includes(m) }, os: 'darwin' },
]
const dirName = outDir.replace(/\\/g, '/').split('/').filter(Boolean).pop() || ''
const targetOs = /^win/.test(dirName) ? 'win32' : /^mac|^darwin/.test(dirName) ? 'darwin' : null

if (targetOs) {
  const foreign = []
  let checked = 0
  for (const path of walk(outDir)) {
    if (!/\.(node|dylib|so)$/.test(path) && !/[\\/](rg|rg\.exe|spawn-helper)$/.test(path)) continue
    let head
    try { const fd = openSync(path, 'r'); head = Buffer.alloc(4)
          readSync(fd, head, 0, 4, 0); closeSync(fd) }
    catch { continue }
    const hit = MAGIC.find(m => { try { return m.test(head) } catch { return false } })
    if (!hit) continue
    checked++
    if (hit.os !== targetOs) foreign.push(`${path.replace(/.*node_modules[\/\\]/, '')} → ${hit.name}`)
  }
  console.log(`verify-package: 原生二进制 ${checked} 个, 目标平台 ${targetOs}`)
  if (foreign.length > 0) {
    console.error(`verify-package: FAILED — ${foreign.length} 个二进制不属于目标平台 ${targetOs}:`)
    for (const f of foreign.slice(0, 12)) console.error('  ' + f)
    console.error('  根因通常是 yarn 只装了宿主平台的 optional 依赖。装配树的 .yarnrc.yml')
    console.error('  需要 supportedArchitectures (assemble.mjs 会写入), 然后重跑 yarn install。')
    process.exit(1)
  }
  console.log('verify-package: 全部原生二进制与目标平台一致')
}
