#!/usr/bin/env node
/** Assert the upstream contract points DSH Cloud depends on, after yarn install.
 *
 *   node desktop/scripts/verify-contract.mjs <assembled-tree>
 *
 * Run on every upstream bump. A failure here means dsh moved one of our seams
 * and desktop/dsh-plugin-cloud (or a patch) needs a matching change BEFORE the
 * bump ships — fail loud beats silently routing traffic to the official API.
 */
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const tree = resolve(process.argv[2] ?? '.')
const plugin = join(tree, 'dsh-plugin-desktop')
const modules = join(plugin, 'node_modules')
const failures = []
const check = (ok, message) => { if (!ok) failures.push(message) }

if (!existsSync(modules)) {
  console.error(`verify-contract: ${modules} missing — run yarn install first`)
  process.exit(2)
}

// 1. the Cordis rows we patch must still exist in the base bundle
const basePatch = readFileSync(join(modules, '@deepseek-ai', 'dsh-base', 'cordis.patch.yml'), 'utf8')
// llm-pi-ai 是 2026-08-20 起新增的依赖: 网关的整份目录靠它以 hand-declared
// 路由暴露, 同时我们**禁用** llm-deepseek 以免同一个模型在选择器里出现两次。
// 这两件事是一对 —— 若上游改掉 pi-ai 的 row id, 注入静默失效而禁用照旧生效,
// 结果是一个模型都不剩, 而无可用模型时上游会禁用输入框 (2026-08-19 那次死锁)。
// 所以这一行必须在构建期就拦住, 不能等用户装上才发现。
for (const rowId of ['llm-deepseek', 'web-search-deepseek', 'session-telemetry-otel', 'llm-pi-ai']) {
  check(basePatch.includes(`id: ${rowId}`), `base bundle lost row id '${rowId}'`)
}

// 2. the adapter config fields we set must still exist
for (const [pkg, fields] of [
  ['dsh-llm-deepseek', ['baseURL', 'apiKeyEnv']],
  ['dsh-web-search-deepseek', ['baseURL', 'apiKeyEnv']],
  // hand-declared 路由要自己给全端点/协议/模型 —— 少任何一个字段, 注入的
  // provider 都会瘸着上线 (可能列不出模型, 或整条路由被上游忽略)。
  ['dsh-llm-pi-ai', ['providers', 'apiKeyEnv', 'baseURL', 'models']],
]) {
  const dir = join(modules, '@deepseek-ai', pkg)
  check(existsSync(dir), `package @deepseek-ai/${pkg} is gone`)
  if (!existsSync(dir)) continue
  let source = ''
  for (const candidate of ['lib/index.js', 'lib/index.cjs', 'src/index.ts']) {
    const path = join(dir, candidate)
    if (existsSync(path)) { source += readFileSync(path, 'utf8') }
  }
  for (const field of fields) {
    check(source.includes(field), `@deepseek-ai/${pkg} no longer mentions config field '${field}'`)
  }
}

// 3. runtime family version matches our pin
const ours = JSON.parse(readFileSync(join(tree, '..', '..', 'upstream.json'), 'utf8'))
const manifest = JSON.parse(readFileSync(join(plugin, 'package.json'), 'utf8'))
for (const [name, range] of Object.entries(manifest.dependencies ?? {})) {
  if (name === '@deepseek-ai/dsh' || name.startsWith('@deepseek-ai/dsh-')) {
    check(range === ours.runtimePackageVersion,
      `${name}@${range} != pinned runtime family ${ours.runtimePackageVersion} (update desktop/upstream.json)`)
  }
}

// 4. redistribution guard: identity-scoped Claude packages must be absent
for (const banned of [
  join(modules, '@deepseek-ai', 'dsh-subagent-claude-code'),
  join(modules, '@anthropic-ai', 'claude-agent-sdk'),
]) {
  check(!existsSync(banned), `${banned} is present — not licensed for our redistribution`)
}

// 5. our overlay landed
check(existsSync(join(plugin, 'src', 'cloud', 'index.ts')), 'src/cloud overlay missing — run assemble.mjs')
check(existsSync(join(plugin, 'build', 'cloud', 'login.html')), 'build/cloud assets missing — run assemble.mjs')
// Present on disk is not enough: electron-builder's `files` is an allow-list,
// and an unlisted path is dropped SILENTLY at packaging time. 2.0.0 shipped
// that way — the login window loaded a missing file and showed a blank page.
check((manifest.build?.files ?? []).includes('build/cloud/**'),
  'build.files does not list build/cloud/** — the login assets would be dropped at packaging')
check(readFileSync(join(plugin, 'src', 'main.ts'), 'utf8').includes('cloudGate'),
  'main.ts lacks the cloudGate call — 0003 patch not applied')

if (failures.length > 0) {
  console.error('verify-contract: FAILED')
  for (const failure of failures) console.error(`  - ${failure}`)
  process.exit(1)
}
console.log('verify-contract: all upstream contract points intact')
