// Actual Electron renderer interaction, using this repository's locked Playwright driver.
// No mock bridge and no @playwright/test dependency.
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const { _electron: electron } = require("playwright");
const { verifyS2 } = require("./s2_desktop.cjs");
const packaged = process.argv.includes("--packaged");
const output = path.join(root, "evidence");
fs.mkdirSync(output, { recursive: true });
const distribution = packaged
  ? process.env.LF2S_TEST_DISTRIBUTION ||
    path.resolve(
      root,
      "release",
      JSON.parse(
        fs.readFileSync(path.join(root, "release/latest.json"), "utf8"),
      ).directory,
    )
  : root;
const executable = packaged
  ? path.join(distribution, "LF2S-Station.exe")
  : require("electron");
const checks = [],
  rendererErrors = [],
  externalRequests = [];
const report = {
  application_version: JSON.parse(
    fs.readFileSync(
      path.join(
        distribution,
        packaged ? "resources/app/package.json" : "package.json",
      ),
      "utf8",
    ),
  ).version,
  mode: packaged
    ? "bundled Windows x64, system-only PATH"
    : "development Electron + isolated Python",
  scope:
    "S0–S2 synthetic loopback task closure and read-only replay; no hardware",
  checks,
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}
function check(name, passed, details = {}) {
  checks.push({ name, passed: !!passed, ...details });
  if (!passed) throw new Error(name);
}
async function eventually(predicate, message, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await sleep(100);
  }
  throw new Error(message);
}
function envFor(profile) {
  const env = {
    ...process.env,
    LF2S_STATION_USER_DATA: path.join(
      root,
      ".work/qa-" + (packaged ? "package-" : "dev-") + profile,
    ),
  };
  for (const key of [
    "ELECTRON_RUN_AS_NODE",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "NODE_OPTIONS",
    "NODE_PATH",
  ])
    delete env[key];
  if (packaged) env.PATH = path.join(process.env.SystemRoot, "System32");
  return env;
}
async function launch(profile) {
  const start = performance.now();
  const instance = await electron.launch({
    executablePath: executable,
    args: packaged ? [] : [root],
    env: envFor(profile),
    timeout: 30000,
  });
  const page = await instance.firstWindow();
  page.on("pageerror", (error) => rendererErrors.push(error.message));
  page.on("console", (msg) => {
    if (msg.type() === "error") rendererErrors.push(msg.text());
  });
  page.on("request", (req) => {
    if (/^https?:/.test(req.url())) externalRequests.push(req.url());
  });
  await eventually(
    async () => (await page.evaluate(() => window.lf2s.state())).service_ready,
    "service did not become ready",
    20000,
  );
  await page.getByTestId("workspace-tools").click();
  await page
    .getByRole("navigation", { name: "主导航" })
    .getByRole("button", { name: "监控总览" })
    .click();
  await page
    .getByRole("button", { name: "连接模拟设备", exact: true })
    .waitFor();
  report.startup_ms ??= Math.round(performance.now() - start);
  return { instance, page };
}
async function state(page) {
  return page.evaluate(() => window.lf2s.state());
}
async function scenario(page, value) {
  await page.getByLabel("模拟场景", { exact: true }).selectOption(value);
  await eventually(
    async () => (await state(page)).scenario === value,
    "scenario did not reach service",
  );
  await eventually(
    async () =>
      !(await page.getByLabel("模拟场景", { exact: true }).isDisabled()),
    "scenario still busy",
  );
}
async function connect(page) {
  await page.getByRole("button", { name: "连接模拟设备", exact: true }).click();
  await page.getByTestId("link").filter({ hasText: "模拟链路在线" }).waitFor();
  await eventually(
    async () => (await state(page)).sensors.height.valid,
    "height not valid",
  );
}

