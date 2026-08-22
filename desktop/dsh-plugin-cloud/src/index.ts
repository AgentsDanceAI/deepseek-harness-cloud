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
 * 操作系统只允许由用户触发的深链可靠地将应用带到前台。后台主动调用 focus
 * 不能替代这个激活信号。
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
  // 目录拉到了才敢禁上游那行 —— 见下面两个分支的说明。
  const catalogReady = discoveredModels.length > 0
  return [
    {
      // web_search speaks Anthropic Messages on a SEPARATE endpoint; without
      // this row it would leak to the official API with a useless token.
      id: 'web-search-deepseek',
      config: {
        baseURL: `${CLOUD_BASE}/llm/anthropic/v1`,
        apiKeyEnv: CLOUD_TOKEN_ENV,
      },
    },
    {
      // Upstream's telemetry defaults to DISABLED; pin the row off so a future
      // upstream default flip cannot ship user session events to a collector.
      id: 'session-telemetry-otel',
      disabled: true,
    },
    ...catalogReady
      ? [
          {
            // Once the catalog is available, pi-ai owns the complete model list.
            // Disable the built-in row to avoid duplicate entries for the same
            // gateway-backed models.
            id: 'llm-deepseek',
            disabled: true,
          },
          {
            // 默认模型必须跟着一起改。base bundle 的默认是
            // `provider: deepseek-official / model: deepseek-v4-flash`, 而
            // deepseek-official is supplied only by llm-deepseek. Disabling that
            // row requires a valid cloud default so the provider reference is not
            // left dangling.
            //
            // 插件 config 只是**底座**: 用户在 UI 里选过之后, settings provider 会
            // 把用户的选择层叠在上面 (见 dsh-agent-default-model 的 README), 所以
            // 这里写死一个默认不会抢掉任何人已有的选择。
            //
            // ⚠️ 该服务**不校验**模型是否在目录里 —— kimi-k3 若哪天下架, 这里不会
            // 报错, 只会在用户真正发第一条消息时才暴露。换默认模型时要对着
            // /api/models 确认 id 还在。
            id: 'agent-default-model',
            config: { provider: 'dsh-cloud', model: 'kimi-k3' },
          },
          {
            // 网关声明成 pi-ai 的一条 hand-declared 路由 (pi-ai 不认识我们的端点,
            // 所以端点/协议/模型都要自己给全)。模型清单用启动时拉到的真实目录,
            // 服务端上下架模型不需要用户换客户端 —— 写死必然漂移。
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
          },
        ]
      : [
          {
            // In fallback mode llm-deepseek remains enabled and pi-ai is absent,
            // so restore the base bundle's default provider.
            id: 'agent-default-model',
            config: { provider: 'deepseek-official', model: 'deepseek-v4-flash' },
          },
          {
            // If the catalog is unavailable, keep the built-in row enabled and
            // route it through the gateway. This preserves a usable model set.
            id: 'llm-deepseek',
            config: {
              baseURL: `${CLOUD_BASE}/llm/v1`,
              apiKeyEnv: CLOUD_TOKEN_ENV,
            },
          },
        ],
  ]
}
