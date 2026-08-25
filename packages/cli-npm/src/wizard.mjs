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
  return lines
}

/** The closing panel. Pure so its content is testable. */
export function nextSteps({ url, directory, hasUpstreamKey }) {
  const lines = [
    '',
    '  云工作台已就绪',
    '',
    `  打开    ${url}`,
    '  登录    用任意邮箱收验证码即可注册；试用模式下没有配邮件服务器，',
    '          验证码打印在服务端日志里：',
    '            dsh-cloud logs | grep -A1 dev-mail',
    '',
  ]
  if (!hasUpstreamKey) {
    lines.push(
      '  注意    还没有配模型上游，聊天会返回 503。把 UPSTREAM_API_KEY 填进',
      `          ${directory}/.env 后执行 dsh-cloud up 生效。`,
      '',
    )
  }
  lines.push(
    `  配置    ${directory}/.env（改完 dsh-cloud up 生效）`,
    '  停止    dsh-cloud down（数据保留）',
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

/** Run the three questions. Returns answers for applyAnswers(). */
export async function promptAnswers(io, { version }) {
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

    output.write('\n')
    return { upstreamBase, upstreamKey, searchKey }
  } finally {
    reader.close()
  }
}
