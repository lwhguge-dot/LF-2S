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
let microphoneUntil = 0;
let voiceQueue = Promise.resolve();

function voiceOperation(action) {
  const owner = generation;
  const pending = voiceQueue.then(() => {
    if (owner !== generation) throw new Error("旧语音请求已失效");
    return action();
  });
  voiceQueue = pending.catch(() => {});
  return pending;
}

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
async function request(route, method = "GET", body, binary = false, requestHeaders = {}) {
  if (!base || serviceError) throw new Error(serviceError || "本地服务未就绪");
  // Native loopback HTTP bypasses machine/Chromium proxy configuration. No configurable destination.
  const rawAudio = Buffer.isBuffer(body);
  const payload = body
    ? rawAudio
      ? body
      : Buffer.from(JSON.stringify(body))
    : undefined;
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
                "Content-Type": route === "vision/analyze"
                  ? "image/jpeg"
                  : rawAudio
                    ? "audio/wav"
                    : "application/json",
                "Content-Length": payload.length,
              }
            : {}),
          ...requestHeaders,
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
    req.setTimeout(
      route === "bench/connect"
        ? 25000
        : binary === "real" ||
        route.startsWith("bench/") ||
        route.startsWith("fc-usb/")
        ? 12000
        : 4000,
      () => req.destroy(new Error("本地服务请求超时")),
    );
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
  if (binary === "real") {
    const h = response.headers;
    const length = Number(h["content-length"]);
    const width = Number(h["x-lf2s-width"]);
    const height = Number(h["x-lf2s-height"]);
    const frame = Number(h["x-lf2s-frame-id"]);
    const epoch = Number(h["x-lf2s-epoch"]);
    const roundtrip = Number(h["x-lf2s-roundtrip-ms"]);
    const decode = Number(h["x-lf2s-decode-ms"]);
    const cameraWait =
      h["x-lf2s-camera-wait-ms"] === undefined
        ? null
        : Number(h["x-lf2s-camera-wait-ms"]);
    const serviceReceived = Number(h["x-lf2s-service-received-ms"]);
    const validU64 = (s) =>
      typeof s === "string" &&
      /^[1-9][0-9]{0,19}$/.test(s) &&
      BigInt(s) < 1n << 64n;
    if (
      !Number.isInteger(length) ||
      length < 4 ||
      length > 262144 ||
      response.body.length !== length ||
      h["content-type"] !== "image/jpeg" ||
      h["x-lf2s-synthetic"] !== "0" ||
      h["x-lf2s-display-only"] !== "1" ||
      h["x-lf2s-hardware-qualified"] !== "0" ||
      h["x-lf2s-capture-after-request"] !== "0" ||
      h["x-lf2s-test-pattern"] !== "0" ||
      ![roundtrip, decode, ...(cameraWait === null ? [] : [cameraWait])].every(
        (v) => Number.isFinite(v) && v >= 0 && v <= 20000,
      ) ||
      !validU64(h["x-lf2s-stream-id"]) ||
      !validU64(h["x-lf2s-capture-us"]) ||
      !Number.isInteger(frame) ||
      frame < 1 ||
      frame > 0xffffffff ||
      !Number.isSafeInteger(epoch) ||
      epoch < 1 ||
      !Number.isSafeInteger(serviceReceived) ||
      serviceReceived < 1 ||
      ![width, height].every((v) => Number.isInteger(v) && v >= 1 && v <= 1600)
    )
      throw new Error("实物图像响应无效");
    return {
      bytes: new Uint8Array(response.body),
      frame_id: frame,
      epoch,
      width,
      height,
      stream_id: h["x-lf2s-stream-id"],
      capture_us: h["x-lf2s-capture-us"],
      synthetic: false,
      display_only: true,
      hardware_freshness_qualified: false,
      received_at: Date.now(),
      service_received_ms: serviceReceived,
      roundtrip_ms: roundtrip,
      decode_ms: decode,
      camera_wait_ms: cameraWait,
    };
  }
  const length = Number(response.headers["content-length"]);
  const serviceReceived = Number(
    response.headers["x-lf2s-service-received-ms"],
  );
  const frame = Number(response.headers["x-lf2s-frame-id"]);
  const stream = response.headers["x-lf2s-stream-id"];
  const validU64 = (value) =>
    typeof value === "string" &&
    /^[1-9][0-9]{0,19}$/.test(value) &&
    BigInt(value) < 1n << 64n;
  if (
    !Number.isInteger(length) ||
    length < 4 ||
    length > 1024 * 1024 ||
    response.headers["content-type"] !== "image/jpeg" ||
    response.headers["x-lf2s-synthetic"] !== "1" ||
    !Number.isInteger(frame) ||
    frame < 1 ||
    frame > 0xffffffff ||
    !validU64(stream) ||
    !Number.isSafeInteger(serviceReceived) ||
    serviceReceived < 1
  )
    throw new Error("合成图像响应无效");
  const bytes = new Uint8Array(response.body);
  if (bytes.length !== length) throw new Error("合成图像不完整");
  return {
    bytes,
    frame_id: frame,
    stream_id: stream,
    synthetic: true,
    hardware_freshness_qualified: false,
    received_at: Date.now(),
    service_received_ms: serviceReceived,
  };
}

