/** Sandboxed preload for the DSH Cloud login window.
 * Exposes the minimal bridge; all network stays in the main process. */
'use strict'

const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('dshCloud', {
  startDevice: () => ipcRenderer.invoke('dsh-cloud:device-start'),
  openUrl: url => ipcRenderer.invoke('dsh-cloud:open-url', url),
  passwordLogin: (email, password) => ipcRenderer.invoke('dsh-cloud:password-login', { email, password }),
  quit: () => ipcRenderer.invoke('dsh-cloud:quit'),
  onStatus: (handler) => {
    const listener = (_event, payload) => { handler(payload) }
    ipcRenderer.on('dsh-cloud:status', listener)
    return () => ipcRenderer.removeListener('dsh-cloud:status', listener)
  },
})
