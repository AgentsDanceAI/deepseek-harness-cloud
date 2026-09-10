/** `docker run` 参数拼错了不会抛异常 —— 只会安静地跑出一个不对的容器。
 *
 * 所以这些用例钉的全是"错了也不报错"的地方: 端口只绑回环、持久卷挂在这一格真正
 * 的家目录上、令牌占位符被换掉、以及计划里不该出现真令牌。
 */

import { describe, expect, it } from 'vitest'
import type { LocalPlan } from '../../src/cloud/api.ts'
import { buildRunArgs, candidates, containerName, homeOf } from '../../src/cloud/local-runner.ts'

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
