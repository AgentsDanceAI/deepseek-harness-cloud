import { randomBytes } from 'node:crypto'
import { accessSync, constants } from 'node:fs'
import { access, chmod, copyFile, cp, mkdir, readFile, readdir, rename, stat, writeFile } from 'node:fs/promises'
import * as pathModule from 'node:path'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { applyAnswers, commandPrefix, nextSteps, promptAnswers, shouldRunWizard } from './wizard.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(here, '..')
const repositoryRoot = resolve(here, '../../..')
const COMMANDS = new Set(['init', 'start', 'doctor', 'up', 'down', 'status', 'logs'])
const VALUE_OPTIONS = new Set(['--mode', '--dir', '--domain', '--admin-email', '--project-name'])
const BOOLEAN_OPTIONS = new Set(['--yes', '--json', '--dry-run', '--wait', '--follow'])
const TRIAL_PUBLIC_BASE = 'http://localhost:8787'
const TRIAL_CADDY_SITE = 'http://localhost'
const PROJECT_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]*$/

export const HELP = `Usage: dsh-cloud COMMAND [DIRECTORY] [OPTIONS]

Commands:
  start    safely initialize when needed, validate, and start the stack
  init     write a managed deployment without starting Docker
  doctor   validate Docker Compose and the managed configuration
  up       start an initialized deployment
  down     stop it without deleting data
  status   show Compose service status
  logs     show service logs

Global options:
  --help              show this help
  --version           show the product release version
  --mode trial|selfhost (default: trial)
  --dir PATH          deployment directory (default: ./dsh-cloud)
  --dry-run           print the exact plan without writing or running Docker
  --json              stable machine-readable output
`

export class CliError extends Error {
  constructor(message, exitCode = 2) {
    super(message)
    this.exitCode = exitCode
  }
}

export function parseArgs(argv) {
  if (!argv.length || argv.includes('--help') || argv[0] === '-h') return { special: 'help' }
  if (argv.includes('--version')) return { special: 'version' }
  const command = argv[0]
  if (!COMMANDS.has(command)) throw new CliError(`unknown command: ${command}`)
  const options = {}
  const positionals = []
  for (let index = 1; index < argv.length; index += 1) {
    const token = argv[index]
    if (token === '--upstream-key' || token.startsWith('--upstream-key=')) throw new CliError('secret values are not accepted as command arguments')
    if (BOOLEAN_OPTIONS.has(token)) options[token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = true
    else if (VALUE_OPTIONS.has(token)) {
      const value = argv[++index]
      if (!value || value.startsWith('--')) throw new CliError(`${token} requires a value`)
      options[token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value
    } else if (token.startsWith('-')) throw new CliError(`unknown option: ${token}`)
    else positionals.push(token)
  }
  return { command, options, positionals }
}

async function exists(path) {
  return access(path, constants.F_OK).then(() => true, () => false)
}

async function atomicWrite(path, value, mode = 0o644) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  const temporary = `${path}.tmp-${process.pid}-${randomBytes(4).toString('hex')}`
  await writeFile(temporary, value, { mode, flag: 'wx' })
  await chmod(temporary, mode).catch(() => {})
  await rename(temporary, path)
}

export async function loadManifest() {
  const packaged = join(packageRoot, 'release-manifest.json')
  if (await exists(packaged)) return JSON.parse(await readFile(packaged, 'utf8'))
  const source = JSON.parse(await readFile(join(repositoryRoot, 'release/release.json'), 'utf8'))
  return {
    schemaVersion: 1,
    version: source.version,
    license: source.license,
    stackSchema: source.stackSchema,
    databaseSchema: source.databaseSchema,
    minCliVersion: source.minCliVersion,
    minUpgradeFrom: source.minUpgradeFrom,
    harnessRuntime: source.harnessRuntime,
    images: source.productImages,
    baseImages: source.baseImages,
  }
}

function targetOf(parsed) {
  return resolve(parsed.options.dir ?? parsed.positionals[0] ?? 'dsh-cloud')
}

function dockerArgv(target, projectName, action = ['up', '-d', '--wait']) {
  return ['docker', 'compose', '--project-directory', target, '--project-name', projectName,
    '--env-file', join(target, '.env'), '-f', join(target, 'docker-compose.yml'), ...action]
}

function validDomain(value) {
  if (typeof value !== 'string' || value.length > 253 || !value.includes('.')) return false
  return value.split('.').every(label => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label))
}

function validateOptions(parsed, mode, projectName) {
  if (!PROJECT_NAME_PATTERN.test(projectName)) throw new CliError('invalid project name')
  if (mode !== 'selfhost') return
  const domain = parsed.options.domain
  const email = parsed.options.adminEmail
  if (!validDomain(domain)) throw new CliError('--domain must be a hostname without a scheme or port')
  if (typeof email !== 'string' || email.length > 254 || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    throw new CliError('--admin-email must be a valid email address')
  }
}

