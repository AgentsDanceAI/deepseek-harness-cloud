/** The login wall window: local HTML only, all network in the main process.
 *
 * The renderer is sandboxed (contextIsolation, no nodeIntegration) and talks
 * to the main process through the small IPC surface registered here. Remote
 * content is never loaded before authentication.
 */

import { app, BrowserWindow, ipcMain, shell } from 'electron'
import { hostname } from 'node:os'
import { fileURLToPath } from 'node:url'
import {
  deviceLogin,
  devicePoll,
  deviceStart,
  type CloudUser,
} from './api.ts'

export interface LoginResult {
  token: string
  user: CloudUser
}

const IPC_CHANNELS = [
  'dsh-cloud:device-start',
  'dsh-cloud:open-url',
  'dsh-cloud:password-login',
  'dsh-cloud:quit',
] as const

/** Shows the login wall and resolves with a token, or undefined if the user
 * closed the window (caller quits the app). */
export async function runLoginWindow(): Promise<LoginResult | undefined> {
  const clientInfo = {
    name: hostname(),
    platform: process.platform,
    appVersion: app.getVersion(),
  }

  const window = new BrowserWindow({
    width: 440,
    height: 600,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    autoHideMenuBar: true,
    title: 'DSH Cloud',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      preload: fileURLToPath(new URL('../build/cloud/login-preload.cjs', import.meta.url)),
    },
  })
  window.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  let settled = false
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  let pollGeneration = 0

  const result = await new Promise<LoginResult | undefined>((resolve) => {
    const finish = (value: LoginResult | undefined): void => {
      if (settled) return
      settled = true
      pollGeneration += 1
      if (pollTimer !== undefined) clearTimeout(pollTimer)
      // Device authorization completes in a browser. Bring the desktop app back
      // to the foreground after success; macOS requires the `steal` option.
      if (value !== undefined) {
        try {
          if (process.platform === 'darwin') app.focus({ steal: true })
          else app.focus()
        } catch { /* 抢焦点失败不该影响登录本身 */ }
      }
      resolve(value)
    }

    const sendStatus = (status: string, detail: string = ''): void => {
      if (!window.isDestroyed()) window.webContents.send('dsh-cloud:status', { status, detail })
    }

    ipcMain.handle('dsh-cloud:device-start', async () => {
      const started = await deviceStart(clientInfo)
      const generation = ++pollGeneration
      const poll = async (): Promise<void> => {
        if (settled || generation !== pollGeneration) return
        try {
          const state = await devicePoll(started.device_code)
          if (state.status === 'approved') {
            sendStatus('approved')
            finish({ token: state.token, user: state.user })
            return
          }
          if (state.status === 'denied') {
            sendStatus('denied')
            return
          }
        } catch (cause) {
          sendStatus('error', cause instanceof Error ? cause.message : String(cause))
        }
        pollTimer = setTimeout(() => { void poll() }, started.interval * 1000)
      }
      pollTimer = setTimeout(() => { void poll() }, started.interval * 1000)
      return { userCode: started.user_code, verificationUrl: started.verification_url }
    })

    ipcMain.handle('dsh-cloud:open-url', (_event, url: string) => {
      // Only our own activation URLs leave the app from this window.
      if (typeof url === 'string' && /^https?:\/\//.test(url)) void shell.openExternal(url)
    })

    ipcMain.handle('dsh-cloud:password-login', async (_event, input: { email: string, password: string }) => {
      try {
        const outcome = await deviceLogin({ ...clientInfo, email: input.email, password: input.password })
        finish(outcome)
        return { ok: true }
      } catch (cause) {
        return { ok: false, error: cause instanceof Error ? cause.message : String(cause) }
      }
    })

    ipcMain.handle('dsh-cloud:quit', () => { finish(undefined) })
    window.on('closed', () => { finish(undefined) })

    void window.loadFile(fileURLToPath(new URL('../build/cloud/login.html', import.meta.url)))
  })

  for (const channel of IPC_CHANNELS) ipcMain.removeHandler(channel)
  if (!window.isDestroyed()) window.destroy()
  return result
}
