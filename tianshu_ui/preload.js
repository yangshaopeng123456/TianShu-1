// 预加载脚本：通过 contextBridge 暴露窗口控制 API
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('tianshu', {
  minimize: () => ipcRenderer.send('window:minimize'),
  maximize: () => ipcRenderer.send('window:maximize'),
  close: () => ipcRenderer.send('window:close'),
  isMaximized: () => ipcRenderer.sendSync('window:is-maximized'),
  onMaximizeChange: (cb) => ipcRenderer.on('window:maximized', (e, v) => cb(v)),
  onBackendReady: (cb) => ipcRenderer.on('backend:ready', (e, info) => cb(info)),
  onBackendProgress: (cb) => ipcRenderer.on('backend:progress', (e, info) => cb(info)),
  onBackendError: (cb) => ipcRenderer.on('backend:error', (e, message) => cb(message)),
  startTaskStream: (streamId) => ipcRenderer.invoke('task:stream:start', streamId),
  stopTaskStream: () => ipcRenderer.invoke('task:stream:stop'),
  onTaskEvent: (cb) => { ipcRenderer.on('task:event', (_e, payload) => cb(payload)); },
  apiRequest: (method, apiPath, body) => ipcRenderer.invoke('api:request', { method, path: apiPath, body }),
  uploadFile: (sessionId, filePaths, content) => ipcRenderer.invoke('api:upload', { sessionId, filePaths, content }),
  sendMessage: (sessionId, content) => ipcRenderer.invoke('api:message', { sessionId, content }),
  chooseFile: () => ipcRenderer.invoke('file:choose'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  saveSettings: (value) => ipcRenderer.invoke('settings:save', value),
  restartBackend: () => ipcRenderer.invoke('backend:restart'),
  openExternal: (url) => ipcRenderer.invoke('external:open', url),
});