async function startWorker() {
  microphoneUntil = 0;
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
    (contents, permission, callback, details) =>
      callback(
        contents === win?.webContents &&
          contents.getURL() === pageURL &&
          permission === "media" &&
          details.isMainFrame === true &&
          details.requestingUrl === pageURL &&
          details.mediaTypes?.length === 1 &&
          details.mediaTypes[0] === "audio" &&
          Date.now() < microphoneUntil,
      ),
  );
  session.defaultSession.setPermissionCheckHandler(
    (contents, permission, _origin, details) =>
      contents === win?.webContents &&
      contents.getURL() === pageURL &&
      permission === "media" &&
      details.isMainFrame === true &&
      details.requestingUrl === pageURL &&
      details.mediaType === "audio" &&
      Date.now() < microphoneUntil,
  );
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
    title: "LF-2S 视觉任务工作台",
    backgroundColor: "#f5f7f9",
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
  ipcMain.handle("station:textParse", (event, text) => {
    validate(event);
    if (typeof text !== "string" || !text.trim() || text.length > 512)
      throw new Error("请输入 1～512 字的任务原文");
    return request("text-tasks/parse", "POST", { text });
  });
  ipcMain.handle("station:textClear", (event) => {
    validate(event);
    return request("text-tasks/clear", "POST");
  });
  ipcMain.handle("station:microphone", (event, enabled) => {
    validate(event);
    if (typeof enabled !== "boolean") throw new Error("录音请求无效");
    if (enabled && (!currentState().service_ready || currentState().replay))
      throw new Error("请在实时页面和本地服务就绪后录音");
    microphoneUntil = enabled ? Date.now() + 20000 : 0;
  });
  ipcMain.handle("station:voiceTranscribe", (event, audio) => {
    validate(event);
    if (
      !(audio instanceof ArrayBuffer) ||
      audio.byteLength < 44 ||
      audio.byteLength > 500000
    )
      throw new Error("音频大小或格式无效");
    return voiceOperation(() =>
      request("voice/transcribe", "POST", Buffer.from(audio)),
    );
  });
  ipcMain.handle("station:voiceClear", (event) => {
    validate(event);
    microphoneUntil = 0;
    return voiceOperation(() => request("voice/clear", "POST"));
  });
  ipcMain.handle("station:recordings", (event) => {
    validate(event);
    return request("recordings");
  });
  for (const name of ["replayOpen", "replaySeek"]) {
    ipcMain.handle(`station:${name}`, async (event, id, index = 0) => {
      validate(event);
      if (
        typeof id !== "string" ||
        !/^[a-f0-9]{24}$/.test(id) ||
        !Number.isInteger(index) ||
        index < 0 ||
        index > 100000
      )
        throw new Error("回放编号或帧无效");
      if (name === "replayOpen") {
        microphoneUntil = 0;
        await voiceOperation(() => request("voice/clear", "POST"));
      }
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
  ipcMain.handle("station:visionAnalyze", (event, capture) => {
    validate(event);
    if (
      !capture ||
      !(capture.bytes instanceof Uint8Array) ||
      capture.bytes.byteLength < 4 ||
      capture.bytes.byteLength > 1024 * 1024 ||
      capture.bytes[0] !== 0xff ||
      capture.bytes[1] !== 0xd8 ||
      capture.bytes.at(-2) !== 0xff ||
      capture.bytes.at(-1) !== 0xd9
    )
      throw new Error("当前工作台 JPEG 图像无效");
    const source = capture.synthetic === true ? "synthetic" : "esp32_preview";
    const frame = capture.frame_id;
    const stream = capture.stream_id;
    const validU64 = (value) =>
      typeof value === "string" &&
      /^[1-9][0-9]{0,19}$/.test(value) &&
      BigInt(value) < 1n << 64n;
    if (
      !Number.isInteger(frame) ||
      frame < 1 ||
      frame > 0xffffffff ||
      !validU64(stream) ||
      !Number.isSafeInteger(capture.service_received_ms) ||
      capture.service_received_ms < 1
    )
      throw new Error("当前工作台画面编号或接收时间无效");
    const epoch = source === "synthetic" ? 0 : capture.epoch;
    if (
      source === "synthetic"
        ? capture.capture_us !== undefined
        : !Number.isSafeInteger(epoch) ||
          epoch < 1 ||
          typeof capture.capture_us !== "string" ||
          !/^[1-9][0-9]{0,19}$/.test(capture.capture_us) ||
          BigInt(capture.capture_us) >= 1n << 64n
    )
      throw new Error("当前工作台相机帧信息无效");
    const headers = {
      "X-LF2S-Source": source,
      "X-LF2S-Frame-Id": String(frame),
      "X-LF2S-Stream-Id": stream,
      "X-LF2S-Epoch": String(epoch),
      "X-LF2S-Service-Received-Ms": String(capture.service_received_ms),
      ...(source === "esp32_preview"
        ? { "X-LF2S-Capture-Us": capture.capture_us }
        : {}),
    };
    return request(
      "vision/analyze",
      "POST",
      Buffer.from(capture.bytes),
      false,
      headers,
    );
  });
  ipcMain.handle("station:visionCancel", (event, requestId) => {
    validate(event);
    if (typeof requestId !== "string" || !/^[a-f0-9]{24}$/.test(requestId))
      throw new Error("单帧观察编号无效");
    return request("vision/cancel", "POST", { request_id: requestId });
  });
  ipcMain.handle("station:benchConnect", (event, host, port) => {
    validate(event);
    if (
      typeof host !== "string" ||
      host.length > 15 ||
      !/^\d{1,3}(\.\d{1,3}){3}$/.test(host) ||
      !Number.isInteger(port) ||
      port < 1 ||
      port > 65535
    )
      throw new Error("ESP32 地址或端口无效");
    return request("bench/connect", "POST", { host, port });
  });
  for (const [name, route] of [
    ["benchDisconnect", "bench/disconnect"],
    ["gatewayConnect", "gateway/connect"],
    ["gatewayDisconnect", "gateway/disconnect"],
    ["gatewayProfile", "gateway/profile"],
    ["benchFocus", "bench/focus"],
    ["fcUsbConnect", "fc-usb/connect"],
    ["fcUsbDisconnect", "fc-usb/disconnect"],
  ]) {
    ipcMain.handle(`station:${name}`, (event) => {
      validate(event);
      return request(route, "POST");
    });
  }
  ipcMain.handle("station:benchPreview", (event) => {
    validate(event);
    return request("bench/preview", "GET", undefined, "real");
  });
  ipcMain.handle("station:benchSettings", (event, size, colour) => {
    validate(event);
    if (
      !["qvga", "vga", "svga"].includes(size) ||
      !["driver", "office"].includes(colour)
    )
      throw new Error("不支持的预览设置");
    return request("bench/settings", "POST", { size, colour });
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
