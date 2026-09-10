/** Main-process HTTP client for the AI Store account service.
 *
 * Uses Electron's net.fetch (Chromium network stack: system proxy + system
 * certificates). Only ever called after app.whenReady().
 */

import { net } from 'electron'
import { CLOUD_BASE } from './config.ts'

export interface CloudUser {
  id: string
  email: string
  display_name: string
}

export interface DeviceStart {
  device_code: string
  user_code: string
  verification_url: string
  expires_in: number
  interval: number
}

export class CloudApiError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(`dsh-cloud: ${status} ${code}`)
  }
}

async function request(path: string, options: { method?: string, token?: string, body?: unknown } = {}):
Promise<{ status: number, json: Record<string, unknown> }> {
  const headers: Record<string, string> = { 'content-type': 'application/json' }
  if (options.token !== undefined) headers.authorization = `Bearer ${options.token}`
  const response = await net.fetch(`${CLOUD_BASE}${path}`, {
    method: options.method ?? (options.body === undefined ? 'GET' : 'POST'),
    headers,
    ...options.body === undefined ? {} : { body: JSON.stringify(options.body) },
  })
  let json: Record<string, unknown> = {}
  try {
    json = await response.json() as Record<string, unknown>
  } catch {
    // non-JSON error bodies are fine; status carries the signal
  }
  return { status: response.status, json }
}

/** Validates a stored token. Returns the user, undefined when rejected (401/403),
 * or throws on network failure so callers can apply the offline grace path. */
export async function validateToken(token: string): Promise<CloudUser | undefined> {
  const { status, json } = await request('/api/auth/me', { token })
  if (status === 200) return (json.user as CloudUser | undefined)
  if (status === 401 || status === 403) return undefined
  throw new CloudApiError(status, 'unexpected_status')
}

export async function deviceStart(info: { name: string, platform: string, appVersion: string }):
Promise<DeviceStart> {
  const { status, json } = await request('/api/device/start', {
    body: { name: info.name, platform: info.platform, app_version: info.appVersion },
  })
  if (status !== 200) throw new CloudApiError(status, String(json.detail ?? 'device_start_failed'))
  return json as unknown as DeviceStart
}

export type DevicePollResult =
  | { status: 'pending' }
  | { status: 'denied' }
  | { status: 'approved', token: string, user: CloudUser }

export async function devicePoll(deviceCode: string): Promise<DevicePollResult> {
  const { status, json } = await request('/api/device/poll', { body: { device_code: deviceCode } })
  if (status === 429) return { status: 'pending' }
  if (status !== 200) throw new CloudApiError(status, String(json.detail ?? 'device_poll_failed'))
  return json as unknown as DevicePollResult
}

/** In-window email+password fallback: mints a device token in one call. */
export async function deviceLogin(input: {
  email: string, password: string, name: string, platform: string, appVersion: string,
}): Promise<{ token: string, user: CloudUser }> {
  const { status, json } = await request('/api/device/login', {
    body: {
      email: input.email,
      password: input.password,
      name: input.name,
      platform: input.platform,
      app_version: input.appVersion,
    },
  })
  if (status !== 200) throw new CloudApiError(status, String(json.detail ?? 'login_failed'))
  return json as unknown as { token: string, user: CloudUser }
}

/** 网关目录里的一个模型, 已转成 pi-ai profile 要的形状。 */
export interface CloudModel {
  id: string
  name: string
  contextWindow?: number
}

/**
 * 拉取网关**当前**提供的模型目录 (GET /llm/v1/models, pi-ai discovery 兼容)。
 *
 * The server owns the catalog, so adding or removing a model must not require a
 * desktop release. Fetching it at startup prevents a stale client-side list.
 */
export async function fetchModels(token: string): Promise<CloudModel[]> {
  const { status, json } = await request('/llm/v1/models', { token })
  if (status !== 200) throw new CloudApiError(status, 'models_unavailable')
  const rows = Array.isArray(json.data) ? json.data : []
  const models: CloudModel[] = []
  for (const row of rows) {
    const entry = row as Record<string, unknown>
    const id = typeof entry.id === 'string' ? entry.id : ''
    if (id === '') continue
    const name = typeof entry.display_name === 'string' && entry.display_name !== ''
      ? entry.display_name
      : id
    const ctx = typeof entry.context_window === 'number' && entry.context_window > 0
      ? entry.context_window
      : undefined
    models.push({ id, name, ...ctx === undefined ? {} : { contextWindow: ctx } })
  }
  return models
}

// ── 本机工作台: 目录与启动计划都由服务端下发 ────────────────────────────────
//
// 客户端**不留第二份目录**。products.py 已经把 16 格的完整拓扑写成数据, 而镜像
// tag 一直在动 (2026-09-10 一天重建了五个) —— 客户端抄一份的下场不是报错, 是拉到
// 一个过期镜像然后一切"正常"。这里只搬运。

/** 目录里的一格: 能不能在本机跑, 不能的话为什么。 */
export interface LocalCatalogEntry {
  id: string
  name: string
  port: number
  mem_mb: number
  /** 'ready' | 'runner_no_stack' —— 只谈**运行器**起不起得动。 */
  runnable: string
  reason: string
  /** 上了锁而这个账号没有通行证。与 runnable 是两件事: 11 个容器的栈, 买了也起不动。 */
  locked: boolean
  containers: number
}

export interface PlanContainer {
  role: 'init' | 'main' | 'sidecar'
  name: string
  image_ref: string
  cmd: string[]
  args: string[]
  env: Record<string, string>
  /** 'own' | 'share:main' —— 伴随容器共享主容器的网络命名空间 (k8s pod 语义),
   *  上游那些写死 127.0.0.1 的配置全靠这一条成立。 */
  network: string
  port?: number
  run_as_user?: number | null
}

export interface LocalPlan {
  product: string
  name: string
  port: number
  ready_path: string
  gateway: string
  /** 计划里凡是该填令牌的地方都是这个串。计划本身不含凭据。 */
  token_placeholder: string
  runnable: string
  reason: string
  host_aliases: string[]
  seeds: Array<[string, string]>
  containers: PlanContainer[]
}

export async function fetchLocalCatalog(token: string): Promise<LocalCatalogEntry[]> {
  const { status, json } = await request('/api/local/catalog', { token })
  if (status !== 200) throw new CloudApiError(status, 'local_catalog_unavailable')
  return Array.isArray(json.products) ? json.products as LocalCatalogEntry[] : []
}

export async function fetchLocalPlan(token: string, productId: string): Promise<LocalPlan> {
  const { status, json } = await request(`/api/local/plan/${encodeURIComponent(productId)}`, { token })
  // 402 = 这一格上了锁而这个账号没买通行证。调用方要把它和"起不动"分开显示。
  if (status !== 200) throw new CloudApiError(status, String(json.detail ?? 'local_plan_unavailable'))
  return json as unknown as LocalPlan
}