function plan(parsed, manifest) {
  const target = targetOf(parsed)
  const mode = parsed.options.mode ?? 'trial'
  if (!['trial', 'selfhost'].includes(mode)) throw new CliError(`invalid mode: ${mode}`)
  const projectName = parsed.options.projectName ?? 'dsh-selfhost'
  validateOptions(parsed, mode, projectName)
  const trial = mode === 'trial'
  return {
    ok: true,
    dryRun: Boolean(parsed.options.dryRun),
    command: parsed.command,
    mode,
    directory: target,
    projectName,
    bindAddress: trial ? '127.0.0.1' : '0.0.0.0',
    url: trial ? TRIAL_PUBLIC_BASE : `https://${parsed.options.domain ?? ''}`,
    publicBaseUrl: trial ? TRIAL_PUBLIC_BASE : `https://${parsed.options.domain ?? ''}`,
    version: manifest.version,
    dockerArgv: dockerArgv(target, projectName, actionFor(parsed.command, parsed.options)),
  }
}

function generatedEnv(parsed, manifest, authFile, answers = {}) {
  const mode = parsed.options.mode ?? 'trial'
  const trial = mode === 'trial'
  if (!trial && (!parsed.options.domain || !parsed.options.adminEmail)) throw new CliError('--domain and --admin-email are required in selfhost mode')
  const domain = trial ? 'localhost' : parsed.options.domain
  const publicBase = trial ? TRIAL_PUBLIC_BASE : `https://${domain}`
  const lines = [
    `COMPOSE_PROJECT_NAME=${parsed.options.projectName ?? 'dsh-selfhost'}`,
    `DOMAIN=${domain}`,
    `SITE_SCHEME=${trial ? 'http' : 'https'}`,
    `PUBLIC_BASE=${publicBase}`,
    `DSH_SITE=${trial ? TRIAL_CADDY_SITE : `https://${domain}`}`,
    `BIND_ADDRESS=${trial ? '127.0.0.1' : '0.0.0.0'}`,
    `HTTP_PORT=${trial ? '8787' : '80'}`,
    `HTTPS_PORT=${trial ? '8443' : '443'}`,
    `DHC_DEV=${trial ? '1' : '0'}`,
    'DHC_CONFIG_DIR=./config',
    'PRICING_FILE=pricing.cny.json',
    `AUTH_SECRET_FILE=${authFile}`,
    'UPSTREAM_BASE_URL=https://api.deepseek.com/v1',
    'UPSTREAM_API_KEY=',
    // 联网搜索: 留空则 web_search 不可用。zhipu 走 open.bigmodel.cn,
    // upstream 则原样转发给上游的 Anthropic 端点。
    'SEARCH_PROVIDER=zhipu',
    'ZHIPU_SEARCH_API_KEY=',
    `ADMIN_EMAILS=${parsed.options.adminEmail ?? ''}`,
    'MAIL_SMTP_HOST=',
    'MAIL_SMTP_USER=',
    'MAIL_SMTP_PASS=',
    'MAIL_FROM=',
    'GOOGLE_LOGIN_CLIENT_ID=',
    'GOOGLE_LOGIN_CLIENT_SECRET=',
    'GITHUB_LOGIN_CLIENT_ID=',
    'GITHUB_LOGIN_CLIENT_SECRET=',
    `DHC_SERVER_IMAGE=${manifest.images.server}`,
    `CADDY_IMAGE=${manifest.baseImages.caddy}`,
    `POSTGRES_IMAGE=${manifest.baseImages.postgres}`,
    // 云工作台。自部署给了域名, 就默认开着 —— 那是这个产品的招牌功能, 装完
    // 却没有它等于交付了半个东西。要跑起来还差一条 DNS 记录 (work.<域名> 指向
    // 本机), 装完的面板会说。试用模式必定关: localhost 与 work.localhost 是不同
    // host, 会话 cookie 过不去, 开了只会得到无限跳登录页。
    `WORK_ENABLED=${trial ? '0' : '1'}`,
    `WORK_DOMAIN=${trial ? '' : `work.${domain}`}`,
    `COOKIE_DOMAIN=${trial ? '' : `.${domain}`}`,
    `COMPOSE_PROFILES=${trial ? '' : 'work'}`,
    `SOCKET_PROXY_IMAGE=${manifest.baseImages.socketProxy}`,
    `WORK_IMAGE=${manifest.images.workspace}`,
    '',
  ]
  return applyAnswers(lines, answers).join('\n')
}

async function templateRoots() {
  const packaged = join(packageRoot, 'templates')
  if (await exists(join(packaged, 'docker-compose.yml'))) return { template: packaged, config: join(packaged, 'config') }
  return { template: join(repositoryRoot, 'deploy/selfhost'), config: join(repositoryRoot, 'server/config') }
}

