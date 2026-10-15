const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld(
  "lf2s",
  Object.freeze({
    state: () => ipcRenderer.invoke("station:state"),
    connect: () => ipcRenderer.invoke("station:connect"),
    disconnect: () => ipcRenderer.invoke("station:disconnect"),
    profile: () => ipcRenderer.invoke("station:profile"),
    scenario: (name) => ipcRenderer.invoke("station:scenario", name),
    image: () => ipcRenderer.invoke("station:image"),
    restart: () => ipcRenderer.invoke("station:restart"),
    openLogs: () => ipcRenderer.invoke("station:open-logs"),
    onState: (callback) => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on("station:state", listener);
      return () => ipcRenderer.removeListener("station:state", listener);
    },
  }),
);
