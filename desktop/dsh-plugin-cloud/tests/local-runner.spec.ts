/** `docker run` 参数拼错了不会抛异常 —— 只会安静地跑出一个不对的容器。
 *
 * 所以这些用例钉的全是"错了也不报错"的地方: 端口只绑回环、持久卷挂在这一格真正
 * 的家目录上、令牌占位符被换掉、以及计划里不该出现真令牌。
 */

import { describe, expect, it } from 'vitest'
import type { LocalPlan } from '../../src/cloud/api.ts'
import { buildRunArgs, buildSidecarArgs, candidates, containerName, freePort, homeOf, sidecarName }
  from '../../src/cloud/local-runner.ts'

const PLACEHOLDER = '${AISTORE_TOKEN}'

function planOf(env: Record<string, string>, extra: Partial<LocalPlan> = {}): LocalPlan {
  return {
    product: 'codex',
    name: 'Codex',
    port: 8080,
    ready_path: '/api/health',
    gateway: 'https://aistore.best',
    token_placeholder: PLACEHOLDER,
    runnable: 'ready',
    reason: '',
    host_aliases: [],
    seeds: [],
    containers: [{
      role: 'main',
      name: 'codex',
      image_ref: 'ghcr.io/agentsdancepro/agentui:0.2.5',
      cmd: ['sh', '-c', `echo ${PLACEHOLDER}`],
      args: [],
      env,
      network: 'own',
      port: 8080,
    }],
    ...extra,
  }
}

describe('buildRunArgs', () => {
  it('只把端口绑到回环', () => {
    const args = buildRunArgs(planOf({ DSH_CLOUD_TOKEN: PLACEHOLDER }), 'tok_real', 18080)
    const publish = args[args.indexOf('-p') + 1]
    // 容器里带着一把能花钱的令牌 —— 绑 0.0.0.0 就是把它交给同一个网络里的任何人
    expect(publish).toBe('127.0.0.1:18080:8080')
  })

  it('把令牌占位符换成真令牌, env 和命令行都换', () => {
    const args = buildRunArgs(planOf({ DSH_CLOUD_TOKEN: PLACEHOLDER }), 'tok_real', 18080)
    expect(args).toContain('DSH_CLOUD_TOKEN=tok_real')
    expect(args.some(a => a.includes(PLACEHOLDER))).toBe(false)
    expect(args[args.length - 1]).toBe('echo tok_real')
  })

  it('持久卷挂在这一格真正的家目录上', () => {
    // 写死 /home/agent 的话, 以 root 跑的那几格 (openmanus / openmausbot) 的数据
    // 会落在一个没人读的地方 —— 不报错, 只是下次打开东西没了
    const rooted = buildRunArgs(planOf({ DSH_AGENT_HOME: '/root' }), 't', 1)
    expect(rooted[rooted.indexOf('-v') + 1]).toBe('aistore-codex-data:/root')
    const agent = buildRunArgs(planOf({ HOME: '/home/agent' }), 't', 1)
    expect(agent[agent.indexOf('-v') + 1]).toBe('aistore-codex-data:/home/agent')
  })

  it('计划里给了降权用户就带上 --user', () => {
    const plan = planOf({ HOME: '/home/agent' })
    plan.containers[0]!.run_as_user = 1000
    const args = buildRunArgs(plan, 't', 1)
    expect(args[args.indexOf('--user') + 1]).toBe('1000')
    // 没给就不能瞎猜一个 —— 有几格是故意以 root 跑的
    expect(buildRunArgs(planOf({ HOME: '/x' }), 't', 1)).not.toContain('--user')
  })
})

describe('homeOf', () => {
  it('DSH_AGENT_HOME 优先于 HOME, 都没有才回落', () => {
    expect(homeOf({ DSH_AGENT_HOME: '/root', HOME: '/home/agent' })).toBe('/root')
    expect(homeOf({ HOME: '/home/agent' })).toBe('/home/agent')
    expect(homeOf({})).toBe('/home/agent')
  })
})

describe('containerName', () => {
  it('带前缀, 停和查都按它过滤, 碰不到用户自己的容器', () => {
    expect(containerName('codex')).toBe('aistore-codex')
  })
})

