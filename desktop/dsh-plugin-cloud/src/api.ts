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
