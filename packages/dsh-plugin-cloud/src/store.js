/** Token store under the dsh home. Plaintext with 0600 (same trust boundary as
 * ~/.dsh itself — anyone who can read this directory can already read every
 * conversation). Synchronous variants exist because the cordis plugin's apply()
 * runs before any async work is welcome. */

import { chmodSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { authStorePath } from './config.js'

export function loadAuthSync() {
  try {
    const parsed = JSON.parse(readFileSync(authStorePath(), 'utf8'))
    return typeof parsed?.token === 'string' && parsed.token !== '' ? parsed : undefined
  } catch {
    return undefined
  }
}

export function saveAuthSync(auth) {
  const path = authStorePath()
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, JSON.stringify({ ...auth, saved_at: new Date().toISOString() }, null, 2) + '\n')
  chmodSync(path, 0o600)
}

export function clearAuthSync() {
  try {
    unlinkSync(authStorePath())
    return true
  } catch {
    return false
  }
}
