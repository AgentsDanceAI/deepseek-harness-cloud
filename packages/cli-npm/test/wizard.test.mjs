import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { applyAnswers, commandPrefix, nextSteps, promptAnswers, shouldRunWizard } from '../src/wizard.mjs'

const parsed = (command, options = {}) => ({ command, options, positionals: [] })

test('wizard runs only for a fresh interactive start or init', () => {
  assert.equal(shouldRunWizard(parsed('start'), { isTTY: true, freshInit: true }), true)
  assert.equal(shouldRunWizard(parsed('init'), { isTTY: true, freshInit: true }), true)
  // 已初始化的目录不能再问 —— 那等于诱导用户覆盖自己的配置
  assert.equal(shouldRunWizard(parsed('start'), { isTTY: true, freshInit: false }), false)
  assert.equal(shouldRunWizard(parsed('up'), { isTTY: true, freshInit: true }), false)
})

test('automation never blocks on a hidden prompt', () => {
  const ctx = { isTTY: true, freshInit: true }
  for (const options of [{ json: true }, { dryRun: true }, { yes: true }]) {
    assert.equal(shouldRunWizard(parsed('start', options), ctx), false)
  }
  // 管道 / CI 里没有 TTY
  assert.equal(shouldRunWizard(parsed('start'), { isTTY: false, freshInit: true }), false)
})

test('answers replace the generated placeholders in place', () => {
  const lines = ['DOMAIN=localhost', 'UPSTREAM_BASE_URL=https://api.deepseek.com/v1', 'UPSTREAM_API_KEY=', 'SEARCH_PROVIDER=zhipu', 'ZHIPU_SEARCH_API_KEY=', '']
  const out = applyAnswers(lines, { upstreamBase: 'https://gw.example.com/v1', upstreamKey: 'sk-x', searchKey: 'zp-y' })
  assert.ok(out.includes('UPSTREAM_BASE_URL=https://gw.example.com/v1'))
  assert.ok(out.includes('UPSTREAM_API_KEY=sk-x'))
  assert.ok(out.includes('ZHIPU_SEARCH_API_KEY=zp-y'))
  assert.equal(out.filter((l) => l.startsWith('UPSTREAM_API_KEY=')).length, 1, '不能重复追加同名键')
  assert.ok(out.includes('DOMAIN=localhost'), '无关行原样保留')
})

test('skipped answers leave the placeholders untouched', () => {
  const lines = ['UPSTREAM_BASE_URL=https://api.deepseek.com/v1', 'UPSTREAM_API_KEY=']
  assert.deepEqual(applyAnswers([...lines], {}), lines)
  assert.deepEqual(applyAnswers([...lines], { upstreamKey: '' }), lines)
})

test('closing panel tells the user where the sign-in code went', () => {
  const panel = nextSteps({ url: 'http://localhost:8787', directory: '/x/dsh-cloud', hasUpstreamKey: true })
  assert.ok(panel.includes('http://localhost:8787'))
  // 2026-08-25 验收: 验证码走日志而页面不说, 用户首次登录必卡 —— 收尾面板必须点破
  assert.ok(panel.includes('dev-mail'))
  // 命令必须从任何目录都能粘: 不依赖 CLI 在 PATH 上, 也不依赖 cwd
  assert.ok(panel.includes('docker logs'), '取码要用 docker, 不能用 dsh-cloud logs')
  assert.ok(panel.includes('--dir /x/dsh-cloud'), 'up/down 必须显式带 --dir')
  assert.ok(!panel.includes('503'), '配好了就不该再警告 503')
  // 装完就走的人从没打开过仓库页, 也就从没被邀请过 —— 给链接, 不代他点
  assert.ok(panel.includes('star') && panel.includes('github.com/AgentsDanceAI/deepseek-harness-cloud'))
})

test('closing panel warns when chat would answer 503', () => {
  const panel = nextSteps({ url: 'http://localhost:8787', directory: '/x/dsh-cloud', hasUpstreamKey: false })
  assert.ok(panel.includes('503') && panel.includes('UPSTREAM_API_KEY'))
})

/** 假 TTY。`atOnce` 把所有行挤进一个 chunk —— 管道输入就长这样,
 *  2026-08-25 真 PTY 实测正是这里把后续问题全吞了。 */
function fakeIo(script, { atOnce = false } = {}) {
  const input = new EventEmitter()
  input.isTTY = true
  input.setRawMode = () => {}
  input.setEncoding = () => {}
  input.resume = () => {}
  input.pause = () => {}
  const written = []
  const output = { write: (text) => written.push(text) }
  queueMicrotask(async () => {
    if (atOnce) {
      input.emit('data', script.map((line) => `${line}\n`).join(''))
      return
    }
    for (const line of script) {
      await new Promise((r) => setTimeout(r, 1))
      input.emit('data', `${line}\r`)
    }
  })
  return { io: { input, output }, written }
}

test('a single chunk carrying every line still answers each question', async () => {
  const { io } = fakeIo(['2', 'https://gw.example.com/v1', 'sk-piped', ''], { atOnce: true })
  const answers = await promptAnswers(io, { version: '0.2.0' })
  assert.equal(answers.upstreamBase, 'https://gw.example.com/v1')
  assert.equal(answers.upstreamKey, 'sk-piped')
  assert.equal(answers.searchKey, '')
})

test('prompts collect a custom endpoint and never echo the key', async () => {
  const { io, written } = fakeIo(['2', 'https://gw.example.com/v1/', 'sk-secret', ''])
  const answers = await promptAnswers(io, { version: '0.2.0' })
  assert.equal(answers.upstreamBase, 'https://gw.example.com/v1', '尾部斜杠要规范化')
  assert.equal(answers.upstreamKey, 'sk-secret')
  assert.equal(answers.searchKey, '')
  const screen = written.join('')
  assert.ok(!screen.includes('sk-secret'), '密钥绝不能出现在屏幕上')
})

