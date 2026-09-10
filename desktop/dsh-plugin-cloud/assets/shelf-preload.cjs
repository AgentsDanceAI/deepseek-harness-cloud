/** Sandboxed preload for the AI Store shelf window.
 * 令牌不在这一层出现 —— 界面只说"起哪一格", 主进程自己去拿计划。 */
'use strict'

const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('aiStoreShelf', {
  list: () => ipcRenderer.invoke('dsh-cloud:shelf-list'),
  bootHost: () => ipcRenderer.invoke('dsh-cloud:shelf-boot-host'),
  start: id => ipcRenderer.invoke('dsh-cloud:shelf-start', id),
  stop: id => ipcRenderer.invoke('dsh-cloud:shelf-stop', id),
  open: id => ipcRenderer.invoke('dsh-cloud:shelf-open', id),
  onProgress: handler => {
    const listener = (_event, payload) => { handler(payload) }
    ipcRenderer.on('dsh-cloud:shelf-progress', listener)
    return () => ipcRenderer.removeListener('dsh-cloud:shelf-progress', listener)
  },
})
