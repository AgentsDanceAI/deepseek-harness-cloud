/** HTTP client for the DSH Cloud account service and gateway catalog.
 * Endpoint contracts mirror desktop/dsh-plugin-cloud/src/api.ts (production-tested). */

import { CLOUD_BASE } from './config.js'

export class CloudApiError extends Error {
  constructor(status, code) {
    super(`dsh-cloud: ${status} ${code}`)
    this.status = status
    this.code = code
  }
}

async function request(path, { method, token, body } = {}) {
  const headers = { 'content-type': 'application/json' }
  if (token !== undefined) headers.authorization = `Bearer ${token}`
  const response = await fetch(`${CLOUD_BASE}${path}`, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    headers,
    ...body === undefined ? {} : { body: JSON.stringify(body) },
  })
  let json = {}
  try {
    json = await response.json()
  } catch {
    // non-JSON error bodies are fine; status carries the signal
  }
  return { status: response.status, json }
}

/** POST /api/device/start → {device_code, user_code, verification_url, expires_in, interval} */
export async function deviceStart(info) {
  const { status, json } = await request('/api/device/start', {
    body: { name: info.name, platform: info.platform, app_version: info.appVersion },
  })
  if (status !== 200) throw new CloudApiError(status, String(json.detail ?? 'device_start_failed'))
  return json
}

/** POST /api/device/poll → {status:'pending'|'denied'} | {status:'approved', token, user}.
 * 429 means "slow down" — treated as pending so the caller just waits another interval. */
export async function devicePoll(deviceCode) {
  const { status, json } = await request('/api/device/poll', { body: { device_code: deviceCode } })
  if (status === 429) return { status: 'pending' }
  if (status !== 200) throw new CloudApiError(status, String(json.detail ?? 'device_poll_failed'))
  return json
}

/** GET /api/auth/me → user, or undefined when the token is rejected (401/403). */
export async function validateToken(token) {
  const { status, json } = await request('/api/auth/me', { token })
  if (status === 200) return json.user
  if (status === 401 || status === 403) return undefined
  throw new CloudApiError(status, 'unexpected_status')
}

/** GET /llm/v1/models → models already in the shape the pi-ai profile wants. */
export async function fetchModels(token) {
  const { status, json } = await request('/llm/v1/models', { token })
  if (status !== 200) throw new CloudApiError(status, 'models_unavailable')
  return mapCatalog(Array.isArray(json.data) ? json.data : [])
}

/** Exported separately so the mapping is unit-testable without a network. */
export function mapCatalog(rows) {
  const models = []
  for (const entry of rows) {
    const id = typeof entry?.id === 'string' ? entry.id : ''
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