async function main() {
  let instance;
  try {
    const first = await launch("normal");
    instance = first.instance;
    const page = first.page;
    const startup = await state(page);
    check(
      "startup_does_not_automatically_connect_or_replay",
      startup.link.phase === "disconnected" &&
        startup.controller.available === false &&
        (await page.getByTestId("controller-mode").textContent()).includes(
          "未知",
        ),
    );
    await page
      .getByRole("button", { name: "ESP32 实物台架", exact: false })
      .click();
    await page.getByTestId("real-bench").waitFor();
    check(
      "bench_disconnected_has_no_live_or_synthetic_picture",
      (await page.getByTestId("real-image").count()) === 0 &&
        (await page.getByTestId("real-image-empty").innerText()).includes(
          "等待实物图像",
        ),
    );
    check(
      "bench_focus_disabled_without_real_connection",
      await page
        .getByRole("button", { name: "重新对焦", exact: true })
        .isDisabled(),
    );
    await page.getByLabel("ESP32 IPv4 地址", { exact: true }).fill("127.0.0.1");
    await page
      .getByRole("button", { name: "连接实物台架", exact: true })
      .click();
    await page.getByText("仅允许局域网 IPv4 地址", { exact: true }).waitFor();
    check(
      "bench_rejects_loopback_and_leaves_simulation_disconnected",
      !(await state(page)).bench.active &&
        (await state(page)).link.phase === "disconnected",
    );
    await page
      .getByLabel("ESP32 IPv4 地址", { exact: true })
      .fill("192.168.4.1");
    await page.screenshot({
      path: path.join(
        output,
        packaged
          ? "package-bench-disconnected.png"
          : "dev-bench-disconnected.png",
      ),
    });
    await page.getByRole("button", { name: "监控总览", exact: false }).click();
    await connect(page);
    await page
      .getByRole("button", { name: "读取协议配置", exact: true })
      .click();
    await page
      .getByTestId("operation")
      .filter({ hasText: "QUERY_PROFILE" })
      .waitFor();
    await eventually(async () => !!(await state(page)).profile, "no profile");
    const initial = await state(page);
    report.runtime = await instance.evaluate(() => ({
      versions: process.versions,
      platform: process.platform,
      arch: process.arch,
    }));
    const pid = initial.service_pid;
    check(
      "actual_click_hello_profile_and_native_udp",
      initial.counters.sent === 2 &&
        initial.counters.received > 0 &&
        initial.profile.fields.config_valid === 0 &&
        initial.qualification.control_allowed === false,
    );
    check(
      "controller_zero_codes_are_current_reports_without_real_qualification",
      initial.controller.available &&
        initial.controller.mode_code === 0 &&
        initial.controller.failsafe_reason_code === 0 &&
        initial.controller.preflight === false &&
        (await page.getByTestId("controller-mode").textContent()).includes(
          "空闲",
        ) &&
        (await page.getByTestId("controller-reason").textContent()).includes(
          "未上报失效原因",
        ) &&
        (await page.getByTestId("controller-preflight").textContent()).includes(
          "未满足",
        ),
    );
    const expected = (
      (BigInt(initial.identity.boot_id) << 32n) |
      1n
    ).toString();
    check(
      "u64_survives_wire_http_ipc_vue_dom",
      typeof initial.identity.session_id === "string" &&
        BigInt(initial.identity.session_id) > BigInt(Number.MAX_SAFE_INTEGER) &&
        (await page.getByTestId("u64").textContent()) === expected,
      { session_id: expected, client_nonce: initial.identity.client_nonce },
    );
    const isolation = await page.evaluate(() => ({
      node: typeof window.require,
      process: typeof window.process,
      keys: Object.keys(window.lf2s).sort(),
    }));
    const prefs = await instance.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences(),
    );
    check(
      "narrow_bridge_and_sandbox",
      isolation.node === "undefined" &&
        isolation.process === "undefined" &&
        isolation.keys.join(",") ===
          "benchConnect,benchDisconnect,benchFocus,benchPreview,benchSettings,connect,disconnect,fcUsbConnect,fcUsbDisconnect,image,microphone,onState,openLogs,profile,recordings,replayClose,replayOpen,replaySeek,restart,scenario,state,taskCancel,taskQuery,taskStart,textClear,textParse,voiceClear,voiceTranscribe" &&
        prefs.contextIsolation &&
        prefs.sandbox &&
        !prefs.nodeIntegration &&
        !prefs.webviewTag,
    );
    await page
      .getByRole("button", { name: "读取合成图像", exact: true })
      .click();
    await page.waitForFunction(
      () => document.querySelector("img")?.naturalWidth === 960,
    );
    check(
      "jpeg_really_decodes_in_renderer",
      await page.locator("img").evaluate((img) => img.naturalHeight === 540),
    );
    const suffix = packaged ? "package" : "dev";
    await page.screenshot({
      path: path.join(output, suffix + "-overview.png"),
      fullPage: true,
    });
    await scenario(page, "sensor_freeze");
    await page
      .getByTestId("sensor-height")
      .filter({ hasText: "样本过期" })
      .waitFor();
    check(
      "per_sensor_stale_while_link_online",
      (await state(page)).link.phase === "online" &&
        (await page.getByTestId("sensor-height").textContent()).includes(
          "未知",
        ),
    );
    await scenario(page, "healthy");
    await eventually(
      async () => (await state(page)).sensors.height.valid,
      "sensor failed to recover",
    );
    await scenario(page, "unknown");
    await eventually(
      async () =>
        Object.values((await state(page)).sensors).every(
          (s) => s.value === null,
        ),
      "unknown was rendered as a number",
    );
    check(
      "unknown_is_null_and_chinese_reason_visible",
      (await page.getByTestId("sensor-height").textContent()).includes("未知"),
    );
    await scenario(page, "healthy");
    await scenario(page, "link_pause");
    await page.getByTestId("link").filter({ hasText: "已过期" }).waitFor();
    await page.getByTestId("image-expired").waitFor();
    check(
      "paused_network_expires_telemetry_image_and_query_ui",
      (await page.getByRole("button", { name: "读取协议配置" }).isDisabled()) &&
        (await state(page)).controller.available === false &&
        (await page.getByTestId("controller-mode").textContent()).includes(
          "未知",
        ) &&
        (await page.getByTestId("sensor-height").textContent()).includes(
          "未知",
        ),
    );
    await page.screenshot({
      path: path.join(output, suffix + "-stale.png"),
      fullPage: true,
    });
    for (const [value, reason] of [
      ["bad_fingerprint", "协议指纹不匹配"],
      ["wrong_source", "来源不匹配"],
    ]) {
      await scenario(page, value);
      await page.getByTestId("link").filter({ hasText: "校验失败" }).waitFor();
      check(
        value + "_chinese_rejection",
        (
          await page.locator(".connection-panel .explanation").textContent()
        ).includes(reason) &&
          (await state(page)).controller.available === false &&
          (await page.getByTestId("controller-mode").textContent()).includes(
            "未知",
          ),
      );
    }
    await scenario(page, "healthy");
    await page
      .getByTestId("link")
      .filter({ hasText: "模拟链路在线" })
      .waitFor();
    await page
      .getByRole("button", { name: "协议详情", exact: false })
      .first()
      .click();
    await page.getByTestId("profile-json").waitFor();
    check(
      "protocol_page_displays_original_configuration",
      (await page.getByTestId("profile-json").textContent()).includes(
        '"config_valid": 0',
      ),
    );
    await page.getByRole("button", { name: "事件日志", exact: false }).click();
    await page.getByLabel("筛选", { exact: true }).selectOption("protocol");
    await page.locator(".event-row summary").first().click();
    check(
      "journal_expands_correlated_protocol_fields",
      (await page.getByTestId("events").textContent()).includes("operation_id"),
    );
    await page.screenshot({
      path: path.join(output, suffix + "-logs.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "后续路线", exact: false }).click();
    check(
      "future_s2_s5_retained_and_pending",
      (await page.locator(".roadmap").textContent()).includes("Mind+") &&
        (await page.locator(".roadmap").textContent()).includes("尚未完成"),
    );
    await page.getByRole("button", { name: "监控总览", exact: false }).click();
    await page.setViewportSize({ width: 760, height: 900 });
    check(
      "compact_desktop_no_horizontal_overflow",
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await page.screenshot({
      path: path.join(output, suffix + "-compact.png"),
      fullPage: true,
    });
    const sent = (await state(page)).counters.sent;
    await page.context().setOffline(true);
    await page.reload();
    await page.getByTestId("workspace-tools").click();
    await page
      .getByRole("navigation", { name: "主导航" })
      .getByRole("button", { name: "监控总览" })
      .click();
    await page
      .getByTestId("link")
      .filter({ hasText: "模拟链路在线" })
      .waitFor();
    check(
      "offline_local_reload_does_not_replay_protocol",
      (await state(page)).counters.sent === sent,
    );
    await page.context().setOffline(false);
    const duplicate = spawn(executable, packaged ? [] : [root], {
      env: envFor("normal"),
      windowsHide: true,
      stdio: "ignore",
    });
    const duplicateExit = await new Promise((resolve) => {
      duplicate.once("exit", resolve);
      setTimeout(() => {
        duplicate.kill();
        resolve("timeout");
      }, 7000).unref();
    });
    check(
      "second_launch_does_not_create_another_owner",
      duplicateExit === 0 && (await state(page)).service_pid === pid,
    );
    await scenario(page, "reboot");
    await page.getByTestId("link").filter({ hasText: "校验失败" }).waitFor();
    check(
      "fc_reboot_invalidates_old_profile",
      (await state(page)).profile === null &&
        (await state(page)).controller.available === false,
    );
    await page.getByRole("button", { name: "断开", exact: true }).click();
    await page.getByTestId("link").filter({ hasText: "尚未连接" }).waitFor();
    await connect(page);
    check(
      "explicit_reconnect_new_boot_session",
      (await state(page)).identity.session_id !== initial.identity.session_id,
    );
    // Kill only our known backend PID. The actual UI must invalidate every measurement.
    process.kill(pid);
    await page.getByTestId("service-error").waitFor();
    check(
      "backend_exit_visible_and_cached_numbers_invalidated",
      (await page.getByTestId("sensor-height").textContent()).includes(
        "未知",
      ) &&
        (await page.getByTestId("controller-mode").textContent()).includes(
          "未知",
        ),
    );
    await page.getByRole("button", { name: "重启本地服务" }).click();
    await page.getByTestId("link").filter({ hasText: "尚未连接" }).waitFor();
    const newPid = (await state(page)).service_pid;
    check(
      "backend_restart_does_not_restore_old_session",
      newPid !== pid && (await state(page)).identity.session_id === null,
    );
    const finalPid = await verifyS2({
      page,
      check,
      state,
      eventually,
      scenario,
      connect,
      output,
      suffix,
    });
    const windowClosed = instance.waitForEvent("close");
    await instance
      .evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close())
      .catch(() => {});
    await windowClosed;
    instance = undefined;
    await eventually(
      () => !alive(newPid) && !alive(finalPid),
      "normal exit leaked backend",
    );
    check(
      "normal_window_exit_reaps_backend",
      !alive(newPid) && !alive(finalPid),
    );
    const second = await launch("abrupt");
    instance = second.instance;
    const abruptPid = (await state(second.page)).service_pid;
    await instance.evaluate(({ app }) => app.exit(0)).catch(() => {});
    instance = undefined;
    await eventually(
      () => !alive(abruptPid),
      "abrupt owner exit leaked backend",
    );
    check("abrupt_owner_exit_eof_reaps_backend", !alive(abruptPid));
    check("renderer_has_no_errors", rendererErrors.length === 0, {
      rendererErrors,
    });
    check("no_external_renderer_requests", externalRequests.length === 0, {
      externalRequests,
    });
    report.passed = true;
  } catch (error) {
    report.error = error.stack;
    throw error;
  } finally {
    if (instance) await instance.close().catch(() => {});
    report.passed ??= false;
    fs.writeFileSync(
      path.join(
        output,
        process.env.LF2S_TEST_REPORT ||
          (packaged ? "desktop-package.json" : "desktop-dev.json"),
      ),
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
