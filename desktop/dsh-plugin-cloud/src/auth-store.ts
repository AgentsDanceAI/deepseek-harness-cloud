/** Encrypted-at-rest device token storage under Electron userData.
 *
 * safeStorage (Keychain / DPAPI / kwallet-libsecret) when available; falls
 * back to a 0600 plaintext file on locked-down Linux setups, recorded in the
 * envelope so a later run does not misinterpret the bytes.
 */

import { safeStorage } from 'electron'
import { mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { AUTH_STORE_FILENAME } from './config.ts'

interface Envelope {
  version: 1
  cipher: 'safeStorage' | 'plain'
  token: string
  email?: string
}

function storePath(userDataDir: string): string {
  return join(userDataDir, AUTH_STORE_FILENAME)
}

export function loadToken(userDataDir: string): { token: string, email?: string } | undefined {
  let raw: string
  try {
    raw = readFileSync(storePath(userDataDir), 'utf8')
  } catch {
    return undefined
  }
  try {
    const envelope = JSON.parse(raw) as Envelope
    if (envelope.cipher === 'safeStorage') {
      const token = safeStorage.decryptString(Buffer.from(envelope.token, 'base64'))
      return { token, email: envelope.email }
    }
    return { token: envelope.token, email: envelope.email }
  } catch {
    // Corrupt or undecryptable (e.g. OS keying changed): treat as logged out.
    clearToken(userDataDir)
    return undefined
  }
}

export function saveToken(userDataDir: string, token: string, email?: string): void {
  const canEncrypt = safeStorage.isEncryptionAvailable()
  const envelope: Envelope = {
    version: 1,
    cipher: canEncrypt ? 'safeStorage' : 'plain',
    token: canEncrypt ? safeStorage.encryptString(token).toString('base64') : token,
    ...email === undefined ? {} : { email },
  }
  const path = storePath(userDataDir)
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, JSON.stringify(envelope), { mode: 0o600 })
}

export function clearToken(userDataDir: string): void {
  try {
    rmSync(storePath(userDataDir), { force: true })
  } catch {
    // best effort
  }
}
