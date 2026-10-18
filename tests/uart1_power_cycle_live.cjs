// Opt-in operator-assisted cold power cycle in an actual Electron window.
// Reads and explicit reconnects only; never opens J-Link or sends an action.
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { _electron } = require("playwright");
const root = path.resolve(__dirname, "..");
const host = process.env.LF2S_ESP32_HOST;
const output = process.env.LF2S_UART1_CYCLE_OUTPUT;
const packaged = process.argv.includes("--packaged");
if (!host || !output) throw new Error("Set LF2S_ESP32_HOST and a new LF2S_UART1_CYCLE_OUTPUT directory");
fs.mkdirSync(output, { recursive: false });
const checks = [], errors = [];
const report = { passed: false, scope: "Operator-assisted real cold power cycle, persistent boot generation and explicit read-only reconnect in Electron; no flight", checks, errors };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
function check(name, ok) {
  checks.push({ name, passed: !!ok });
  if (!ok) throw new Error(name);
}
function select(state) {
  return Object.fromEntries(["application_version", "mode", "link", "identity", "sensors", "gateway", "bench", "hardware_control_allowed"].map(key => [key, state[key]]));
}
function health() {
  return new Promise(resolve => {
    const request = http.get({ hostname: host, port: 80, path: "/health", timeout: 1500 }, response => {
      let body = "";
      response.on("data", chunk => { body += chunk; if (body.length > 32768) response.destroy(); });
      response.on("end", () => {
        try {
          const value = response.statusCode === 200 ? JSON.parse(body) : null;
          fs.appendFileSync(path.join(output, "health-responses.jsonl"), JSON.stringify({ timestamp: new Date().toISOString(), status: response.statusCode, health: value }) + "\n");
          resolve(value);
        } catch { resolve(null); }
      });
      response.on("error", () => resolve(null));
    });
    request.on("timeout", () => request.destroy());
    request.on("error", () => resolve(null));
  });
}
async function main() {
  const env = { ...process.env, LF2S_STATION_USER_DATA: path.join(root, ".work/qa-uart1-power-cycle") };
  for (const key of ["ELECTRON_RUN_AS_NODE", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "NODE_OPTIONS", "NODE_PATH"]) delete env[key];
  const distribution = packaged ? path.join(root, "release", JSON.parse(fs.readFileSync(path.join(root, "release/latest.json"))).directory) : root;
  if (packaged) env.PATH = path.join(process.env.SystemRoot, "System32");
  let app;
  const stream = fs.createWriteStream(path.join(output, "snapshots.jsonl"));
  let stage = "starting";
  try {
    app = await _electron.launch({ executablePath: packaged ? path.join(distribution, "LF2S-Station.exe") : require("electron"), args: packaged ? [] : [root], env, timeout: 20000 });
    const page = await app.firstWindow();
    page.on("pageerror", error => errors.push(error.message));
    async function sample() {
      const state = await page.evaluate(() => window.lf2s.state());
      stream.write(JSON.stringify({ timestamp: new Date().toISOString(), stage, ...select(state) }) + "\n");
      return state;
    }
    async function until(predicate, failure, timeout = 20000) {
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline) {
        const state = await sample();
        if (await predicate(state)) return state;
        await pause(100);
      }
      throw new Error(failure);
    }
    await until(state => state.service_ready === true && state.bench, "service_not_ready");
    await page.getByTestId("workspace-tools").click();
    await page.getByRole("button", { name: "ESP32 实物台架", exact: false }).click();
    await page.getByLabel("ESP32 IPv4 地址", { exact: true }).fill(host);
    await page.getByRole("button", { name: "连接实物台架", exact: true }).click();
    await until(state => state.bench.phase === "online", "initial_camera_not_online");
    await page.getByRole("button", { name: "连接 UART1 遥测", exact: true }).click();
    const before = await until(state => state.link.phase === "online" && state.sensors.attitude.valid, "initial_attitude_missing");
    report.application_version = before.application_version;
    report.before = select(before);
    stage = "waiting_for_operator_power_cycle";
    fs.writeFileSync(path.join(output, "status.json"), JSON.stringify({ ready: true, timestamp: new Date().toISOString(), boot_id: before.identity.boot_id, stage }, null, 2));
    console.log(JSON.stringify({ ready: true, boot_id: before.identity.boot_id, output }));
    const lost = await until(async state => state.link.phase !== "online" && !state.sensors.attitude.valid && !(await health()), "operator_power_loss_not_observed", 600000);
    stage = "power_loss_observed";
    report.lost = select(lost);
    check("power_loss_invalidates_current_attitude", lost.sensors.attitude.value === null && lost.hardware_control_allowed === false);
    await page.screenshot({ path: path.join(output, "power-off.png"), fullPage: true });
    let returned;
    await until(async () => {
      returned = await health();
      return returned && returned.camera_stream_id !== before.bench.health.camera_stream_id;
    }, "new_camera_boot_not_observed", 600000);
    stage = "new_board_boot_before_explicit_reconnect";
    if (!process.argv.includes("--immediate-reconnect")) await pause(3000);
    const blocked = await sample();
    report.before_reconnect = select(blocked);
    report.returned_camera_stream_id = returned.camera_stream_id;
    report.first_returned_health = returned;
    check("new_boot_does_not_auto_adopt_old_session", blocked.link.phase !== "online" && !blocked.sensors.attitude.valid && blocked.sensors.attitude.value === null);
    await page.evaluate(() => window.lf2s.benchDisconnect());
    const cleared = await until(state => !state.gateway.active && !state.identity.boot_id, "explicit_disconnect_did_not_clear_identity");
    check("disconnect_clears_old_boot_and_session", !cleared.identity.session_id);
    stage = "explicit_reconnect";
    await page.getByRole("button", { name: "连接实物台架", exact: true }).click();
    await until(state => state.bench.phase === "online", "restarted_camera_not_online", 35000);
    await page.getByRole("button", { name: "连接 UART1 遥测", exact: true }).click();
    const after = await until(state => state.link.phase === "online" && state.sensors.attitude.valid, "restarted_uart1_not_online");
    report.after = select(after);
    report.returned_camera_stream_id = after.bench.health.camera_stream_id;
    check("persistent_boot_id_increases_after_true_power_cycle", BigInt(after.identity.boot_id) > BigInt(before.identity.boot_id));
    check("new_boot_has_new_explicit_session", after.identity.session_id !== before.identity.session_id);
    check("fresh_real_attitude_returns_readonly", after.sensors.attitude.source === "uart1" && !after.hardware_control_allowed && after.mode === "uart_readonly");
    await page.getByRole("button", { name: "读取飞控配置", exact: true }).click();
    await until(state => state.profile, "profile_after_reboot_missing");
    check("configuration_query_recovers_after_new_session", true);
    await page.getByRole("button", { name: "返回工作台", exact: false }).click();
    await until(async state => state.sensors.attitude.valid && await page.getByTestId("real-image").isVisible(), "camera_and_attitude_after_reboot_missing");
    await page.screenshot({ path: path.join(output, "reconnected-workbench.png"), fullPage: true });
    check("camera_and_uart1_resume_together", true);
    await page.evaluate(() => window.lf2s.benchDisconnect());
    check("no_renderer_errors", errors.length === 0);
    report.passed = true;
  } catch (error) {
    report.error = String(error.stack || error);
    throw error;
  } finally {
    if (app) await app.close();
    stream.end();
    report.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(report, null, 2) + "\n");
    console.log(JSON.stringify({ passed: report.passed, checks, error: report.error }));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
