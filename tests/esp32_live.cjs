// Opt-in paired-board bench. The calling helper owns Wi-Fi restoration.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { _electron } = require("playwright");
const root = path.resolve(__dirname, "..");
const packaged = process.argv.includes("--packaged");
const suffix = packaged ? "package" : "dev";
const boardHost = process.env.LF2S_ESP32_HOST || "192.168.4.1";
const output = path.join(root, "evidence");
const report = {
  passed: false,
  application_version: JSON.parse(
    fs.readFileSync(path.join(root, "package.json"), "utf8"),
  ).version,
  physical_esp32: true,
  transport: "serialized_http_keepalive",
  hardware_control_allowed: false,
  scope:
    "Paired ESP32 HTTP diagnostics, JPEG display and focus request; no STM32 or flight",
  checks: [],
};
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function check(name, passed) {
  report.checks.push({ name, passed: !!passed });
  if (!passed) throw new Error(name);
}
async function until(test, message, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await test()) return;
    await delay(100);
  }
  throw new Error(message);
}
async function main() {
  let instance;
  const errors = [],
    requests = [];
  const env = {
    ...process.env,
    LF2S_STATION_USER_DATA: path.join(root, ".work/qa-esp32-" + suffix),
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
  try {
    const distribution = packaged
      ? path.join(
          root,
          "release",
          JSON.parse(
            fs.readFileSync(path.join(root, "release/latest.json"), "utf8"),
          ).directory,
        )
      : root;
    report.provider_sha256_lf = packaged
      ? JSON.parse(
          fs.readFileSync(
            path.join(distribution, "bundle-manifest.json"),
            "utf8",
          ),
        ).source_sha256_lf["service/esp32.py"]
      : crypto
          .createHash("sha256")
          .update(
            fs
              .readFileSync(path.join(root, "service/esp32.py"), "utf8")
              .replaceAll("\r\n", "\n"),
          )
          .digest("hex");
    instance = await _electron.launch({
      executablePath: packaged
        ? path.join(distribution, "LF2S-Station.exe")
        : require("electron"),
      args: packaged ? [] : [root],
      env,
      timeout: 20000,
    });
    const page = await instance.firstWindow();
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => {
      if (/^https?:/.test(request.url())) requests.push(request.url());
    });
    await until(
      async () =>
        (await page.evaluate(() => window.lf2s.state())).service_ready,
      "service_not_ready",
      20000,
    );
    await page.getByTestId("workspace-tools").click();
    await page
      .getByRole("button", { name: "ESP32 实物台架", exact: false })
      .click();
    await page.getByLabel("ESP32 IPv4 地址", { exact: true }).fill(boardHost);
    await page
      .getByRole("button", { name: "连接实物台架", exact: true })
      .click();
    await until(
      async () =>
        (await page.evaluate(() => window.lf2s.state())).bench?.phase ===
        "online",
      "board_not_online",
    );
    const state = await page.evaluate(() => window.lf2s.state());
    report.health = state.bench.health;
    report.board_host = boardHost;
    if (state.bench.health.wifi_mode) {
      const networkLabel =
        state.bench.health.wifi_mode === "sta"
          ? "板卡连接电脑热点"
          : "板卡提供热点";
      check(
        "physical_network_diagnostics_render_from_board",
        (await page.getByTestId("wifi-mode").innerText()) === networkLabel &&
          (await page.getByTestId("wifi-ipv4").innerText()) ===
            state.bench.health.wifi_ipv4 &&
          (await page.getByTestId("wifi-signal").innerText()).includes(
            state.bench.health.wifi_rssi_dbm == null
              ? "未知"
              : String(state.bench.health.wifi_rssi_dbm),
          ),
      );
    }
    check(
      "real_mode_and_unknown_fc_measurements",
      state.mode === "esp32_bench" &&
        !state.simulation &&
        !state.hardware_control_allowed &&
        Object.values(state.sensors).every(
          (s) => s.valid === false && s.value === null,
        ),
    );
    await page.getByTestId("real-image").waitFor({ timeout: 10000 });
    check(
      "renderer_decodes_actual_camera_jpeg",
      await page
        .getByTestId("real-image")
        .evaluate(
          (img) =>
            img.complete && img.naturalWidth > 0 && img.naturalHeight > 0,
        ),
    );
    check(
      "actual_camera_has_separate_banner",
      (await page.getByTestId("bench-banner").innerText()).includes("真实图像"),
    );
    await page.getByLabel("连续预览", { exact: true }).uncheck();
    await until(
      async () =>
        !(await page
          .getByRole("button", { name: "重新对焦", exact: true })
          .isDisabled()),
      "focus_not_available",
    );
    await page.getByRole("button", { name: "重新对焦", exact: true }).click();
    await page
      .getByText("重新对焦请求已接受；请观察画面与传感器状态", { exact: true })
      .waitFor();
    check("focus_acceptance_is_distinct_from_completion", true);
    const frames = [];
    const started = performance.now();
    // Same backend/IPC route as the UI; no renderer network access.
    await page.getByLabel("连续预览", { exact: true }).uncheck();
    await delay(800);
    for (let index = 0; index < 12; index++) {
      const frame = await page.evaluate(async () => {
        const f = await window.lf2s.benchPreview();
        return { ...f, bytes: f.bytes.length };
      });
      check(
        "jpeg_identity_" + index,
        frame.synthetic === false &&
          frame.display_only === true &&
          frame.hardware_freshness_qualified === false &&
          typeof frame.stream_id === "string" &&
          typeof frame.capture_us === "string" &&
          frame.width === state.bench.health.preview_width &&
          frame.height === state.bench.health.preview_height &&
          Number.isFinite(frame.roundtrip_ms) &&
          frame.roundtrip_ms >= 0 &&
          Number.isFinite(frame.decode_ms) &&
          frame.decode_ms >= 0 &&
          frame.bytes >= 4 &&
          frame.bytes <= 262144 &&
          (!frames.length ||
            (frame.frame_id > frames.at(-1).frame_id &&
              BigInt(frame.capture_us) > BigInt(frames.at(-1).capture_us))),
      );
      frames.push(frame);
      await delay(150);
    }
    report.frames = frames;
    report.elapsed_ms = Math.round(performance.now() - started);
    await page.getByLabel("连续预览", { exact: true }).check();
    await page.getByTestId("real-image").waitFor({ timeout: 7000 });
    if (Number(state.bench.health.firmware.split(".")[2]) >= 5) {
      const before = (await page.evaluate(() => window.lf2s.state())).bench
        .health.camera_stream_id;
      await page.getByLabel("画面尺寸", { exact: true }).selectOption("qvga");
      await page.getByLabel("白平衡", { exact: true }).selectOption("driver");
      await page
        .getByRole("button", { name: "应用画面设置", exact: true })
        .click();
      await until(
        async () => {
          const b = (await page.evaluate(() => window.lf2s.state())).bench;
          return (
            b.phase === "online" &&
            b.health?.preview_width === 320 &&
            b.health.camera_stream_id !== before
          );
        },
        "qvga_settings_not_verified",
        16000,
      );
      const configured = (await page.evaluate(() => window.lf2s.state())).bench;
      check(
        "explicit_settings_advance_stream_and_keep_fc_disabled",
        BigInt(configured.health.camera_stream_id) === BigInt(before) + 1n &&
          !configured.hardware_control_allowed &&
          configured.health.preview_colour === 0,
      );
      await page.getByLabel("预览频率", { exact: true }).selectOption("1");
      const seen = new Set();
      const observeStart = performance.now();
      while (performance.now() - observeStart < 5000) {
        seen.add(await page.getByTestId("real-frame").innerText());
        await delay(100);
      }
      report.one_fps = {
        seconds: (performance.now() - observeStart) / 1000,
        displayed_changes: seen.size,
      };
      check(
        "one_fps_mode_caps_new_display_frames",
        seen.size >= 2 && seen.size <= 7,
      );
      const beforeVga = (await page.evaluate(() => window.lf2s.state())).bench
        .health.camera_stream_id;
      await page.getByLabel("画面尺寸", { exact: true }).selectOption("vga");
      await page
        .getByRole("button", { name: "应用画面设置", exact: true })
        .click();
      await until(
        async () => {
          const b = (await page.evaluate(() => window.lf2s.state())).bench;
          return (
            b.phase === "online" &&
            b.health?.preview_width === 640 &&
            b.health.camera_stream_id !== beforeVga
          );
        },
        "vga_settings_not_verified",
        16000,
      );
      await page.getByLabel("预览频率", { exact: true }).selectOption("10");
      await delay(6000);
      report.preview_metrics = await page
        .getByTestId("preview-metrics")
        .innerText();
      check(
        "actual_frame_rate_and_latency_are_visible",
        !report.preview_metrics.includes("— FPS") &&
          /\d+(\.\d+)? FPS/.test(report.preview_metrics) &&
          /\d+ ms/.test(report.preview_metrics),
      );
    }
    await page.screenshot({
      path: path.join(output, `esp32-${suffix}-live.png`),
    });
    await page
      .getByRole("button", { name: "断开实物台架", exact: true })
      .click();
    await until(
      async () =>
        !(await page.evaluate(() => window.lf2s.state())).bench.active,
      "disconnect_failed",
    );
    await until(
      async () => (await page.getByTestId("real-image").count()) === 0,
      "late_picture_survived_disconnect",
    );
    check("disconnect_invalidates_live_picture", true);
    check("no_renderer_network_or_errors", !errors.length && !requests.length);
    report.passed = true;
  } catch (error) {
    report.error = error.stack;
    if (instance) {
      const page = await instance.firstWindow().catch(() => null);
      if (page) {
        report.failure_state = await page
          .evaluate(() => window.lf2s.state())
          .catch(() => null);
        report.failure_text = (
          await page
            .locator("main")
            .innerText()
            .catch(() => "")
        ).slice(0, 6000);
        await page
          .screenshot({
            path: path.join(output, `esp32-${suffix}-failure.png`),
          })
          .catch(() => {});
      }
    }
    throw error;
  } finally {
    if (instance) await instance.close().catch(() => {});
    fs.writeFileSync(
      path.join(output, `esp32-${suffix}-live.json`),
      JSON.stringify(report, null, 2),
    );
    console.log(
      JSON.stringify({
        passed: report.passed,
        checks: report.checks.length,
        frames: report.frames?.length || 0,
      }),
    );
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
