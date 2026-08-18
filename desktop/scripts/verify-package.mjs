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

// 两道检查都跑完再统一判定 —— 顺序 exit 会让前一道失败时看不到后一道的问题,
// 而它们成因不同 (打包白名单 vs 平台依赖), 一次看全才好定位。
const failures = []
const missing = REQUIRED.filter(name => !unpacked.has(name) && !inAsar.has(name))
console.log(`verify-package: scanned ${asars.length} asar(s) under ${outDir}`)
console.log(`  unpacked build/cloud: ${[...unpacked].join(', ') || '(none)'}`)
console.log(`  named inside asar:    ${[...inAsar].join(', ') || '(none)'}`)
if (missing.length > 0) {
  console.error('verify-package: FAILED — these login-wall assets did not ship: '
    + missing.join(', '))
  console.error('  electron-builder `files` is an allow-list; ensure build/cloud/** is listed '
    + '(assemble.mjs registers it) and rerun packaging.')
  failures.push('登录墙资源缺失')
}
console.log('verify-package: login-wall assets present in the packaged app')

// ── 原生二进制校验 (2026-08-18 两次事故后定稿) ──────────────────────────
// 判据只有一条: **目标平台的原生二进制必须在包里**。
//
// 不判"包里有没有其他平台的二进制": 装了全架构依赖后 node_modules 本就同时存在
// linux/win32/darwin 三套, 它们躺在包里只是多占几十 MB, 运行时按平台选择, 无害。
// 曾试图在 build.files 里裁掉它们, 两次都把目标平台自己那份也一起排掉, 导致包
// 装上去启动即闪退 —— 所以现在不裁剪, 也不为此报错。
//
// 两次事故的真正教训是反过来的:
//   ① 2026-08-17 win 包里 sharp/koffi/ripgrep 是 Linux ELF (yarn 只装了宿主平台
//      那份), 目标平台的二进制根本不存在 → 装到 Windows 上一调就崩;
//   ② 2026-08-18 extglob 取反把目标平台的也排掉 → 包里一个原生模块都没有 → 闪退。
// 两次都是"该有的没有", 都能被下面这条判据抓住。
const NATIVE_REQUIRED = {
  darwin: ['node-pty/prebuilds/darwin', '@vscode/ripgrep-darwin', '@img/sharp-darwin'],
  win32:  ['node-pty/prebuilds/win32',  '@vscode/ripgrep-win32',  '@img/sharp-win32'],
}
const dirName = outDir.replace(/\\/g, '/').split('/').filter(Boolean).pop() || ''
// 目标平台从**产物结构**判断, 不靠目录名。目录名 (mac-arm64 / win-unpacked) 是
// electron-builder 的默认命名, 一旦谁改了输出目录或拷到别处, 靠名字推断就会静默
// 跳过整段检查 —— 2026-08-18 写反向测试时就撞上了 (拷到 /tmp/ep 后检查不执行)。
// .app 目录是 macOS 独有, .exe 主程序是 Windows 独有, 这两个信号不会因改名而变。
const allPaths = [...walk(outDir)].map(p => p.replace(/\\/g, '/'))
const targetOs = allPaths.some(p => p.includes('.app/Contents/'))  ? 'darwin'
               : allPaths.some(p => /\/[^/]+\.exe$/i.test(p))      ? 'win32'
               : null

if (targetOs) {
  const missing = NATIVE_REQUIRED[targetOs].filter(frag => !allPaths.some(p => p.includes(frag)))
  console.log(`verify-package: 目标平台 ${targetOs}, 检查 ${NATIVE_REQUIRED[targetOs].length} 组原生模块`)
  if (missing.length > 0) {
    console.error(`verify-package: FAILED — 目标平台 ${targetOs} 的原生模块缺失:`)
    for (const m of missing) console.error('  ' + m)
    console.error('  这样的包装到目标系统上会崩溃或功能失效。两个已知成因:')
    console.error('   · yarn 只装了宿主平台的 optional 依赖 → 装配树 .yarnrc.yml 需要')
    console.error('     supportedArchitectures (assemble.mjs 会写入), 然后重跑 yarn install;')
    console.error('   · build.files 的排除规则误伤了目标平台自己那份 (别用 extglob 取反)。')
    failures.push(`目标平台 ${targetOs} 原生模块缺失: ${missing.join(', ')}`)
  }
  console.log('verify-package: 目标平台原生模块齐全')
}

if (failures.length > 0) {
  console.error(`\nverify-package: FAILED — ${failures.length} 项:`)
  for (const f of failures) console.error('  · ' + f)
  process.exit(1)
}
