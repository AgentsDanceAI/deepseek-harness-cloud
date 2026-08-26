/** First-run setup wizard.
 *
 * `dsh-cloud start` on an empty directory used to write an .env with an empty
 * UPSTREAM_API_KEY, boot the stack, and print a URL — leaving the operator to
 * discover on their own that chat answers 503 until they hand-edit a file and
 * start over. This module turns that silent gap into three questions.
 *
 * Two hard rules, both inherited from the CLI's existing stance:
 *  - secrets are read from the TTY with echo off, never from argv (they would
 *    otherwise land in shell history and in `ps` output for every local user);
 *  - a non-interactive run (CI, --json, --yes, no TTY) skips every prompt and
 *    behaves exactly as before, so automation never blocks on a hidden prompt.
 */

const DEEPSEEK_BASE = 'https://api.deepseek.com/v1'
const REPOSITORY = 'https://github.com/AgentsDanceAI/deepseek-harness-cloud'

/** How to spell "run me again" for the way this process was actually started.
 *
 * Printing a bare `dsh-cloud …` is wrong for almost everyone: `npx` leaves
 * nothing on PATH, and running from a checkout never put it there either. So
 * resolve it from facts — is the name really on PATH, and what did argv say —
 * rather than assuming the happy `npm install -g` case. Pure: the caller
 * supplies both facts so this is testable without touching the filesystem.
 */
export function commandPrefix({ resolved = '', entry = '' }) {
  // npx 在**运行期间**把自己的 node_modules/.bin 塞进 PATH, 所以"dsh-cloud 在
  // PATH 上"当场为真、进程一退就没了 —— 2026-08-26 实测: 面板印了裸 dsh-cloud,
  // 老板照着敲得到 command not found。落在 npx 缓存里的那份不算数。
  const fromNpx = (path) => path.includes('/_npx/') || path.includes('\\_npx\\')
  if (resolved && !fromNpx(resolved)) return 'dsh-cloud'
  if (fromNpx(resolved) || fromNpx(entry)) return 'npx --yes @agentsdanceai/dsh-cloud'
  return entry ? `node ${entry}` : 'dsh-cloud'
}

/** Should the interactive wizard run for this invocation? */
export function shouldRunWizard(parsed, { isTTY, freshInit }) {
  if (!freshInit || !isTTY) return false
  if (parsed.options.json || parsed.options.dryRun || parsed.options.yes) return false
  return parsed.command === 'start' || parsed.command === 'init'
}

/** Merge wizard answers into the generated .env lines. Pure. */
export function applyAnswers(lines, answers = {}) {
  const set = (key, value) => {
    const index = lines.findIndex((line) => line.startsWith(`${key}=`))
    const next = `${key}=${value}`
    if (index === -1) lines.push(next)
    else lines[index] = next
  }
  if (answers.upstreamBase) set('UPSTREAM_BASE_URL', answers.upstreamBase)
  if (answers.upstreamKey) set('UPSTREAM_API_KEY', answers.upstreamKey)
  if (answers.searchKey) {
    set('SEARCH_PROVIDER', 'zhipu')
    set('ZHIPU_SEARCH_API_KEY', answers.searchKey)
  }
  for (const [key, value] of Object.entries(answers.identity ?? {})) {
    if (value) set(key, value)
  }
  return lines
}

