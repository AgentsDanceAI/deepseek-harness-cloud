/** 把一格工作台跑在**用户自己的机器**上。
 *
 * 分工不变: 算力在本机, 账号 / 模型网关 / 计费仍在 aistore.best。这里只做执行 ——
 * 起什么、用哪个镜像、怎么接线, 全部来自服务端下发的计划 (见 api.fetchLocalPlan)。
 *
 * 为什么执行端要这么薄: 目录一旦在客户端留一份副本, 它就会过期, 而过期的表现不是
 * 报错 —— 是拉到一个旧镜像然后一切看起来正常。桌面端发一次版要签名公证, 追不上
 * 镜像的节奏。
 */

import { execFile } from 'node:child_process'
import { homedir } from 'node:os'
import { promisify } from 'node:util'
import type { LocalPlan, PlanContainer } from './api.ts'

const exec = promisify(execFile)

/** 容器名前缀。停/查都按它过滤, 不会碰到用户自己的容器。 */
export const CONTAINER_PREFIX = 'aistore-'

export function containerName(productId: string): string {
  return `${CONTAINER_PREFIX}${productId}`
}

export class DockerMissingError extends Error {
  constructor(readonly state: DockerState) {
    super(`docker not available: ${state}`)
  }
}

/**
 * docker 可执行文件的候选位置。
 *
 * **不能只 execFile('docker')**: 从 Finder / Dock 启动的 GUI 应用**不继承登录
 * shell 的 PATH** —— macOS 给的是 launchd 的默认值 (/usr/bin:/bin:/usr/sbin:/sbin),
 * 而 Docker Desktop 把 CLI 装在 /usr/local/bin 或 ~/.docker/bin。表现是应用里说
 * 「这台机器上没有可用的 Docker」, 而用户在终端里 `docker info` 好好的。
 * 2026-09-10 老板第一次跑本地包就是这样: 机器上装着 29.2.1, 货架说没有。
 */
export function candidates(): string[] {
  if (process.platform === 'win32') {
    return ['docker.exe', 'C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe']
  }
  return [
    'docker', // PATH 里有就用 PATH 的 (终端启动、Linux、Windows 都走这条)
    '/usr/local/bin/docker',
    '/opt/homebrew/bin/docker',
    `${homedir()}/.docker/bin/docker`,
    '/Applications/Docker.app/Contents/Resources/bin/docker',
    '/usr/bin/docker',
  ]
}

let resolvedBin: string | undefined

/** 找到 docker 可执行文件的绝对路径; 找不到返回 undefined。找到的结果会记住。 */
export async function dockerBin(): Promise<string | undefined> {
  if (resolvedBin !== undefined) return resolvedBin
  for (const candidate of candidates()) {
    try {
      await exec(candidate, ['--version'], { timeout: 8_000 })
      resolvedBin = candidate
      return resolvedBin
    } catch {
      // 换下一个候选
    }
  }
  return undefined
}

export type DockerState = 'ready' | 'daemon-down' | 'missing'

/**
 * 「找不到 docker」和「docker 装了但没启动」是**两种状态, 两种说法**。
 *
 * 合成一个 boolean 的话, 用户得到的提示永远是"去装 Docker Desktop" —— 而他可能
 * 已经装了, 只是没打开。`--version` 只证明二进制在, `info` 才证明守护进程通。
 */
export async function dockerState(): Promise<DockerState> {
  const bin = await dockerBin()
  if (bin === undefined) return 'missing'
  try {
    await exec(bin, ['info', '--format', '{{.ServerVersion}}'], { timeout: 15_000 })
    return 'ready'
  } catch {
    return 'daemon-down'
  }
}

export async function dockerReady(): Promise<boolean> {
  return await dockerState() === 'ready'
}

function mainOf(plan: LocalPlan): PlanContainer {
  const main = plan.containers.find(c => c.role === 'main')
  if (main === undefined) throw new Error(`plan for ${plan.product} has no main container`)
  return main
}

/**
 * 容器里的家目录 —— 持久卷挂这里。
 *
 * 不能写死 /home/agent: 有几格是以 root 跑的 (openmanus / openmausbot 的
 * DSH_AGENT_HOME=/root)。挂错地方不会报错, 只是用户下次打开发现东西没了。
 */
export function homeOf(env: Record<string, string>): string {
  return env.DSH_AGENT_HOME ?? env.HOME ?? '/home/agent'
}

/** 把计划里的令牌占位符换成本机这一把。env 值和命令行都要换 —— 初始化容器的
 *  命令行里也可能带占位符。 */
