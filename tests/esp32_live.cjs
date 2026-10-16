// Opt-in paired-board bench. The calling helper owns Wi-Fi restoration.
const fs = require("node:fs");
const path = require("node:path");
const { _electron } = require("playwright");
const root = path.resolve(__dirname, "..");
const packaged = process.argv.includes("--packaged");
const suffix = packaged ? "package" : "dev";
const output = path.join(root, "evidence");
const report = {
  passed: false,
  application_version: JSON.parse(
    fs.readFileSync(path.join(root, "package.json"), "utf8"),
  ).version,
  physical_esp32: true,
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
    await page
      .getByRole("button", { name: "ESP32 实物台架", exact: false })
      .click();
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
            img.complete &&
            img.naturalWidth === 800 &&
            img.naturalHeight === 600,
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
    await page.getByLabel("连续预览", { exact: true }).check();
    await until(
      async () =>
        !(await page
          .getByRole("button", { name: "读取实物图像", exact: true })
          .isDisabled()),
      "image_request_still_busy",
    );
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
          frame.width === 800 &&
          frame.height === 600 &&
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
