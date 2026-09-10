/** 真跑一格: 拉镜像 → 起容器 → 等就绪 → 从容器里打一次网关。
 *
 * **默认跳过**, 因为它要拉 2.5GB 镜像并且真的花积分。要跑:
 *
 *   AISTORE_E2E_PLAN=/tmp/codex_plan.json \
 *     corepack yarn test --run tests/cloud/local-runner.e2e.spec.ts
 *
 * 那个 JSON 是 `{"plan": <GET /api/local/plan/codex 的响应>, "token": "<设备令牌>"}`。
 *
 * 为什么值得有: 单元用例钉的是 `docker run` 的参数**拼得对不对**, 证明不了这串参数
 * 真的能跑出一个能用的工作台。2026-09-10 那次 `no matching manifest` 就是单元用例
 * 全绿、真跑第一下就挂。
 */

import { execFile } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { promisify } from 'node:util'
import { describe, expect, it } from 'vitest'
import type { LocalPlan } from '../../src/cloud/api.ts'
import { containerName, dockerBin, dockerState, freePort, pullAll, start, stop }
  from '../../src/cloud/local-runner.ts'

const exec = promisify(execFile)
const planFile = process.env.AISTORE_E2E_PLAN

describe.skipIf(planFile === undefined)('本机跑一格 (端到端)', () => {
  it('拉得下来、起得来、就绪探针通、容器里打得通网关', async () => {
    const { plan, token } = JSON.parse(readFileSync(planFile as string, 'utf8')) as
      { plan: LocalPlan, token: string }

    expect(await dockerState()).toBe('ready')

    const platforms = await pullAll(plan, line => {
      if (/Pulling from|Status:|模拟/.test(line)) console.log('   ', line)
    })
    const port = await freePort(plan.port)
    console.log(`   起 ${plan.containers.length} 个容器, 端口 ${port}`)
    await start(plan, token, port, platforms)

    // 就绪: 容器起来 ≠ 应用能应答。探针路径由服务端下发, 不猜。
    const url = `http://127.0.0.1:${port}${plan.ready_path}`
    let ready = 0
    for (let i = 0; i < 180; i += 1) {
      try {
        const res = await fetch(url)
        if (res.ok) { ready = res.status; break }
      } catch { /* 还没起来 */ }
      await new Promise(r => setTimeout(r, 2000))
    }
    expect(ready, `${url} 一直不就绪`).toBe(200)

    // **判据落在钱上**: 从容器内部、用容器自己的环境变量打一次网关。
    // 这一发同时证明三件事: 令牌替换对了、容器出得去网、用量记在账上。
    //
    // 只有主容器自己带网关环境变量的格子才做这一步。多容器栈 (Dify/Coze) 的主
    // 容器是 nginx, 模型配置在别的容器里、也没有 curl —— 对它做这个检查只会得到
    // 一个与"能不能用"无关的失败。
    // 用 OPENAI_BASE_URL 而不是 DSH_GATEWAY_BASE 判断 —— 后者**每格含义不一样**:
    // agentui 那几格是站点根 (https://aistore.best), Dify 那格已经带上了 /llm/v1。
    // 拿它拼路径会拼出 /llm/v1/llm/v1/... 然后收一个 405, 看起来像产品坏了,
    // 其实是这条用例自己的假设错了 (2026-09-10 跑 Dify 时踩到)。
    const main = plan.containers.find(c => c.role === 'main')
    const apiBase = main?.env?.OPENAI_BASE_URL
    if (apiBase === undefined || main?.env?.DSH_CLOUD_TOKEN === undefined) {
      console.log('   (这一格的主容器不直接连网关, 跳过计费一发; 就绪已验)')
      await stop(plan.product)
      return
    }
    const bin = await dockerBin() as string
    const { stdout } = await exec(bin, [
      'exec', containerName(plan.product), 'sh', '-lc',
      'curl -s -o /dev/null -w "%{http_code}" -X POST '
      + '"$OPENAI_BASE_URL/chat/completions" '
      + '-H "authorization: Bearer $OPENAI_API_KEY" -H "content-type: application/json" '
      + '-d \'{"model":"gpt-5.6-luna","max_tokens":8,"messages":[{"role":"user","content":"say ok"}]}\'',
    ], { timeout: 180_000 })
    expect(stdout.trim(), '容器里打网关没拿到 200').toBe('200')

    await stop(plan.product)
  }, 1_800_000)
})