async function initialize(parsed, manifest, deploymentPlan = plan(parsed, manifest), answers = {}) {
  const target = targetOf(parsed)
  if (await exists(target)) {
    const entries = await readdir(target)
    if (entries.includes('.dsh-cloud')) throw new CliError(`deployment is already initialized: ${target}`)
    if (entries.length) throw new CliError(`refusing to overwrite non-empty directory: ${target}`)
  }
  await mkdir(target, { recursive: true, mode: 0o700 })
  const roots = await templateRoots()
  for (const name of ['docker-compose.yml', 'Caddyfile', 'compose.build.yml', 'compose.postgres.yml']) {
    if (await exists(join(roots.template, name))) await copyFile(join(roots.template, name), join(target, name))
  }
  const composePath = join(target, 'docker-compose.yml')
  const compose = (await readFile(composePath, 'utf8')).replace(
    '      - dhc-data:/app/data\n',
    '      - dhc-data:/app/data\n      - ./secrets/auth_secret:/run/secrets/auth_secret:ro\n',
  )
  await atomicWrite(composePath, compose)
  await cp(roots.config, join(target, 'config'), { recursive: true })
  const secret = process.env.DSH_CLOUD_TEST_RANDOM_HEX ?? randomBytes(32).toString('hex')
  await atomicWrite(join(target, 'secrets/auth_secret'), `${secret}\n`, 0o600)
  await atomicWrite(join(target, '.env'), generatedEnv(parsed, manifest, '/run/secrets/auth_secret', answers), 0o600)
  await atomicWrite(join(target, '.gitignore'), '.env\nsecrets/\n.dsh-cloud/lock\n', 0o644)
  await atomicWrite(join(target, '.dsh-cloud/state.json'), `${JSON.stringify({
    schemaVersion: 1,
    version: manifest.version,
    stackSchema: manifest.stackSchema,
    mode: parsed.options.mode ?? 'trial',
    projectName: parsed.options.projectName ?? 'dsh-selfhost',
    publicBaseUrl: deploymentPlan.publicBaseUrl,
    composeFiles: ['docker-compose.yml'],
  }, null, 2)}\n`, 0o600)
  return target
}

async function run(argv, cwd, capture = false) {
  const prefix = process.env.DSH_CLOUD_TEST_COMMAND_JSON ? JSON.parse(process.env.DSH_CLOUD_TEST_COMMAND_JSON) : []
  const command = prefix.length ? prefix[0] : argv[0]
  const args = prefix.length ? [...prefix.slice(1), ...argv] : argv.slice(1)
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { cwd, shell: false, stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit' })
    let stdout = ''
    let stderr = ''
    if (capture) {
      child.stdout.setEncoding('utf8')
      child.stderr.setEncoding('utf8')
      child.stdout.on('data', chunk => { stdout += chunk })
      child.stderr.on('data', chunk => { stderr += chunk })
    }
    child.on('error', reject)
    child.on('close', code => resolvePromise({ code: code ?? 1, stdout, stderr }))
  })
}

function actionFor(command, options) {
  if (command === 'up' || command === 'start') return ['up', '-d', '--wait']
  if (command === 'down') return ['down']
  if (command === 'status') return ['ps', '--format', 'json']
  if (command === 'logs') return ['logs', ...(options.follow ? ['--follow'] : [])]
  return ['config', '--quiet']
}

async function restoreDeployment(value, statePath, parsed) {
  let state
  try {
    state = JSON.parse(await readFile(statePath, 'utf8'))
  } catch {
    throw new CliError(`invalid deployment state: ${statePath}`)
  }
  if (typeof state.projectName !== 'string' || !state.projectName) throw new CliError(`invalid deployment project name: ${statePath}`)
  let environment = {}
  try {
    const content = await readFile(join(value.directory, '.env'), 'utf8')
    environment = Object.fromEntries(content.split(/\r?\n/).filter(line => line && !line.startsWith('#') && line.includes('='))
      .map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)]))
  } catch {
    // Docker Compose will produce the authoritative missing-env error later.
  }
  value.projectName = state.projectName
  value.mode = state.mode ?? value.mode
  value.publicBaseUrl = environment.PUBLIC_BASE || state.publicBaseUrl || value.publicBaseUrl
  value.url = value.publicBaseUrl
  value.bindAddress = environment.BIND_ADDRESS || (value.mode === 'trial' ? '127.0.0.1' : '0.0.0.0')
  value.dockerArgv = dockerArgv(value.directory, value.projectName, actionFor(parsed.command, parsed.options))
}

function parseComposeOutput(stdout) {
  const output = stdout.trim()
  if (!output) return undefined
  try {
    return JSON.parse(output)
  } catch {
    return output
  }
}

