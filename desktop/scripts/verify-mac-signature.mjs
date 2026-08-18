#!/usr/bin/env node
/**
 * 校验 .app 里每个 Mach-O 可执行体都开了 hardened runtime —— 公证的硬性要求。
 *
 *   node desktop/scripts/verify-mac-signature.mjs "<path>/DSH Cloud Desktop.app"
 *
 * 为什么要有这个: 公证一轮 3-5 分钟, 失败只回一句 "The executable does not have
 * the hardened runtime enabled" 加一个路径。2026-08-18 用公证当测试循环跑了五轮,
 * 每轮五分钟, 才定位到问题 —— 而同样的信息在本地解析 Mach-O 几秒就能拿到, 还能
 * 一次列出**所有**没开的文件而不是 Apple 挑出来的头几个。
 *
 * 直接读 CodeDirectory 的 flags 位 (0x10000 = CS_RUNTIME), 不依赖 codesign
 * (macOS 独有) 也不依赖 rcodesign 的输出格式。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'

const app = process.argv[2]
if (!app) { console.error('用法: verify-mac-signature.mjs <path/to/App.app>'); process.exit(2) }

const CS_RUNTIME = 0x10000

/** CodeDirectory flags, 或 'NOSIG' (无签名) / null (不是 Mach-O)。 */
function codeSignatureFlags(path) {
  const d = readFileSync(path)
  if (d.length < 32) return null
  let off = 0
  if (d.readUInt32BE(0) === 0xcafebabe) off = d.readUInt32BE(16)   // fat: 取第一个架构
  const magic = d.readUInt32LE(off)
  if (magic !== 0xfeedfacf && magic !== 0xfeedface) return null
  const is64 = magic === 0xfeedfacf
  const ncmds = d.readUInt32LE(off + 16)
  let pos = off + (is64 ? 32 : 28)
  for (let i = 0; i < ncmds; i++) {
    const cmd = d.readUInt32LE(pos), sz = d.readUInt32LE(pos + 4)
    if (cmd === 0x1d) {                                            // LC_CODE_SIGNATURE
      const so = d.readUInt32LE(pos + 8)
      const sup = d.subarray(off + so)
      const cnt = sup.readUInt32BE(8)
      for (let j = 0; j < cnt; j++) {
        const type = sup.readUInt32BE(12 + j * 8), o = sup.readUInt32BE(16 + j * 8)
        if (type === 0) return sup.readUInt32BE(o + 12)             // CSSLOT_CODEDIRECTORY
      }
      return 'NOCD'
    }
    pos += sz
  }
  return 'NOSIG'
}

function* walk(dir) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name)
    if (e.isSymbolicLink()) continue
    if (e.isDirectory()) yield* walk(p)
    else yield p
  }
}

const bad = []
let ok = 0
for (const p of walk(app)) {
  if (/\.(dylib|node)$/.test(p)) continue          // 库不是可执行体, 公证不要求
  let type
  try { type = execFileSync('file', ['-b', p], { encoding: 'utf8' }) } catch { continue }
  if (!/Mach-O/.test(type) || !/executable/.test(type)) continue
  const f = codeSignatureFlags(p)
  const rel = p.slice(app.length + 1)
  if (typeof f === 'number' && (f & CS_RUNTIME)) ok++
  else bad.push([rel, typeof f === 'number' ? '0x' + f.toString(16) : String(f)])
}

console.log(`    hardened runtime: ${ok} 个已开, ${bad.length} 个未开`)

// ── Helper 的 entitlements (2026-08-18 用户实测崩溃后加) ────────────────────
// 光有 hardened runtime 不够。Electron 的渲染进程跑在 Helper (Renderer).app 里,
// 开了 runtime 却没有 com.apple.security.cs.allow-jit 的话, V8 申请不到 JIT 内存,
// 报 "Failed to reserve virtual memory for CodeRange" 后渲染进程死, 应用启动即退。
// 而**公证照样通过** —— Apple 只校验 runtime 标志, 不校验 entitlements 够不够用。
// rcodesign 的 entitlements 默认只作用于主实体, 每个 Helper 必须 scoped 再给一遍。
const helpers = readdirSync(join(app, 'Contents', 'Frameworks'), { withFileTypes: true })
  .filter(e => e.isDirectory() && e.name.endsWith('.app'))
  .map(e => join(app, 'Contents', 'Frameworks', e.name))
const noJit = []
for (const h of helpers) {
  const exeDir = join(h, 'Contents', 'MacOS')
  let exe
  try { exe = join(exeDir, readdirSync(exeDir)[0]) } catch { continue }
  // entitlements 以明文 XML 嵌在签名超级块里, 直接找特征串即可
  const blob = readFileSync(exe).toString('latin1')
  if (!blob.includes('com.apple.security.cs.allow-jit')) noJit.push(h.slice(app.length + 1))
}
console.log(`    Helper entitlements: ${helpers.length - noJit.length}/${helpers.length} 个带 allow-jit`)
if (noJit.length > 0) {
  console.error('verify-mac-signature: FAILED — 以下 Helper 缺 allow-jit, 应用会启动即闪退:')
  for (const h of noJit) console.error('  ' + h)
  console.error('  修法: rcodesign sign 对每个 Helper 加 --entitlements-xml-file "<相对路径>:<plist>"')
  console.error('  (entitlements 默认只作用于主实体, --for-notarization 不覆盖这一项)')
  process.exit(1)
}
if (bad.length > 0) {
  console.error('verify-mac-signature: FAILED — 以下可执行体没开 hardened runtime, 公证必被拒:')
  for (const [rel, f] of bad) console.error(`  ${rel}  flags=${f}`)
  console.error('  修法: rcodesign sign 加 --for-notarization (对所有 Mach-O 统一开 runtime),')
  console.error('  不要手工用 "<路径>:runtime" 逐个指定 —— 含 @ 的路径写不进 scope。')
  process.exit(1)
}