test('pressing enter through everything picks the documented defaults', async () => {
  const { io } = fakeIo(['', '', ''])
  const answers = await promptAnswers(io, { version: '0.2.0' })
  assert.equal(answers.upstreamBase, 'https://api.deepseek.com/v1')
  assert.equal(answers.upstreamKey, '')
})

test('stdin closing mid-question falls back to defaults instead of hanging', async () => {
  // Ctrl-D 或 `start < /dev/null`: 2026-08-25 实测原实现会永久挂在问号后面。
  const input = new EventEmitter()
  Object.assign(input, { isTTY: true, setRawMode() {}, setEncoding() {}, resume() {}, pause() {} })
  const io = { input, output: { write() {} } }
  queueMicrotask(() => input.emit('end'))
  const answers = await promptAnswers(io, { version: '0.2.0' })
  assert.equal(answers.upstreamBase, 'https://api.deepseek.com/v1')
  assert.equal(answers.upstreamKey, '')
})

test('trial mode never asks about login', async () => {
  const { io } = fakeIo(['', '', ''], { atOnce: true })
  const answers = await promptAnswers(io, { version: '0.2.0' })
  assert.deepEqual(answers.identity, {}, '试用模式验证码走日志, 问登录纯属噪音')
})

test('selfhost collects SMTP so the first account can exist', async () => {
  // 自部署没有 SMTP/OAuth 就没人能注册, start 会硬拒 —— 引导必须问到
  const { io, written } = fakeIo(['', '', '', '1', 'smtp.example.com', 'bot@example.com', 'pw', ''], { atOnce: true })
  const { identity } = await promptAnswers(io, { version: '0.2.0', mode: 'selfhost' })
  assert.equal(identity.MAIL_SMTP_HOST, 'smtp.example.com')
  assert.equal(identity.MAIL_SMTP_PASS, 'pw')
  assert.equal(identity.MAIL_FROM, 'bot@example.com', '发件地址留空时回落到用户名')
  assert.ok(!written.join('').includes('pw'), 'SMTP 密码不能回显')
})

test('selfhost can pick OAuth instead', async () => {
  const { io } = fakeIo(['', '', '', '2', 'client-id', 'client-secret'], { atOnce: true })
  const { identity } = await promptAnswers(io, { version: '0.2.0', mode: 'selfhost' })
  assert.equal(identity.GITHUB_LOGIN_CLIENT_ID, 'client-id')
  assert.equal(identity.GITHUB_LOGIN_CLIENT_SECRET, 'client-secret')
  assert.equal(identity.MAIL_SMTP_HOST, undefined)
})

test('identity answers land in the env', () => {
  const out = applyAnswers(['MAIL_SMTP_HOST=', 'MAIL_FROM='], {
    identity: { MAIL_SMTP_HOST: 'smtp.example.com', MAIL_FROM: 'bot@example.com' },
  })
  assert.ok(out.includes('MAIL_SMTP_HOST=smtp.example.com'))
  assert.equal(out.filter((l) => l.startsWith('MAIL_SMTP_HOST=')).length, 1)
})

test('printed command matches how the process was actually started', () => {
  // 装了才用裸名字
  assert.equal(commandPrefix({ onPath: true, entry: '/opt/homebrew/bin/dsh-cloud' }), 'dsh-cloud')
  // npx 用完 PATH 上什么都没有 —— 必须印回 npx 形式
  assert.equal(commandPrefix({ onPath: false, entry: '/Users/x/.npm/_npx/abc/node_modules/.bin/dsh-cloud' }),
    'npx --yes @agentsdanceai/dsh-cloud')
  // 从源码跑: 印出那条真的能用的 node 调用
  assert.equal(commandPrefix({ onPath: false, entry: '/repo/packages/cli-npm/bin/dsh-cloud.mjs' }),
    'node /repo/packages/cli-npm/bin/dsh-cloud.mjs')
})

test('panel uses the resolved prefix everywhere', () => {
  const panel = nextSteps({ url: 'u', directory: '/x', hasUpstreamKey: true, prefix: 'npx --yes @agentsdanceai/dsh-cloud' })
  assert.ok(panel.includes('npx --yes @agentsdanceai/dsh-cloud up --dir /x'))
  assert.ok(panel.includes('npx --yes @agentsdanceai/dsh-cloud down --dir /x'))
})

test('selfhost panel names the DNS record the workspace needs', () => {
  // 工作台按 host 路由, 这条记录不加它开着也打不开 —— 装完必须点名
  const panel = nextSteps({ url: 'https://dsh.example.com', directory: '/x', hasUpstreamKey: true, workDomain: 'work.dsh.example.com' })
  assert.ok(panel.includes('work.dsh.example.com') && panel.includes('DNS'))
})

test('trial panel says nothing about DNS', () => {
  const panel = nextSteps({ url: 'http://localhost:8787', directory: '/x', hasUpstreamKey: true })
  assert.ok(!panel.includes('DNS'), '试用模式没有工作台, 提 DNS 是噪音')
})

test('selfhost does not tell you to fish the code out of the log', () => {
  // 自部署配了 SMTP, 验证码真发邮件 —— 那时再说"去日志里捞"就是错的
  const panel = nextSteps({ url: 'https://x', directory: '/d', hasUpstreamKey: true, devMail: false })
  assert.ok(!panel.includes('docker logs') || !panel.includes('dev-mail'))
  assert.ok(panel.includes('你配置的邮件服务器'))
})
