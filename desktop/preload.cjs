const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld(
  "lf2s",
  Object.freeze({
    state: () => ipcRenderer.invoke("station:state"),
    connect: () => ipcRenderer.invoke("station:connect"),
    disconnect: () => ipcRenderer.invoke("station:disconnect"),
    profile: () => ipcRenderer.invoke("station:profile"),
    taskStart: (confirmed) =>
      ipcRenderer.invoke("station:taskStart", confirmed),
    taskCancel: () => ipcRenderer.invoke("station:taskCancel"),
    taskQuery: () => ipcRenderer.invoke("station:taskQuery"),
    recordings: () => ipcRenderer.invoke("station:recordings"),
    replayOpen: (id, index = 0) =>
      ipcRenderer.invoke("station:replayOpen", id, index),
    replaySeek: (id, index) =>
      ipcRenderer.invoke("station:replaySeek", id, index),
    replayClose: () => ipcRenderer.invoke("station:replayClose"),
    scenario: (name) => ipcRenderer.invoke("station:scenario", name),
    image: () => ipcRenderer.invoke("station:image"),
    benchConnect: (host, port) =>
      ipcRenderer.invoke("station:benchConnect", host, port),
    benchDisconnect: () => ipcRenderer.invoke("station:benchDisconnect"),
    benchFocus: () => ipcRenderer.invoke("station:benchFocus"),
    benchPreview: () => ipcRenderer.invoke("station:benchPreview"),
    restart: () => ipcRenderer.invoke("station:restart"),
    openLogs: () => ipcRenderer.invoke("station:open-logs"),
    onState: (callback) => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on("station:state", listener);
      return () => ipcRenderer.removeListener("station:state", listener);
    },
  }),
);
