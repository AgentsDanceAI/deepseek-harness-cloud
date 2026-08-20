/** DSH Cloud gate: the login wall and the gateway injection.
 *
 * Called from dsh-plugin-desktop/src/main.ts by the 0003-cloud-gate patch —
 * the ONLY two upstream call sites. Everything else lives in this directory so
 * upstream bumps stay a mechanical rebase.
 */

import { app, BrowserWindow } from 'electron'
import { fetchModels, validateToken, type CloudModel, type CloudUser } from './api.ts'
import { clearToken, loadToken, saveToken } from './auth-store.ts'
import { CLOUD_BASE, CLOUD_TOKEN_ENV } from './config.ts'
import { runLoginWindow } from './login-window.ts'

/**
 * 网关目录, 由 cloudGate() 在登录成功后拉一次填上。
 *
 * cloudProfilePatches() 是同步的, 而目录要走网络, 所以只能这样交接 —— 好在
 * 补丁的两个调用点顺序是固定的 (0003-cloud-gate.patch: 先 await cloudGate(),
 * 再 push cloudProfilePatches()), 拉取结果一定赶得上。拉不到就留空, 见下面
 * cloudProfilePatches() 里的降级说明。
 */
let discoveredModels: CloudModel[] = []

export interface CloudSession {
  token: string
  user?: CloudUser
}

/**
 * Blocks until a usable cloud session exists, or returns undefined when the
 * user declined to log in (the launcher then quits).
 *
 * On success the token is exported into the real process environment under
 * CLOUD_TOKEN_ENV, where dsh's credential seam (env source: highest priority,
 * read-only, resolved per request) picks it up via the apiKeyEnv references
 * injected by cloudProfilePatches().
 */
/** 拉一次网关目录; 失败不阻塞启动 (见 cloudProfilePatches 的降级说明)。 */
async function loadCatalog(token: string): Promise<void> {
  try {
    discoveredModels = await fetchModels(token)
  } catch {
    discoveredModels = []
  }
}

/**
 * 把已经在跑的应用带到前台。
 *
 * ⚠️ 只有在**系统已经允许**我们前台化时才有效 —— 即这次调用源自用户在前台
 * 浏览器里点开的 dshcloud:// 深链。单纯在后台调 app.focus({steal:true}) 是
 * 不行的 (2026-08-19 实测: 2.0.0 包里就有这行, macOS 照样不切换, 顶多 Dock
 * 图标跳一下), 所以焦点这件事必须由深链驱动, 不能自己硬抢。
 */
function bringToFront(): void {
  try {
    const win = BrowserWindow.getAllWindows().find(w => !w.isDestroyed())
    if (win !== undefined) {
      if (win.isMinimized()) win.restore()
      win.show()
      win.focus()
    }
    if (process.platform === 'darwin') app.focus({ steal: true })
  } catch { /* 抢焦点失败不该影响任何主流程 */ }
}

/**
 * 接住授权成功页发来的 dshcloud:// 深链。
 *
 * macOS 走 open-url; Windows/Linux 上第二次启动会把 URL 交给首个实例, 走
 * second-instance。两条路都只做一件事: 把窗口带到前台 —— 令牌本身是登录窗
 * 自己轮询拿到的, 深链只负责"把人送回来", 不携带也不信任任何凭据
 * (URL 是外部输入, 当作纯信号处理)。
 */
export function registerDeepLink(): void {
  app.on('open-url', (event, _url) => {
    event.preventDefault()
    bringToFront()
  })
  app.on('second-instance', () => { bringToFront() })
}

export async function cloudGate(): Promise<CloudSession | undefined> {
  registerDeepLink()
  const userDataDir = app.getPath('userData')
  const stored = loadToken(userDataDir)
  if (stored !== undefined) {
    try {
      const user = await validateToken(stored.token)
      if (user !== undefined) {
        process.env[CLOUD_TOKEN_ENV] = stored.token
        await loadCatalog(stored.token)
        return { token: stored.token, user }
      }
      clearToken(userDataDir) // definitively rejected (revoked device, epoch bump)
    } catch {
      // Network failure: offline grace. The gateway is the real enforcement
      // point — a revoked token still dies there on the next request.
      process.env[CLOUD_TOKEN_ENV] = stored.token
      return { token: stored.token }
    }
  }

  const outcome = await runLoginWindow()
  if (outcome === undefined) return undefined
  saveToken(userDataDir, outcome.token, outcome.user.email)
  process.env[CLOUD_TOKEN_ENV] = outcome.token
  await loadCatalog(outcome.token)
  return outcome
}