describe('candidates', () => {
  it('macOS 上不能只靠 PATH', () => {
    // 从 Finder / Dock 启动的应用拿到的是 launchd 的默认 PATH
    // (/usr/bin:/bin:/usr/sbin:/sbin) —— Docker Desktop 的 CLI 不在里面。
    // 实测: 老写法在这个 PATH 下 ENOENT, 而机器上装着 29.2.1。
    if (process.platform === 'win32') return
    const list = candidates()
    expect(list[0]).toBe('docker') // PATH 里有就先用 PATH 的
    for (const known of ['/usr/local/bin/docker', '/Applications/Docker.app/Contents/Resources/bin/docker']) {
      expect(list).toContain(known)
    }
    expect(list.some(p => p.endsWith('/.docker/bin/docker'))).toBe(true)
  })
})

describe('buildRunArgs 的 --platform', () => {
  it('拉的时候用了哪个 platform, 跑的时候就得带上同一个', () => {
    // Apple Silicon 上 `docker pull` 一个只有 amd64 manifest 的镜像**会失败**,
    // 不是"慢一点"。拉用了 --platform, run 不带 = docker 去找一个不存在的本机
    // 架构镜像, 容器起不来。
    const args = buildRunArgs(planOf({ HOME: '/home/agent' }), 't', 1, 'linux/amd64')
    expect(args[args.indexOf('--platform') + 1]).toBe('linux/amd64')
    // 本机原生的时候不能瞎加 —— 写死 amd64 会让将来的 arm64 镜像白白走模拟
    expect(buildRunArgs(planOf({ HOME: '/x' }), 't', 1)).not.toContain('--platform')
  })
})

describe('多容器栈', () => {
  function stackPlan(): LocalPlan {
    const p = planOf({ HOME: '/home/agent' })
    p.product = 'dify'
    p.host_aliases = ['api', 'redis']
    p.containers.push({
      role: 'sidecar', name: 'redis', image_ref: 'redis:6-alpine',
      cmd: ['redis-server'], args: ['--requirepass', 'x'], env: { FOO: 'bar' }, network: 'share:main',
    })
    return p
  }

  it('伴随容器加入主容器的网络命名空间, 不是接同一个 bridge', () => {
    // 上游那些栈的配置里全是 127.0.0.1 (实测 Dify: 24 处回环、0 处服务名) ——
    // 接 bridge 再靠 DNS 解析服务名的话, 这些配置一条都不成立。
    const p = stackPlan()
    const args = buildSidecarArgs(p, p.containers[1]!, 'tok', undefined)
    expect(args[args.indexOf('--network') + 1]).toBe('container:aistore-dify')
    expect(args).not.toContain('-p')       // 端口只有主容器映射
    expect(args).not.toContain('--add-host') // 共享命名空间时 docker 会拒绝
    // cmd 在 args 前面, 顺序不能反 —— 反了就是给 entrypoint 传了一堆它不认的参数
    expect(args.slice(-3)).toEqual(['redis-server', '--requirepass', 'x'])
  })

  it('host 别名只加在主容器上', () => {
    const args = buildRunArgs(stackPlan(), 'tok', 18080)
    expect(args[args.indexOf('--add-host') + 1]).toBe('api:127.0.0.1')
    expect(args.filter(a => a === '--add-host')).toHaveLength(2)
  })

  it('伴随容器的名字带双横线, 好让 stop 一把收干净又不误伤别的格', () => {
    expect(sidecarName('dify', 'redis')).toBe('aistore-dify--redis')
    expect(sidecarName('dify', 'redis').startsWith(containerName('dify'))).toBe(true)
  })
})

describe('freePort', () => {
  it('特权端口要挪到高位 —— 不然 Dify(容器端口 80) 一个都绑不上', async () => {
    // 失败形状很坑: 五十次 EACCES 报出来是"附近没有空闲端口", 和端口真被占完
    // 长得一样, 排查方向会跑偏。
    const port = await freePort(80)
    expect(port).toBeGreaterThanOrEqual(1024)
    // 普通端口保持"能用原号就用原号"的好处: 用户看到的端口和产品端口对得上
    const high = await freePort(28123)
    expect(high).toBeGreaterThanOrEqual(28123)
  })
})