/** The closing panel. Pure so its content is testable. */
export function nextSteps({ url, directory, hasUpstreamKey, projectName = 'dsh-selfhost', prefix = 'dsh-cloud', workDomain = '', devMail = true }) {
  // 命令必须从任何目录粘过去都能跑。取日志用 docker 而不是 `dsh-cloud logs`:
  // 后者既要求 CLI 在 PATH 上 (npx 用完就没了), 又依赖当前目录是部署目录的
  // 上一级 —— 两个前提对刚装完的人都不成立。up/down 显式带 --dir 同理。
  const lines = [
    '',
    '  云工作台已就绪',
    '',
    `  打开    ${url}`,
    // 自部署配了 SMTP, 验证码是真发邮件的 —— 那时再说"去日志里捞"就是错的。
    ...devMail
      ? [
          '  登录    用任意邮箱收验证码即可注册；本部署没有配邮件服务器，',
          '          验证码打印在服务端日志里：',
          `            docker logs ${projectName}-dhc-server-1 2>&1 | grep -A1 dev-mail`,
        ]
      : ['  登录    用任意邮箱收验证码即可注册，验证码走你配置的邮件服务器。'],
    '',
  ]
  if (!hasUpstreamKey) {
    lines.push(
      '  注意    还没有配模型上游，聊天会返回 503。把 UPSTREAM_API_KEY 填进',
      `          ${directory}/.env 后执行 dsh-cloud up 生效。`,
      '',
    )
  }
  if (workDomain) {
    // 工作台按 host 路由, 这条 DNS 记录不加的话它开着也访问不到 —— 装完必须
    // 说, 否则用户只会看到一个打不开的地址而无从判断缺了什么。
    lines.push(
      `  还差一步  给 ${workDomain} 加一条指向本机的 DNS 记录（云工作台按域名路由）`,
      '',
    )
  }
  lines.push(
    `  配置    ${directory}/.env`,
    `  重启    ${prefix} up --dir ${directory}`,
    `  停止    ${prefix} down --dir ${directory}（数据保留）`,
    '',
    // 装完就走的人从没打开过仓库页 —— 14 天里 82 个克隆者对 3 个 star, 差距
    // 全在"没被邀请过"。给个链接让他自己点, 绝不代他操作账号。
    `  觉得有用就给个 star：${REPOSITORY}`,
    '',
  )
  return lines.join('\n')
}

/** A line reader that owns stdin for the whole wizard.
 *
 * One persistent listener plus a buffer, because a chunk can carry several
 * lines at once (anything piped, and fast typists) — reading a chunk as a
 * single answer swallowed the remaining questions. `secret()` flips the TTY to
 * raw for the duration of one answer so the key is never echoed, and restores
 * the previous mode afterwards.
 */
/** `init` 的收尾摘要。
 *
 * init 只写配置、不起容器, 所以不能用 nextSteps (那张面板说的是"已就绪")。
 * 它原本无论如何都吐一整坨 JSON —— 那是给脚本解析的, 而人在终端前只想知道
 * "写到哪了、下一步敲什么"。带 --json 或非 TTY 时仍然只吐 JSON, 自动化不受影响。
 */
export function initSummary({ directory, prefix = 'dsh-cloud', mode = 'trial', workDomain = '' }) {
  const lines = [
    '',
    '  配置已写入，容器还没起',
    '',
    `  目录    ${directory}`,
    `  启动    ${prefix} up --dir ${directory}`,
    '',
  ]
  if (mode === 'selfhost') {
    lines.push(
      '  这是对外服务的配置：绑 0.0.0.0、占用 80/443、申请真证书，',
      '  请在目标服务器上启动，而不是本机。',
      '',
    )
    if (workDomain) {
      lines.push(`  别忘了给 ${workDomain} 加一条指向该服务器的 DNS 记录（云工作台按域名路由）`, '')
    }
  }
  lines.push(`  启动前可以先过一遍 ${directory}/.env`, '')
  return lines.join('\n')
}

