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
import { promisify } from 'node:util'
import type { LocalPlan, PlanContainer } from './api.ts'

const exec = promisify(execFile)

/** 容器名前缀。停/查都按它过滤, 不会碰到用户自己的容器。 */
export const CONTAINER_PREFIX = 'aistore-'

export function containerName(productId: string): string {
  return `${CONTAINER_PREFIX}${productId}`
}

export class DockerMissingError extends Error {
  constructor() {
    super('docker not available')
  }
}

/** docker 在不在、守护进程通不通。**两件事一起验**: 装了但没启动的机器上
 * `docker --version` 照样成功, 而任何真实操作都会挂。 */
export async function dockerReady(): Promise<boolean> {
  try {
    await exec('docker', ['info', '--format', '{{.ServerVersion}}'], { timeout: 10_000 })
    return true
  } catch {
    return false
  }
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
  await new Promise<void>((resolve, reject) => {
    const child = execFile('docker', ['pull', image], { maxBuffer: 1 << 24 }, error => {
      if (error) reject(error)
      else resolve()
    })
    child.stdout?.on('data', (chunk: Buffer | string) => onLine?.(String(chunk).trimEnd()))
  })
}

/** 起一格。同名容器还在就先收掉 —— 否则 docker 只回一句名字冲突, 而用户看到的
 *  是一行看不懂的红字, 不知道那是"上次那个还开着"。 */
export async function start(plan: LocalPlan, token: string, hostPort: number): Promise<void> {
  if (!await dockerReady()) throw new DockerMissingError()
  if (plan.runnable !== 'ready') throw new Error(plan.reason || 'not runnable locally')
  await stop(plan.product)
  await exec('docker', buildRunArgs(plan, token, hostPort), { timeout: 120_000 })
}

export async function stop(productId: string): Promise<void> {
  try {
    await exec('docker', ['rm', '-f', containerName(productId)], { timeout: 60_000 })
  } catch {
    // 没在跑就没什么可停的
  }
}

export interface RunningSlot { product: string, status: string, ports: string }

export async function running(): Promise<RunningSlot[]> {
  if (!await dockerReady()) return []
  const { stdout } = await exec(
    'docker',
    ['ps', '--filter', `name=${CONTAINER_PREFIX}`, '--format', '{{.Names}}\t{{.Status}}\t{{.Ports}}'],
    { timeout: 15_000 },
  )
  return stdout.split('\n').filter(line => line.trim() !== '').map(line => {
    const [name = '', status = '', ports = ''] = line.split('\t')
    return { product: name.slice(CONTAINER_PREFIX.length), status, ports }
  })
}