async function publicIdentityConfigured(target) {
  const content = await readFile(join(target, '.env'), 'utf8')
  const environment = Object.fromEntries(content.split(/\r?\n/).filter(line => line && !line.startsWith('#') && line.includes('='))
    .map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1).trim()]))
  return Boolean(
    (environment.MAIL_SMTP_HOST && (environment.MAIL_FROM || environment.MAIL_SMTP_USER)) ||
    (environment.GOOGLE_LOGIN_CLIENT_ID && environment.GOOGLE_LOGIN_CLIENT_SECRET) ||
    (environment.GITHUB_LOGIN_CLIENT_ID && environment.GITHUB_LOGIN_CLIENT_SECRET)
  )
}

async function collectAnswers(parsed, manifest, freshInit) {
  const isTTY = Boolean(process.stdin.isTTY && process.stdout.isTTY)
  if (!shouldRunWizard(parsed, { isTTY, freshInit })) return {}
  const mode = parsed.options.mode ?? 'trial'
  return promptAnswers({ input: process.stdin, output: process.stdout }, { version: manifest.version, mode })
}

/** `dsh-cloud` 在 PATH 上的哪个位置 —— 决定面板该印哪种调用形式。
 *  只查文件系统, 不起子进程; 找不到返回空串。 */
function resolveOnPath(name) {
  const { delimiter, join } = pathModule
  for (const dir of (process.env.PATH ?? '').split(delimiter)) {
    if (!dir) continue
    const candidate = join(dir, name)
    try {
      accessSync(candidate, constants.X_OK)
      return candidate
    } catch {
      // 这一段 PATH 里没有, 继续找
    }
  }
  return ''
}

/** 上游密钥到底填了没 —— 决定收尾面板要不要提醒聊天会 503。 */
async function upstreamKeyConfigured(directory) {
  try {
    const text = await readFile(join(directory, '.env'), 'utf8')
    return /^UPSTREAM_API_KEY=.+$/m.test(text)
  } catch {
    return false
  }
}

export async function execute(parsed) {
  const manifest = await loadManifest()
  if (parsed.special === 'help') return { text: HELP }
  if (parsed.special === 'version') return { text: `${manifest.version}\n` }
  const value = plan(parsed, manifest)
  const statePath = join(value.directory, '.dsh-cloud/state.json')
  const freshInit = !(await exists(statePath))
  // 只有全新部署才问; 已初始化的目录再问一遍等于诱导用户覆盖自己的配置。
  const answers = await collectAnswers(parsed, manifest, freshInit)
  if (parsed.command === 'init') {
    if (parsed.options.dryRun) return { json: value }
    await initialize(parsed, manifest, value, answers)
    return { json: { ...value, initialized: true } }
  }
  if (parsed.command === 'start' && freshInit && !parsed.options.dryRun) {
    await initialize(parsed, manifest, value, answers)
  }
  if (await exists(statePath)) await restoreDeployment(value, statePath, parsed)
  if (parsed.options.dryRun) return { json: value }
  if (!(await exists(statePath))) throw new CliError(`not an initialized deployment: ${value.directory}`)
  if (value.mode === 'selfhost' && ['start', 'up', 'doctor'].includes(parsed.command) && !(await publicIdentityConfigured(value.directory))) {
    throw new CliError(`public self-host requires SMTP or OAuth for the first verified account; edit ${join(value.directory, '.env')} and run dsh-cloud up`)
  }
  const action = actionFor(parsed.command, parsed.options)
  const argv = dockerArgv(value.directory, value.projectName, action)
  const child = await run(argv, value.directory, Boolean(parsed.options.json))
  if (child.code !== 0) throw new CliError(`Docker Compose exited with status ${child.code}`, child.code)
  if (parsed.options.json) {
    const response = { ...value, dockerArgv: argv }
    const output = parseComposeOutput(child.stdout)
    if (output !== undefined) response.composeOutput = output
    if (child.stderr.trim()) response.composeError = child.stderr.trim()
    return { json: response }
  }
  if (parsed.command !== 'start' && parsed.command !== 'up') return { text: '' }
  // 人在终端前就给完整指引; 管道/CI 里保持裸 URL, 免得打断既有脚本的解析。
  if (!process.stdout.isTTY) return { text: `${value.url}\n` }
  const hasUpstreamKey = await upstreamKeyConfigured(value.directory)
  const prefix = commandPrefix({ resolved: resolveOnPath('dsh-cloud'), entry: process.argv[1] ?? '' })
  return { text: nextSteps({ url: value.url, directory: value.directory, hasUpstreamKey, projectName: value.projectName, prefix, workDomain: value.mode === 'selfhost' ? `work.${parsed.options.domain}` : '', devMail: value.mode !== 'selfhost' }) }
}