export function createReader(input, output) {
  let buffer = ''
  let waiting = null
  input.setEncoding('utf8')
  input.resume()

  const deliver = () => {
    if (!waiting) return
    const match = buffer.match(/\r\n|\r|\n/)
    if (!match) return
    const line = buffer.slice(0, match.index)
    buffer = buffer.slice(match.index + match[0].length)
    const { resolve, restore } = waiting
    waiting = null
    restore()
    output.write('\n')
    resolve(line.trim())
  }

  // stdin 关掉时 (Ctrl-D, 或 `start < /dev/null`) 不能干等 —— 未答的问题
  // 一律取默认值继续, 否则进程永久挂起而屏幕上只停在一个问号后面。
  let ended = false
  input.on('end', () => {
    ended = true
    if (!waiting) return
    const { resolve, restore } = waiting
    waiting = null
    restore()
    output.write('\n')
    resolve('')
  })

  input.on('data', (chunk) => {
    const text = String(chunk)
    // Ctrl-C in raw mode does not reach the shell; honour it ourselves.
    if (waiting?.raw && text.includes('\u0003')) {
      waiting.restore()
      output.write('\n')
      process.exit(130)
    }
    buffer += waiting?.raw ? text.replace(/[\u007f\b]/g, '') : text
    deliver()
  })

  const readLine = (question, raw) => new Promise((resolve) => {
    output.write(question)
    let restore = () => {}
    if (raw && input.isTTY && input.setRawMode) {
      const wasRaw = Boolean(input.isRaw)
      input.setRawMode(true)
      restore = () => input.setRawMode(wasRaw)
    }
    waiting = { resolve, restore, raw }
    deliver()  // the answer may already be sitting in the buffer
    if (waiting && ended) {
      waiting = null
      restore()
      resolve('')
    }
  })

  return {
    ask: (question) => readLine(question, false),
    secret: (question) => readLine(question, true),
    close: () => input.pause(),
  }
}

/** Self-host has a hard requirement: without SMTP or OAuth nobody can ever
 * register the first account, and `start` refuses to run. Asking here is the
 * difference between a guided setup and the wall the CLI used to throw. Trial
 * runs skip this entirely — dev mode prints sign-in codes to the log. */
async function promptIdentity(reader, output) {
  output.write('\n  登录方式（自部署必须配一种，否则没人能注册第一个账号）\n')
  output.write('    1) SMTP 邮件验证码\n')
  output.write('    2) GitHub OAuth\n')
  output.write('    3) Google OAuth\n')
  const choice = await reader.ask('  选择 [1]: ')
  if (choice === '2' || choice === '3') {
    const vendor = choice === '2' ? 'GITHUB' : 'GOOGLE'
    const name = choice === '2' ? 'GitHub' : 'Google'
    const id = await reader.ask(`  ${name} Client ID: `)
    const secret = await reader.secret(`  ${name} Client Secret（不回显）: `)
    return { [`${vendor}_LOGIN_CLIENT_ID`]: id, [`${vendor}_LOGIN_CLIENT_SECRET`]: secret }
  }
  const host = await reader.ask('  SMTP 主机（如 smtp.example.com）: ')
  const user = await reader.ask('  SMTP 用户名: ')
  const pass = await reader.secret('  SMTP 密码（不回显）: ')
  const from = await reader.ask(`  发件地址 [${user}]: `)
  return {
    MAIL_SMTP_HOST: host,
    MAIL_SMTP_USER: user,
    MAIL_SMTP_PASS: pass,
    MAIL_FROM: from || user,
  }
}

/** Run the questions. Returns answers for applyAnswers(). */
export async function promptAnswers(io, { version, mode = 'trial' }) {
  const { output } = io
  const reader = createReader(io.input, output)
  try {
    output.write(`\n  DSH Cloud ${version} · 自部署引导\n`)
    output.write('  按回车用默认值，任何一项都可以稍后在 .env 里改。\n\n')

    output.write('  模型上游（你自己的 OpenAI 兼容 API）\n')
    output.write(`    1) DeepSeek 官方  ${DEEPSEEK_BASE}\n`)
    output.write('    2) 其他 OpenAI 兼容端点\n')
    const choice = await reader.ask('  选择 [1]: ')
    let upstreamBase = DEEPSEEK_BASE
    if (choice === '2') {
      const entered = await reader.ask('  端点 URL（形如 https://host/v1）: ')
      if (entered) upstreamBase = entered.replace(/\/+$/, '')
    }

    const upstreamKey = await reader.secret('  API Key（不回显，回车可跳过）: ')

    output.write('\n  联网搜索（可选，智谱 open.bigmodel.cn；回车跳过）\n')
    const searchKey = await reader.secret('  搜索 API Key: ')

    const identity = mode === 'selfhost' ? await promptIdentity(reader, output) : {}

    output.write('\n')
    return { upstreamBase, upstreamKey, searchKey, identity }
  } finally {
    reader.close()
  }
}
