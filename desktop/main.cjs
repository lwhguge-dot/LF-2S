const { app, BrowserWindow, ipcMain, session, shell } = require("electron");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const readline = require("node:readline");
const http = require("node:http");

const root = path.resolve(__dirname, "..");
const uiFile = path.join(root, "build/ui/index.html");
const pageURL = pathToFileURL(uiFile).href;
app.setName("LF2SStation");
app.setPath(
  "userData",
  process.env.LF2S_STATION_USER_DATA ||
    path.join(app.getPath("appData"), "LF2SStation"),
);
const userData = app.getPath("userData");
let win, worker, base, token, pollTimer, latest;
let stopping = false,
  restarting = false,
  generation = 0;
let serviceError = "本地服务正在启动";

function desktopLog(text) {
  const folder = path.join(userData, "logs");
  fs.mkdirSync(folder, { recursive: true });
  const file = path.join(folder, "desktop.log");
  if (fs.existsSync(file) && fs.statSync(file).size > 512 * 1024) {
    fs.copyFileSync(file, file + ".1");
    fs.writeFileSync(file, "");
  }
  fs.appendFileSync(
    file,
    new Date().toISOString() + " " + String(text).slice(0, 4096) + "\n",
  );
}

function currentState() {
  if (serviceError)
    return {
      simulation: true,
      read_only: true,
      service_ready: false,
      service_error: serviceError,
      link: { phase: "service_error", reason: serviceError },
    };
  return (
    latest || {
      simulation: true,
      read_only: true,
      service_ready: false,
      service_error: "等待后台快照",
    }
  );
}
function publish() {
  if (win && !win.isDestroyed())
    win.webContents.send("station:state", currentState());
}
function validate(event) {
  if (
    !win ||
    event.sender !== win.webContents ||
    event.senderFrame !== win.webContents.mainFrame ||
    event.senderFrame?.url !== pageURL
  )
    throw new Error("不允许的界面调用来源");
}
async function request(route, method = "GET", body, binary = false) {
  if (!base || serviceError) throw new Error(serviceError || "本地服务未就绪");
  // Native loopback HTTP bypasses machine/Chromium proxy configuration. No configurable destination.
  const payload = body ? Buffer.from(JSON.stringify(body)) : undefined;
  const response = await new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: new URL(base).port,
        path: "/api/v1/" + route,
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          ...(payload
            ? {
                "Content-Type": "application/json",
                "Content-Length": payload.length,
              }
            : {}),
        },
      },
      (res) => {
        let length = 0;
        const chunks = [];
        res.on("data", (chunk) => {
          length += chunk.length;
          if (length > 2 * 1024 * 1024) {
            req.destroy(new Error("本地响应超过大小上限"));
            return;
          }
          chunks.push(chunk);
        });
        res.on("error", reject);
        res.on("end", () =>
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks),
          }),
        );
      },
    );
    req.setTimeout(4000, () => req.destroy(new Error("本地服务请求超时")));
    req.on("error", reject);
    req.end(payload);
  });
  if (response.status < 200 || response.status >= 300) {
    let data = {};
    try {
      data = JSON.parse(response.body.toString("utf8"));
    } catch {}
    throw new Error(data.error || `本地服务返回 ${response.status}`);
  }
  if (!binary) return JSON.parse(response.body.toString("utf8"));
  const length = Number(response.headers["content-length"]);
  if (
    !Number.isInteger(length) ||
    length < 4 ||
    length > 1024 * 1024 ||
    response.headers["content-type"] !== "image/jpeg" ||
    response.headers["x-lf2s-synthetic"] !== "1"
  )
    throw new Error("合成图像响应无效");
  const bytes = new Uint8Array(response.body);
  if (bytes.length !== length) throw new Error("合成图像不完整");
  return {
    bytes,
    frame_id: Number(response.headers["x-lf2s-frame-id"]),
    stream_id: response.headers["x-lf2s-stream-id"],
    synthetic: true,
    hardware_freshness_qualified: false,
    received_at: Date.now(),
  };
}

async function startWorker() {
  const myGeneration = ++generation;
  serviceError = "本地服务正在启动";
  base = undefined;
  latest = undefined;
  publish();
  token = randomBytes(32).toString("hex");
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, "backend/lf2s-service.exe")
    : path.join(root, ".venv/Scripts/python.exe");
  const args = app.isPackaged
    ? []
    : ["-B", path.join(root, "service/entry.py")];
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONUTF8: "1" };
  delete env.PYTHONPATH;
  delete env.PYTHONHOME;
  delete env.VIRTUAL_ENV;
  worker = spawn(executable, args, {
    windowsHide: true,
    stdio: ["pipe", "pipe", "pipe"],
    env,
    cwd: userData,
  });
  const owned = worker;
  owned.stdin.on("error", () => {});
  owned.stderr.on("data", (value) => desktopLog(value.toString()));
  owned.on("exit", (code, signal) => {
    if (myGeneration !== generation || stopping) return;
    clearTimeout(pollTimer);
    base = undefined;
    latest = undefined;
    serviceError = `本地服务已退出（${signal || code}），全部测量已失效。可重启服务。`;
    desktopLog(serviceError);
    publish();
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("本地服务启动超过 20 秒")),
      20000,
    );
    const reader = readline.createInterface({ input: owned.stdout });
    const fail = (error) => {
      clearTimeout(timer);
      reader.close();
      reject(error);
    };
    owned.once("error", fail);
    owned.once("exit", (code) =>
      fail(new Error(`本地服务未就绪就退出（${code}）`)),
    );
    reader.once("line", (line) => {
      try {
        const ready = JSON.parse(line);
        if (
          !ready.ready ||
          !ready.simulation ||
          ready.hardware_control_allowed !== false ||
          ready.api_version !== "1" ||
          !Number.isInteger(ready.port) ||
          ready.port < 1 ||
          ready.port > 65535 ||
          (app.isPackaged && !ready.frozen)
        )
          throw new Error("后台就绪握手不符合本机模拟边界");
        base = `http://127.0.0.1:${ready.port}`;
        clearTimeout(timer);
        reader.close();
        serviceError = null;
        desktopLog(JSON.stringify({ backend_ready: ready }));
        resolve();
      } catch (error) {
        fail(error);
      }
    });
    owned.stdin.write(JSON.stringify({ token, user_data: userData }) + "\n");
  });
  const health = await request("health");
  if (
    !health.ready ||
    health.hardware_control_allowed !== false ||
    !health.simulation
  )
    throw new Error("后台状态核对失败");
  async function poll() {
    if (myGeneration !== generation || stopping || serviceError) return;
    try {
      latest = await request("state");
      publish();
    } catch (error) {
      serviceError = "无法取得后台状态，全部测量已失效：" + error.message;
      publish();
    }
    if (!serviceError) pollTimer = setTimeout(poll, 250);
  }
  await poll();
}

