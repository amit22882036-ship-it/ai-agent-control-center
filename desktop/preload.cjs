// The only desktop capabilities the interface gets: a native folder picker, facts about this
// installation, and opening the log folder. Nothing else from Node or Electron is exposed.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('controlCenterDesktop', {
  chooseFolder: () => ipcRenderer.invoke('control-center:choose-folder'),
  info: () => ipcRenderer.invoke('control-center:info'),
  openLogs: () => ipcRenderer.invoke('control-center:open-logs'),
})
