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
            // 目录到手, 网关的全部模型都由下面那条 pi-ai 路由提供 —— 这时必须把
            // 上游内置的 deepseek 行**关掉**, 否则模型选择器里会并排出现「DeepSeek」
            // 和「DSH Cloud」两组, 而且同一个 DeepSeek-V4-Flash 在两边各来一次。
            //
            // 那两组看着像"官方 vs 我们", 实则**都是我们**: 这一行的 baseURL 早就被
            // 指到了网关, 用的是设备 token、扣的是我们的积分。让用户对着两个同名
            // 模型猜哪个是哪个, 是白白制造困惑 (2026-08-20 老板实测提出)。
            id: 'llm-deepseek',
            disabled: true,
          },
          {
            // 默认模型必须跟着一起改。base bundle 的默认是
            // `provider: deepseek-official / model: deepseek-v4-flash`, 而
            // deepseek-official 是 llm-deepseek 独占的路由 —— 我们在上面把那行
            // 关掉之后, 默认模型就指向了一个**不存在的 provider**: 客户端解析不到,
            // 于是首次启动直接"当前模型不可用", 输入框锁死, 新用户装完就卡在这里
            // (2026-08-20 老板全新安装实测)。这与 8-19 那次退掉千面后的死锁同一个
            // 模式 —— provider 没了, 指向它的默认值成了悬空引用。
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
            // 降级路径下默认模型必须**指回上游路由**: 这时 llm-deepseek 仍然启用、
            // pi-ai 那条不注入, 若默认还指着 dsh-cloud 就又成了悬空引用 —— 同一个
            // 坑换个方向再踩一次。写回 base bundle 的原值。
            id: 'agent-default-model',
            config: { provider: 'deepseek-official', model: 'deepseek-v4-flash' },
          },
          {
            // 降级路径: 目录没拉到 (离线 / 网关故障)。**绝不能**在这里禁掉这一行 ——
            // pi-ai 那条也不会注入, 两边都没了就一个模型都不剩, 而上游在无可用模型时
            // 会把输入框禁用, 用户连"换个模型"都打不出来 (2026-08-19 那次死锁就是
            // 这么来的)。所以退回原来的做法: 这一行仍指向网关, 至少内置的两个能用。
            id: 'llm-deepseek',
            config: {
              baseURL: `${CLOUD_BASE}/llm/v1`,
              apiKeyEnv: CLOUD_TOKEN_ENV,
            },
          },
        ],
  ]
}