async function stopWorker() {
  ++generation;
  clearTimeout(pollTimer);
  base = undefined;
  latest = undefined;
  const owned = worker;
  if (!owned || owned.exitCode !== null || owned.signalCode !== null) return;
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      owned.kill();
      resolve();
    }, 4500);
    owned.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    if (!owned.stdin.destroyed) owned.stdin.end("shutdown\n");
    else owned.kill();
  });
}

async function boot() {
  fs.mkdirSync(userData, { recursive: true });
  session.defaultSession.setPermissionRequestHandler(
    (_contents, _permission, callback) => callback(false),
  );
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
    const allowed =
      details.url.startsWith("file:") ||
      details.url.startsWith("blob:") ||
      details.url.startsWith("data:");
    callback({ cancel: !allowed });
  });
  win = new BrowserWindow({
    width: 1440,
    height: 980,
    minWidth: 700,
    minHeight: 650,
    title: "LF-2S 上位机 · 只读模拟版",
    backgroundColor: "#0b141c",
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
      webviewTag: false,
    },
  });
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event) => event.preventDefault());
  win.on("ready-to-show", () => win.show());
  ipcMain.handle("station:state", (event) => {
    validate(event);
    return currentState();
  });
  for (const [name, route] of [
    ["connect", "connect"],
    ["disconnect", "disconnect"],
    ["profile", "profile"],
    ["taskCancel", "tasks/cancel"],
    ["taskQuery", "tasks/query"],
    ["replayClose", "replay/close"],
  ]) {
    ipcMain.handle(`station:${name}`, (event) => {
      validate(event);
      return request(route, "POST");
    });
  }
  ipcMain.handle("station:scenario", (event, name) => {
    validate(event);
    if (
      ![
        "healthy",
        "unknown",
        "sensor_freeze",
        "link_pause",
        "bad_fingerprint",
        "wrong_source",
        "reboot",
        "task_bench",
        "manual_takeover",
        "position_reset",
        "result_loss",
      ].includes(name)
    )
      throw new Error("未定义的模拟场景");
    return request("simulation/scenario", "POST", { name });
  });
  ipcMain.handle("station:taskStart", (event, confirmed) => {
    validate(event);
    if (confirmed !== true) throw new Error("本次模拟许可尚未确认");
    return request("tasks/start", "POST", { confirmed: true });
  });
  ipcMain.handle("station:recordings", (event) => {
    validate(event);
    return request("recordings");
  });
  for (const name of ["replayOpen", "replaySeek"]) {
    ipcMain.handle(`station:${name}`, (event, id, index = 0) => {
      validate(event);
      if (
        typeof id !== "string" ||
        !/^[a-f0-9]{24}$/.test(id) ||
        !Number.isInteger(index) ||
        index < 0 ||
        index > 100000
      )
        throw new Error("回放编号或帧无效");
      return request(
        name === "replayOpen" ? "replay/open" : "replay/seek",
        "POST",
        { recording_id: id, index },
      );
    });
  }
  ipcMain.handle("station:image", (event) => {
    validate(event);
    return request("image", "GET", undefined, true);
  });
  ipcMain.handle("station:open-logs", (event) => {
    validate(event);
    return shell.openPath(path.join(userData, "logs"));
  });
  ipcMain.handle("station:restart", async (event) => {
    validate(event);
    if (restarting) throw new Error("正在重启本地服务");
    restarting = true;
    try {
      await stopWorker();
      await startWorker();
      return { restarted: true };
    } catch (error) {
      serviceError = "后台启动失败：" + error.message;
      publish();
      throw error;
    } finally {
      restarting = false;
    }
  });
  await win.loadFile(uiFile);
  try {
    await startWorker();
  } catch (error) {
    await stopWorker();
    serviceError = "后台启动失败：" + error.message;
    desktopLog(serviceError);
    publish();
  }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", (event) => {
    if (stopping) return;
    event.preventDefault();
    stopping = true;
    stopWorker().finally(() => app.quit());
  });
  app
    .whenReady()
    .then(boot)
    .catch((error) => {
      desktopLog(error.stack);
      app.quit();
    });
}
