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

/** 伴随容器的容器名。用双横线分隔, 好让 stop 能按一个正则把整栈收干净, 又不会
 *  误伤名字前缀相同的另一格 (aistore-dify 与 aistore-dify-x)。 */
export function sidecarName(productId: string, sidecar: string): string {
  return `${containerName(productId)}--${sidecar}`
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
export function buildRunArgs(
  plan: LocalPlan, token: string, hostPort: number, platform?: string,
): string[] {
  const main = mainOf(plan)
  const env = Object.fromEntries(
    Object.entries(main.env).map(([k, v]) => [k, fill(v, plan, token)]),
  )
  const args = [
    'run', '-d',
    // 栈里的容器是一起起来的, 谁先谁后没保证 —— Dify 的 plugin-daemon 连不上还
    // 没起来的 postgres 就 panic 退出, 一退就没了 (docker run 默认不重启), 而
    // 页面照样能开: 用户只有点到插件市场才会发现它是坏的。
    // 有界重启把这个竞态吃掉; 用 on-failure 而不是 unless-stopped, 免得用户重启
    // 电脑之后十个容器自己冒出来。
    '--restart', 'on-failure:10',
    '--name', containerName(plan.product),
    // 拉的时候用了哪个 platform, 跑的时候必须一致 —— 否则 docker 会去找一个本机
    // 架构的镜像, 而那个镜像根本不存在。
    ...platform === undefined ? [] : ['--platform', platform],
    // **只绑回环**: 容器里带着一把能花钱的令牌, 不该对局域网可见。
    '-p', `127.0.0.1:${hostPort}:${plan.port}`,
    '-v', `${containerName(plan.product)}-data:${homeOf(env)}`,
    // 主容器是**网络命名空间的主人**, 只有它能加 host 映射 (伴随容器用
    // --network container: 加入之后, docker 会拒绝 --add-host)。
    // 上游那些配置把服务名当主机名用, 在共享命名空间里都指回环。
    ...plan.host_aliases.flatMap(alias => ['--add-host', `${alias}:127.0.0.1`]),
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

/** 本机架构上没有原生 manifest 时 docker 的说法。 */
const NO_NATIVE_MANIFEST = /no matching manifest|no match for platform/i

/** 工作台镜像目前只出 linux/amd64。 */
export const FOREIGN_PLATFORM = 'linux/amd64'

function pullOnce(bin: string, image: string, platform: string | undefined, onLine?: PullProgress):
Promise<void> {
  const args = platform === undefined ? ['pull', image] : ['pull', '--platform', platform, image]
  return new Promise<void>((resolve, reject) => {
    const child = execFile(bin, args, { maxBuffer: 1 << 24 }, error => {
      if (error) reject(error)
      else resolve()
    })
    child.stdout?.on('data', (chunk: Buffer | string) => onLine?.(String(chunk).trimEnd()))
  })
}

/**
 * 拉镜像。返回**跑它要用的 --platform**: undefined 表示本机原生。
 *
 * 先按原生拉, 拉不动再退回 linux/amd64 —— 而不是一上来就写死 amd64: 哪天我们出了
 * arm64 镜像, 写死的那版会让 M 系机器继续白白走模拟。
 *
 * 为什么必须有这个回退: Apple Silicon 上 `docker pull` 一个只有 amd64 manifest 的
 * 镜像**会直接失败**, 不是"慢一点" ——
 *   no matching manifest for linux/arm64/v8 in the manifest list entries
 * 2026-09-10 老板点 Codex 撞上的就是这个, 而我在 README 里写的是"走模拟, 能用但慢"。
 * 说错了: 不给 --platform 根本拉不下来。
 */
export async function pull(image: string, onLine?: PullProgress): Promise<string | undefined> {
  const bin = await dockerBin()
  if (bin === undefined) throw new DockerMissingError('missing')
  try {
    await pullOnce(bin, image, undefined, onLine)
    return undefined
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause)
    if (!NO_NATIVE_MANIFEST.test(message)) throw cause
    onLine?.(`本机架构没有原生镜像，改用 ${FOREIGN_PLATFORM} 模拟运行（会慢一些）`)
    await pullOnce(bin, image, FOREIGN_PLATFORM, onLine)
    return FOREIGN_PLATFORM
  }
}

/** 起一格。同名容器还在就先收掉 —— 否则 docker 只回一句名字冲突, 而用户看到的
 *  是一行看不懂的红字, 不知道那是"上次那个还开着"。 */
/**
 * 伴随容器的 `docker run` 参数。
 *
 * **加入主容器的网络命名空间**, 不是接同一个 bridge 网络 —— 上游那些栈的配置里
 * 全是 `127.0.0.1:5432` 这类回环地址 (实测 Dify: 24 处回环 vs 0 处服务名), 接
 * bridge 再靠 DNS 解析服务名的话, 这些配置一条都不成立。云端用的是 k8s pod 语义,
 * 本机对应的就是 `--network container:`。
 */
export function buildSidecarArgs(
  plan: LocalPlan, sidecar: PlanContainer, token: string, platform?: string,
): string[] {
  const env = Object.fromEntries(
    Object.entries(sidecar.env ?? {}).map(([k, v]) => [k, fill(v, plan, token)]),
  )
  const args = [
    'run', '-d',
    '--restart', 'on-failure:10',
    '--name', sidecarName(plan.product, sidecar.name),
    '--network', `container:${containerName(plan.product)}`,
    ...platform === undefined ? [] : ['--platform', platform],
  ]
  for (const [k, v] of Object.entries(env)) args.push('-e', `${k}=${v}`)
  args.push(sidecar.image_ref)
  // cmd 顶掉 entrypoint, args 不顶 —— 有些镜像的 entrypoint 是一整套初始化
  // (Hermes 是 s6 监督树), 顶掉它服务起不来, 而它只在 stderr 抱怨一句、进程照跑。
  for (const part of sidecar.cmd ?? []) args.push(fill(part, plan, token))
  for (const part of sidecar.args ?? []) args.push(fill(part, plan, token))
  return args
}

/** 初始化容器: 前台跑完就退, 后面的容器才起。顺序保证是它存在的全部意义。 */
export function buildInitArgs(
  plan: LocalPlan, init: PlanContainer, token: string, home: string, platform?: string,
): string[] {
  const args = [
    'run', '--rm',
    '--name', sidecarName(plan.product, `init-${init.name}`),
    ...platform === undefined ? [] : ['--platform', platform],
    '-v', `${containerName(plan.product)}-data:${home}`,
  ]
  args.push(init.image_ref)
  for (const part of init.cmd ?? []) args.push(fill(part, plan, token))
  return args
}

/**
 * 把这一格要用的镜像全部拉下来, 返回**每个镜像各自**的 --platform。
 *
 * 逐个镜像判, 不是一刀切: 我们自己的工作台镜像只有 amd64, 而栈里的上游中间件
 * (postgres / redis / weaviate) 大多是多架构的 —— 一刀切给它们也扣上 amd64,
 * 等于让本来能原生跑的东西白白走模拟。
 */
export async function pullAll(plan: LocalPlan, onLine?: PullProgress):
Promise<Map<string, string | undefined>> {
  const out = new Map<string, string | undefined>()
  for (const c of plan.containers) {
    if (out.has(c.image_ref)) continue
    onLine?.(`拉镜像 ${c.image_ref}`)
    out.set(c.image_ref, await pull(c.image_ref, onLine))
  }
  return out
}

export async function start(
  plan: LocalPlan, token: string, hostPort: number,
  platform?: string | ReadonlyMap<string, string | undefined>,
): Promise<void> {
  const state = await dockerState()
  if (state !== 'ready') throw new DockerMissingError(state)
  if (plan.runnable !== 'ready') throw new Error(plan.reason || 'not runnable locally')
  await stop(plan.product)
  const bin = await dockerBin() as string
  const main = mainOf(plan)
  const home = homeOf(main.env ?? {})
  // 单个字符串是"整栈同一个架构"的简写 (单容器格子就是这么调的)
  const platformOf = (image: string): string | undefined =>
    typeof platform === 'string' || platform === undefined ? platform : platform.get(image)

  // 1. 初始化容器逐个跑完 —— **不能并行**, 它们之间就是靠顺序保证的
  for (const init of plan.containers.filter(c => c.role === 'init')) {
    await exec(bin, buildInitArgs(plan, init, token, home, platformOf(init.image_ref)),
      { timeout: 600_000 })
  }
  // 2. 主容器: 它建网络命名空间, 端口也只有它映射
  await exec(bin, buildRunArgs(plan, token, hostPort, platformOf(main.image_ref)), { timeout: 300_000 })
  // 3. 伴随容器加入主容器的命名空间
  for (const sidecar of plan.containers.filter(c => c.role === 'sidecar')) {
    await exec(bin, buildSidecarArgs(plan, sidecar, token, platformOf(sidecar.image_ref)),
      { timeout: 300_000 })
  }
}

/** 停一格 —— 连同它的整栈。
 *
 * 按名字正则收: 主容器叫 aistore-<格>, 伴随容器叫 aistore-<格>--<名>。锚定两端,
 * 免得停 dify 顺手把 dify-x 也带走。 */
export async function stop(productId: string): Promise<void> {
  const bin = await dockerBin()
  if (bin === undefined) return
  try {
    const { stdout } = await exec(bin, [
      'ps', '-aq', '--filter', `name=^${containerName(productId)}(--.*)?$`,
    ], { timeout: 30_000 })
    const ids = stdout.split('\n').map(x => x.trim()).filter(x => x !== '')
    if (ids.length > 0) await exec(bin, ['rm', '-f', ...ids], { timeout: 120_000 })
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
  // 伴随容器不是"一格" —— 列出来的话货架上会冒出 dify--redis 这种鬼东西
  }).filter(row => !row.product.includes('--'))
}

/**
 * 从 `preferred` 起找一个本机能绑的端口。
 *
 * 不直接用计划里那个: 端口是**容器内**的, 两格撞车很正常 (agentui 那几格都是
 * 8080)。而 docker 端口被占时的报错发生在 `docker run` 之后 —— 容器已经建了,
 * 用户看到的是一行 bind 失败的红字, 而不是"换一个端口就好"。
 */
export async function freePort(preferred: number, tries = 50): Promise<number> {
  // **1024 以下要 root 才绑得住。** Dify 的容器端口是 80 —— 直接拿它当本机端口,
  // 五十次尝试全是 EACCES, 而报出来的是"附近没有空闲端口", 与真的端口被占完
  // 长得一模一样 (2026-09-10 本机跑 Dify 撞上, 排查方向差点跑偏)。
  const base = preferred < 1024 ? 18000 + preferred : preferred
  const { createServer } = await import('node:net')
  const usable = (port: number): Promise<boolean> => new Promise(resolve => {
    const server = createServer()
    server.once('error', () => { resolve(false) })
    server.once('listening', () => { server.close(() => { resolve(true) }) })
    server.listen(port, '127.0.0.1')
  })
  for (let port = base; port < base + tries; port += 1) {
    if (await usable(port)) return port
  }
  throw new Error(`no free port near ${base}`)
}