function fill(value: string, plan: LocalPlan, token: string): string {
  return value.split(plan.token_placeholder).join(token)
}

/**
 * 拼 `docker run` 的参数。**纯函数, 不碰系统** —— 这是这个模块唯一值得写用例的
 * 地方: 端口映射、卷挂载、降权、令牌替换错了都不会抛异常, 只会安静地跑出一个
 * 不对的容器。
 */
export function buildRunArgs(plan: LocalPlan, token: string, hostPort: number): string[] {
  const main = mainOf(plan)
  const env = Object.fromEntries(
    Object.entries(main.env).map(([k, v]) => [k, fill(v, plan, token)]),
  )
  const args = [
    'run', '-d',
    '--name', containerName(plan.product),
    // **只绑回环**: 容器里带着一把能花钱的令牌, 不该对局域网可见。
    '-p', `127.0.0.1:${hostPort}:${plan.port}`,
    '-v', `${containerName(plan.product)}-data:${homeOf(env)}`,
  ]
  if (main.run_as_user !== undefined && main.run_as_user !== null) {
    args.push('--user', String(main.run_as_user))
  }
  for (const [k, v] of Object.entries(env)) args.push('-e', `${k}=${v}`)
  args.push(main.image_ref)
  for (const part of main.cmd) args.push(fill(part, plan, token))
  return args
}

export interface PullProgress { (line: string): void }

/** 拉镜像。逐行回调, 让界面能显示进度 —— 首次拉一格是 2.5GB 起, 没有进度的
 *  等待会被当成卡死。 */
export async function pull(image: string, onLine?: PullProgress): Promise<void> {
  const bin = await dockerBin()
  if (bin === undefined) throw new DockerMissingError('missing')
  await new Promise<void>((resolve, reject) => {
    const child = execFile(bin, ['pull', image], { maxBuffer: 1 << 24 }, error => {
      if (error) reject(error)
      else resolve()
    })
    child.stdout?.on('data', (chunk: Buffer | string) => onLine?.(String(chunk).trimEnd()))
  })
}

/** 起一格。同名容器还在就先收掉 —— 否则 docker 只回一句名字冲突, 而用户看到的
 *  是一行看不懂的红字, 不知道那是"上次那个还开着"。 */
export async function start(plan: LocalPlan, token: string, hostPort: number): Promise<void> {
  const state = await dockerState()
  if (state !== 'ready') throw new DockerMissingError(state)
  if (plan.runnable !== 'ready') throw new Error(plan.reason || 'not runnable locally')
  await stop(plan.product)
  const bin = await dockerBin()
  await exec(bin as string, buildRunArgs(plan, token, hostPort), { timeout: 120_000 })
}

export async function stop(productId: string): Promise<void> {
  const bin = await dockerBin()
  if (bin === undefined) return
  try {
    await exec(bin, ['rm', '-f', containerName(productId)], { timeout: 60_000 })
  } catch {
    // 没在跑就没什么可停的
  }
}

export interface RunningSlot { product: string, status: string, ports: string }

export async function running(): Promise<RunningSlot[]> {
  const bin = await dockerBin()
  if (bin === undefined) return []
  const { stdout } = await exec(
    bin,
    ['ps', '--filter', `name=${CONTAINER_PREFIX}`, '--format', '{{.Names}}\t{{.Status}}\t{{.Ports}}'],
    { timeout: 15_000 },
  )
  return stdout.split('\n').filter(line => line.trim() !== '').map(line => {
    const [name = '', status = '', ports = ''] = line.split('\t')
    return { product: name.slice(CONTAINER_PREFIX.length), status, ports }
  })
}

/**
 * 从 `preferred` 起找一个本机能绑的端口。
 *
 * 不直接用计划里那个: 端口是**容器内**的, 两格撞车很正常 (agentui 那几格都是
 * 8080)。而 docker 端口被占时的报错发生在 `docker run` 之后 —— 容器已经建了,
 * 用户看到的是一行 bind 失败的红字, 而不是"换一个端口就好"。
 */
export async function freePort(preferred: number, tries = 50): Promise<number> {
  const { createServer } = await import('node:net')
  const usable = (port: number): Promise<boolean> => new Promise(resolve => {
    const server = createServer()
    server.once('error', () => { resolve(false) })
    server.once('listening', () => { server.close(() => { resolve(true) }) })
    server.listen(port, '127.0.0.1')
  })
  for (let port = preferred; port < preferred + tries; port += 1) {
    if (await usable(port)) return port
  }
  throw new Error(`no free port near ${preferred}`)
}
