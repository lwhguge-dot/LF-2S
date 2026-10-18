// Opt-in physical USB bench through the real Electron renderer. No flight commands.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { _electron } = require("playwright");
const root = path.resolve(__dirname, "..");
const packaged = process.argv.includes("--packaged");
const removal = process.argv.includes("--usb-removal");
const suffix = packaged ? "package" : "dev";
const out = path.join(root, "evidence");
const report = {
  passed: false,
  physical_usb: true,
  application_version: require("../package.json").version,
  hardware_control_allowed: false,
  mavlink_session_established: false,
  scope:
    "Real native vendor USB input -> backend -> IPC -> Vue; display only; no flight",
  checks: [],
};
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function check(name, passed) {
  report.checks.push({ name, passed: !!passed });
  if (!passed) throw new Error(name);
}
async function until(test, message, timeout = 12000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await test()) return;
    await delay(100);
  }
  throw new Error(message);
}
async function main() {
  let instance;
  const errors = [];
  const userData = path.join(
    root,
    ".work",
    "qa-fc-usb-" + suffix + "-" + Date.now(),
  );
  const env = { ...process.env, LF2S_STATION_USER_DATA: userData };
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
      timeout: 30000,
    });
    const page = await instance.firstWindow();
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.getByTestId("simulation-banner").waitFor();
    fs.writeFileSync(
      path.join(out, `fc-usb-${suffix}-initial.yaml`),
      await page.locator("body").ariaSnapshot(),
    );
    let state = await page.evaluate(() => window.lf2s.state());
    await until(async () => {
      state = await page.evaluate(() => window.lf2s.state());
      return state.service_ready && !!state.usb_fc;
    }, "Local service did not publish its initial hardware state");
    check(
      "startup_does_not_open_hardware",
      !state.usb_fc.active && !state.bench.active,
    );
    await page.getByTestId("workspace-tools").click();
    await page
      .getByRole("button", { name: "⌁ 飞控 USB 台架", exact: true })
      .click();
    await page.getByTestId("fc-usb-connect").click();
    await until(async () => {
      state = await page.evaluate(() => window.lf2s.state());
      return (
        state.usb_fc?.phase === "online" &&
        state.usb_fc.attitude.available &&
        state.usb_fc.rc_raw.available
      );
    }, "Physical USB reports did not reach the renderer");
    check(
      "real_source_and_display_only",
      state.simulation === false &&
        state.read_only &&
        state.usb_fc.source === "vendor_usb_hid" &&
        state.usb_fc.display_only &&
        !state.hardware_control_allowed,
    );
    check(
      "no_synthetic_or_mavlink_identity",
      state.link.phase === "disconnected" &&
        state.identity.boot_id === null &&
        state.identity.session_id === null &&
        !state.usb_fc.mavlink_session_established &&
        Object.values(state.sensors).every((s) => s.value === null),
    );
    check(
      "unknowns_not_filled_from_placeholders",
      [
        "battery_v",
        "height_m",
        "position_m",
        "firmware_version",
        "mcu_model",
        "receiver_link_valid",
      ].every((key) => state.usb_fc[key] === null) &&
        state.usb_fc.attitude.sample_age_ms === null,
    );
    const rejected = await page.evaluate(async () => {
      const results = [];
      for (const invoke of [
        () => window.lf2s.connect(),
        () => window.lf2s.taskStart(true),
      ]) {
        try {
          await invoke();
          results.push(false);
        } catch {
          results.push(true);
        }
      }
      return results;
    });
    check(
      "physical_mode_rejects_simulation_and_task_requests",
      rejected.every(Boolean),
    );
    const start = performance.now();
    const samples = [];
    while (performance.now() - start < 20000) {
      const snapshot = await page.evaluate(() => window.lf2s.state());
      samples.push({
        elapsed_ms: performance.now() - start,
        usb_fc: snapshot.usb_fc,
      });
      await delay(250);
    }
    state = await page.evaluate(() => window.lf2s.state());
    report.device = state.usb_fc.device;
    report.last_live_state = state.usb_fc;
    report.observed_seconds = (performance.now() - start) / 1000;
    check(
      "continuous_live_reports_and_values",
      samples.every(
        (s) => s.usb_fc.phase === "online" && s.usb_fc.attitude.available,
      ) && state.usb_fc.counters.frames > 3000,
    );
    check(
      "no_checksum_report_read_or_log_error",
      ["checksum_failures", "report_errors", "read_errors"].every(
        (k) => !state.usb_fc.counters[k],
      ) &&
        state.usb_fc.raw_log.dropped === 0 &&
        state.usb_fc.raw_log.error === null,
    );
    await until(
      async () =>
        (await page.getByTestId("fc-usb-roll_deg").innerText()).includes("°"),
      "Current angles not rendered",
    );
    check(
      "actual_svg_chart_and_rc_channels",
      (await page.getByTestId("fc-usb-chart").isVisible()) &&
        !(await page.getByTestId("fc-usb-channels").innerText()).includes(
          "未知",
        ),
    );
    fs.writeFileSync(
      path.join(out, `fc-usb-${suffix}-online.yaml`),
      await page.locator("body").ariaSnapshot(),
    );
    await page.screenshot({
      path: path.join(out, `fc-usb-${suffix}-online.png`),
      fullPage: true,
    });
    if (removal) {
      const stageFile = path.join(out, `fc-usb-${suffix}-physical-stage.json`);
      fs.writeFileSync(
        stageFile,
        JSON.stringify({
          stage: "awaiting_unplug",
          timestamp: new Date().toISOString(),
        }),
      );
      console.log("USB_REMOVAL_READY");
      await until(
        async () => {
          state = await page.evaluate(() => window.lf2s.state());
          return state.usb_fc.phase === "error";
        },
        "Physical USB unplug was not observed",
        180000,
      );
      check(
        "physical_unplug_clears_live_values",
        state.usb_fc.attitude.value === null &&
          state.usb_fc.history.length === 0,
      );
      report.physical_unplug_state = state.usb_fc;
      fs.writeFileSync(
        stageFile,
        JSON.stringify({
          stage: "unplug_observed_replug_now",
          timestamp: new Date().toISOString(),
        }),
      );
      const resumeFile = path.join(
        out,
        `fc-usb-${suffix}-replug-confirmed.flag`,
      );
      await until(
        async () => fs.existsSync(resumeFile),
        "Operator replug confirmation was not received",
        180000,
      );
      await delay(1500);
      state = await page.evaluate(() => window.lf2s.state());
      check(
        "physical_replug_does_not_automatically_reopen",
        state.usb_fc.phase === "error" && state.usb_fc.attitude.value === null,
      );
      await page.getByTestId("fc-usb-disconnect").click();
      await until(
        async () =>
          !(await page.evaluate(() => window.lf2s.state())).usb_fc.active,
        "Close after unplug failed",
      );
      await page.getByTestId("fc-usb-connect").click();
      await until(async () => {
        state = await page.evaluate(() => window.lf2s.state());
        return (
          state.usb_fc.phase === "online" && state.usb_fc.attitude.available
        );
      }, "Explicit reconnect did not restore physical reports");
      check(
        "explicit_reconnect_changes_epoch_and_restores_values",
        state.usb_fc.epoch > report.last_live_state.epoch &&
          state.usb_fc.attitude.available,
      );
      report.explicit_reconnect_state = state.usb_fc;
      fs.writeFileSync(
        stageFile,
        JSON.stringify({
          stage: "physical_reconnect_passed",
          timestamp: new Date().toISOString(),
        }),
      );
    }
    await page.getByTestId("fc-usb-disconnect").click();
    await until(
      async () =>
        !(await page.evaluate(() => window.lf2s.state())).usb_fc.active,
      "USB disconnect did not finish",
    );
    state = await page.evaluate(() => window.lf2s.state());
    check(
      "disconnect_clears_values_and_history",
      state.usb_fc.attitude.value === null && state.usb_fc.history.length === 0,
    );
    await page.screenshot({
      path: path.join(out, `fc-usb-${suffix}-disconnected.png`),
    });
    check("no_renderer_errors", errors.length === 0);
    report.passed = true;
  } catch (error) {
    report.error = String(error.stack || error);
  } finally {
    if (instance) await instance.close();
    const raw = path.join(userData, "logs", "fc-usb.jsonl");
    if (fs.existsSync(raw)) {
      const bytes = fs.readFileSync(raw);
      fs.writeFileSync(path.join(out, `fc-usb-${suffix}-raw.jsonl`), bytes);
      report.raw_sha256 = crypto
        .createHash("sha256")
        .update(bytes)
        .digest("hex");
      const rows = bytes.toString("utf8").trim().split("\n").map(JSON.parse);
      const closed = rows.filter((row) => row.event === "reader_closed");
      report.raw_report_count = rows.filter((row) => row.report_hex).length;
      report.reader_closed = closed;
      if (
        !closed.length ||
        closed.reduce((total, row) => total + row.counters.reports, 0) !==
          report.raw_report_count
      )
        report.passed = false;
    } else report.passed = false;
    report.renderer_errors = errors;
    fs.writeFileSync(
      path.join(out, `fc-usb-${suffix}-live.json`),
      JSON.stringify(report, null, 2),
    );
    console.log(
      JSON.stringify({
        passed: report.passed,
        checks: report.checks,
        error: report.error,
        frames: report.last_live_state?.counters.frames,
        raw_reports: report.raw_report_count,
      }),
    );
  }
  if (!report.passed) process.exitCode = 1;
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