/**
 * Cordis patch rows appended after prepareDesktopProfile()'s own pushes, so
 * they win over bundle, profile, and machine-level layers (last write wins per
 * row). Row ids and config fields are upstream contract points — see
 * docs/compatibility.md; desktop/scripts/verify-contract.mjs asserts them on
 * every upstream bump.
 *
 * Typed loosely on purpose: PatchOptions lives in @deepseek-ai/dsh-app-boot
 * and tracking its exact shape here would couple us to internals we don't use.
 */
export function cloudProfilePatches(): { id: string, disabled?: boolean, config?: object }[] {
  return [
    {
      // OpenAI-compatible chat completions -> our gateway. The user token is
      // the "API key"; the upstream provider key never reaches this machine.
      id: 'llm-deepseek',
      config: {
        baseURL: `${CLOUD_BASE}/llm/v1`,
        apiKeyEnv: CLOUD_TOKEN_ENV,
      },
    },
    {
      // web_search speaks Anthropic Messages on a SEPARATE endpoint; without
      // this row it would leak to the official API with a useless token.
      id: 'web-search-deepseek',
      config: {
        baseURL: `${CLOUD_BASE}/llm/anthropic/v1`,
        apiKeyEnv: CLOUD_TOKEN_ENV,
      },
    },
    // 网关有整整一份目录 (2026-08-19: 20 个模型 —— claude / gpt / gemini / grok /
    // kimi / glm / qwen / minimax / deepseek …), 但上面那行 llm-deepseek 用的是
    // **上游内置的 deepseek 清单**, 它不会去调 /llm/v1/models 做发现。结果桌面端
    // 开箱只能选到其中 2 个, 用户想用别的只能自己去「添加自定义提供方」手配一个
    // 网关 —— 这正是老板一直配着千面的原因, 他是在绕过这个缺陷, 而不是需要千面。
    //
    // 这里把网关声明成 pi-ai 的一条 hand-declared 路由 (pi-ai 不认识我们的端点,
    // 所以端点/协议/模型都要自己给全), 模型清单用启动时拉到的真实目录, 服务端
    // 上下架模型不需要用户换客户端。
    //
    // ⚠️ 出包后必须实测一件事: patch 层是「整 row config 替换」(docs/compatibility.md),
    // 所以这一行会不会盖掉用户自己在设置里加的自定义提供方 (settings.yaml 的
    // llm-pi-ai.providers) —— 用户层通常优先于 profile 层, 但没实证过就不能当真。
    // 若确实会盖掉, 改用独立 row id 挂第二个 pi-ai 实例 (README 明说同一 seam 可以
    // 并排挂多条路径, 只要提供方路由名不冲突)。
    //
    // 拉不到目录时 (离线 / 网关故障) 整行不注入 —— 宁可维持现状只有 deepseek 可选,
    // 也不要注入一个模型列表为空的提供方, 那会让模型选择器变成一个空壳。
    ...discoveredModels.length === 0 ? [] : [{
      id: 'llm-pi-ai',
      config: {
        providers: {
          'dsh-cloud': {
            displayName: 'DSH Cloud',
            apiKeyEnv: CLOUD_TOKEN_ENV,
            api: 'openai-completions',
            baseURL: `${CLOUD_BASE}/llm/v1`,
            models: discoveredModels,
          },
        },
      },
    }],
    {
      // Upstream's telemetry defaults to DISABLED; pin the row off so a future
      // upstream default flip cannot ship user session events to a collector.
      id: 'session-telemetry-otel',
      disabled: true,
    },
  ]
}
