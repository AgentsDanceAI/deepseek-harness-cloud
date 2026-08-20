/** Main-process HTTP client for the DSH Cloud account service.
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
 * 为什么动态拉而不是把清单写死在客户端: 目录是服务端的
 * (server/config 下的 catalog), 服务端上/下架模型不该要求用户换客户端。
 * 写死必然漂移 —— 2026-08-19 桌面端只能用到 20 个模型里的 2 个, 根子就是
 * 客户端只认上游内置的那份 deepseek 清单。
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
