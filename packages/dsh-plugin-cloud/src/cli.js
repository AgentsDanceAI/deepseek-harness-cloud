#!/usr/bin/env node
/** dsh-plugin-cloud CLI — login / setup / status / logout.
 *
 * `setup` is the one-command onboarding: device login (RFC 8628 style, same
 * flow the DSH Cloud desktop uses) → fetch the live model catalog → write the
 * plugin row and the gateway provider row into $DSH_HOME/cordis.patch.yml.
 */

import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { deviceStart, devicePoll, fetchModels, validateToken } from './api.js'
import { CLOUD_BASE, TOKEN_ENV, dshHome, homePatchPath } from './config.js'
import { clearAuthSync, loadAuthSync, saveAuthSync } from './store.js'
import { pluginRow, providerRow, writeManagedRows } from './patch.js'

const VERSION = createRequire(import.meta.url)('../package.json').version

const USAGE = `dsh-plugin-cloud ${VERSION} — connect stock DeepSeek Harness to DSH Cloud

Usage: dsh-plugin-cloud <command>

Commands:
  setup    log in (if needed), fetch the model catalog, and write the
           provider into $DSH_HOME/cordis.patch.yml — then restart dsh
  login    device login only (opens the browser, waits for approval)
  status   show the current session and configuration target
  logout   delete the locally stored token

Environment:
  DSH_CLOUD_BASE   service base URL (default https://dshcloud.online)
  DSH_HOME         DeepSeek Harness home (default ~/.dsh)

DSH Cloud is a commercial hosted service by AgentsDance AI.
New accounts include 500 free credits. https://dshcloud.online`

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** Best-effort browser open; the URL is always printed regardless. */
function openBrowser(url) {
  const cmd = process.platform === 'darwin' ? 'open'
    : process.platform === 'win32' ? 'start'
      : 'xdg-open'
  try {
    spawn(cmd, [url], { stdio: 'ignore', detached: true, shell: process.platform === 'win32' }).unref()
  } catch {
    // printing the URL is the fallback
  }
}

async function currentUser() {
  const auth = loadAuthSync()
  if (!auth?.token) return undefined
  try {
    const user = await validateToken(auth.token)
    return user ? { token: auth.token, user } : undefined
  } catch {
    // network trouble: trust the stored token; the gateway is the arbiter
    return { token: auth.token, user: auth.user }
  }
}

async function login() {
  const existing = await currentUser()
  if (existing) {
    console.log(`already logged in as ${existing.user?.email ?? 'unknown'}`)
    return existing
  }
  const start = await deviceStart({
    name: 'dsh-plugin-cloud',
    platform: process.platform,
    appVersion: VERSION,
  })
  console.log(`\nOpen to approve this device (code ${start.user_code}):\n\n  ${start.verification_url}\n`)
  openBrowser(start.verification_url)
  const intervalMs = Math.max(1, Number(start.interval) || 3) * 1000
  const deadline = Date.now() + (Number(start.expires_in) || 600) * 1000
  process.stdout.write('waiting for approval ')
  while (Date.now() < deadline) {
    await sleep(intervalMs)
    const result = await devicePoll(start.device_code)
    if (result.status === 'pending') {
      process.stdout.write('.')
      continue
    }
    process.stdout.write('\n')
    if (result.status === 'denied') throw new Error('login denied in the browser')
    saveAuthSync({ token: result.token, user: result.user })
    console.log(`logged in as ${result.user?.email ?? 'unknown'}`)
    return { token: result.token, user: result.user }
  }
  process.stdout.write('\n')
  throw new Error('login code expired (10 minutes) — run the command again')
}

async function setup() {
  const session = await login()
  const models = await fetchModels(session.token)
  if (models.length === 0) throw new Error('the gateway returned no models — try again later')
  const path = homePatchPath()
  const backup = writeManagedRows(path, [pluginRow(), providerRow(models)])
  console.log(`\nwrote ${models.length} models into ${path}`)
  if (backup) console.log(`previous file backed up to ${backup}`)
  console.log(`\nDone. Restart DeepSeek Harness and pick a model under “DSH Cloud”.`)
  console.log(`Re-run \`npx dsh-plugin-cloud setup\` any time to refresh the catalog.`)
}

async function status() {
  console.log(`service:  ${CLOUD_BASE}`)
  console.log(`dsh home: ${dshHome()}`)
  const auth = loadAuthSync()
  if (!auth?.token) {
    console.log('session:  not logged in')
    return
  }
  if (process.env[TOKEN_ENV]) console.log(`env:      ${TOKEN_ENV} already set (takes precedence)`)
  try {
    const user = await validateToken(auth.token)
    console.log(user ? `session:  ${user.email}` : 'session:  stored token was revoked — run setup again')
  } catch {
    console.log(`session:  stored for ${auth.user?.email ?? 'unknown'} (service unreachable right now)`)
  }
}

function logout() {
  console.log(clearAuthSync() ? 'local token deleted' : 'nothing to delete')
  console.log('to revoke the device server-side too: https://dshcloud.online/ → devices')
}

const command = process.argv[2]
try {
  if (command === 'setup') await setup()
  else if (command === 'login') await login()
  else if (command === 'status') await status()
  else if (command === 'logout') logout()
  else if (command === '--version' || command === '-v') console.log(VERSION)
  else {
    console.log(USAGE)
    if (command !== undefined && command !== '--help' && command !== '-h') process.exitCode = 1
  }
} catch (error) {
  console.error(`error: ${error instanceof Error ? error.message : String(error)}`)
  process.exitCode = 1
}
