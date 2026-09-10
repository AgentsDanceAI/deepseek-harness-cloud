/** 货架窗口: 打开桌面版先看见 16 格, 而不是直接掉进 DeepSeek Harness 一个 agent。
 *
 * 登录成功后由 cloudGate() 顺手开出来, **不阻塞启动** —— DSH 照常在后面起, 它只是
 * 货架上的第一格。所以这个文件没有给上游增加任何新的调用点 (0003-cloud-gate 仍是
 * 唯一那个补丁, 仍是那两处), 上游 bump 的摩擦不变。
 *
 * 渲染进程是沙箱的 (contextIsolation, 无 nodeIntegration), 网络与 docker 全在主
 * 进程。令牌**从不进渲染进程**: 界面只发"起哪一格", 主进程自己去拿计划、自己替换
 * 占位符。
 */

import { BrowserWindow, ipcMain, shell } from 'electron'
import { fileURLToPath } from 'node:url'
import { fetchLocalCatalog, fetchLocalPlan, type LocalCatalogEntry } from './api.ts'
import { dockerReady, freePort, pull, running, start, stop } from './local-runner.ts'

const IPC_CHANNELS = [
  'dsh-cloud:shelf-list',
  'dsh-cloud:shelf-start',
  'dsh-cloud:shelf-stop',
  'dsh-cloud:shelf-open',
] as const

/** 已经起来的格子映射到本机端口 —— 界面据此把「启动」换成「打开」。 */
const openPorts = new Map<string, number>()

interface ShelfRow extends LocalCatalogEntry {
  /** 正在本机跑, 且我们知道它开在哪个端口。 */
  port_local?: number
}

function openWorkspace(url: string, title: string): void {
  const view = new BrowserWindow({
    width: 1180,
    height: 820,
    title,
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  })
  void view.loadURL(url)
}

export function openShelf(token: string): BrowserWindow {
  const window = new BrowserWindow({
    width: 940,
    height: 700,
    title: 'AI Store',
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      preload: fileURLToPath(new URL('../build/cloud/shelf-preload.cjs', import.meta.url)),
    },
  })
  // 货架里的外链一律交给系统浏览器, 不在应用里开第二个可导航的窗口。
  window.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  const say = (channel: string, payload: unknown): void => {
    if (!window.isDestroyed()) window.webContents.send(channel, payload)
  }

  ipcMain.handle('dsh-cloud:shelf-list', async (): Promise<{
    docker: boolean, products: ShelfRow[], error?: string,
  }> => {
    const docker = await dockerReady()
    try {
      const products = await fetchLocalCatalog(token)
      const live = new Set((await running()).map(r => r.product))
      for (const id of [...openPorts.keys()]) if (!live.has(id)) openPorts.delete(id)
      return {
        docker,
        products: products.map(p => {
          // 先取到局部再判 —— exactOptionalPropertyTypes 下, 展开一个可能是
          // undefined 的值和"这个键不存在"是两回事。
          const port = openPorts.get(p.id)
          return port === undefined ? { ...p } : { ...p, port_local: port }
        }),
      }
    } catch (cause) {
      return { docker, products: [], error: cause instanceof Error ? cause.message : String(cause) }
    }
  })

  ipcMain.handle('dsh-cloud:shelf-start', async (_event, productId: string) => {
    try {
      const plan = await fetchLocalPlan(token, productId)
      if (plan.runnable !== 'ready') return { ok: false, error: plan.reason }
      const image = plan.containers.find(c => c.role === 'main')?.image_ref ?? ''
      say('dsh-cloud:shelf-progress', { id: productId, line: `拉镜像 ${image}` })
      await pull(image, line => { say('dsh-cloud:shelf-progress', { id: productId, line }) })
      const port = await freePort(plan.port)
      say('dsh-cloud:shelf-progress', { id: productId, line: '起容器…' })
      await start(plan, token, port)
      openPorts.set(productId, port)
      // 开根路径, 不是 ready_path —— 后者是给探针用的 (codex 那格是
      // /api/health), 直接开会给用户看一段 JSON。
      openWorkspace(`http://127.0.0.1:${port}`, plan.name)
      return { ok: true, port }
    } catch (cause) {
      return { ok: false, error: cause instanceof Error ? cause.message : String(cause) }
    }
  })

  ipcMain.handle('dsh-cloud:shelf-stop', async (_event, productId: string) => {
    await stop(productId)
    openPorts.delete(productId)
    return { ok: true }
  })

  ipcMain.handle('dsh-cloud:shelf-open', (_event, productId: string) => {
    const port = openPorts.get(productId)
    if (port !== undefined) openWorkspace(`http://127.0.0.1:${port}`, productId)
    return { ok: port !== undefined }
  })

  window.on('closed', () => {
    for (const channel of IPC_CHANNELS) ipcMain.removeHandler(channel)
  })
  void window.loadFile(fileURLToPath(new URL('../build/cloud/shelf.html', import.meta.url)))
  return window
}
