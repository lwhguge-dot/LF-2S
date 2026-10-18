// Explicit company inference -> backend -> IPC -> actual Vue page. No flight or microphone.
const fs = require("node:fs");
const path = require("node:path");
const { _electron } = require("playwright");
const root = path.resolve(__dirname, "..");
const packaged = process.argv.includes("--packaged");
const suffix = packaged ? "package" : "dev";
const checks = [];
const errors = [];
const report = {
  passed: false,
  application_version: require("../package.json").version,
  scope:
    "Actual company HTTP inference and local original-text checks through Electron; no ASR, permission or flight",
  thinking_enabled: false,
  checks,
  runs: [],
};
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function check(name, passed) {
  checks.push({ name, passed: !!passed });
  if (!passed) throw new Error(name);
}
async function until(test, message, timeout = 18000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await test()) return;
    await pause(80);
  }
  throw new Error(message);
}
async function main() {
  if (!process.env.LF2S_MODEL_BASE_URL || !process.env.LF2S_MODEL_API_KEY)
    throw new Error(
      "Explicitly set model endpoint and credentials for this live test",
    );
  const distribution = packaged
    ? path.join(
        root,
        "release",
        JSON.parse(
          fs.readFileSync(path.join(root, "release/latest.json"), "utf8"),
        ).directory,
      )
    : root;
  const env = {
    ...process.env,
    LF2S_STATION_USER_DATA: path.join(
      root,
      ".work",
      `qa-text-${suffix}-${Date.now()}`,
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
  let instance;
  try {
    instance = await _electron.launch({
      executablePath: packaged
        ? path.join(distribution, "LF2S-Station.exe")
        : require("electron"),
      args: packaged ? [] : [root],
      cwd: root,
      env,
    });
    const page = await instance.firstWindow();
    page.on("pageerror", (e) => errors.push(String(e)));
    await until(
      async () =>
        (await page.evaluate(() => window.lf2s.state())).service_ready,
      "Service not ready",
    );
    await page.getByTestId("workbench").waitFor();
    let state = await page.evaluate(() => window.lf2s.state());
    check(
      "configured_non_thinking_no_startup_request",
      state.text_tasks.configured &&
        state.text_tasks.model === "qwen38" &&
        !state.text_tasks.thinking_enabled &&
        state.text_tasks.current === null,
    );
    for (const [text, phase, provider, intent] of [
      ["起飞到50，保持两秒，再落地", "clarify", "local_validation", null],
      ["起飞后保持三秒再降落", "clarify", "local_validation", null],
      [
        "起飞到五十厘米，保持三秒，然后降落。",
        "review",
        "company_model",
        "takeoff_hold_land",
      ],
      ["去马路边上，然后自己过马路。", "review", "company_model", "cross_road"],
      [
        "现在B点有多少人民币，他们一共多少钱？",
        "review",
        "company_model",
        "inspect_currency_at_counter",
      ],
      ["现在去到B点", "review", "company_model", "navigate_to_landmark"],
      [
        "现在红绿灯是什么颜色？",
        "review",
        "company_model",
        "observe_traffic_signal",
      ],
      ["现在电池电压是多少？", "review", "company_model", "query_status"],
    ]) {
      await page.getByTestId("text-task-source").fill(text);
      await page.getByTestId("text-task-parse").click();
      await until(async () => {
        state = await page.evaluate(() => window.lf2s.state());
        return (
          state.text_tasks.current?.source_text === text &&
          state.text_tasks.current.phase !== "pending"
        );
      }, "Text proposal did not complete");
      const current = state.text_tasks.current;
      if (current.result?.proposal)
        await page.getByTestId("text-task-proposal").waitFor();
      check(
        `source_and_phase_${report.runs.length}`,
        current.phase === phase &&
          current.provider === provider &&
          (intent === null
            ? current.result.proposal === null
            : current.result.proposal?.intent === intent),
      );
      check(
        `no_permission_or_wire_side_effect_${report.runs.length}`,
        !current.result.executable &&
          !current.result.permission_granted &&
          !state.hardware_control_allowed &&
          state.counters.sent === 0 &&
          state.task.current === null,
      );
      if (intent === "takeoff_hold_land") {
        check(
          "exact_original_units_visible",
          current.result.proposal.height_m === 0.5 &&
            current.result.proposal.hold_s === 3 &&
            current.result.source_evidence.length === 2,
        );
        await page.screenshot({
          path: path.join(root, "evidence", `text-${suffix}-review.png`),
          fullPage: true,
        });
      }
      if (["cross_road", "inspect_currency_at_counter"].includes(intent)) {
        await page.screenshot({
          path: path.join(root, "evidence", `text-${suffix}-${intent}.png`),
          fullPage: true,
        });
        if (intent === "cross_road")
          await page.screenshot({
            path: path.join(root, "evidence", `text-${suffix}-review.png`),
            fullPage: true,
          });
      }
      if (phase === "clarify")
        await page.getByTestId("text-task-questions").waitFor();
      report.runs.push(current);
    }
    await page.getByTestId("text-task-source").fill("新的原文尚未解析");
    await until(
      async () =>
        (await page.evaluate(() => window.lf2s.state())).text_tasks.current ===
        null,
      "Editing did not clear old proposal",
    );
    check(
      "editing_hides_previous_suggestion",
      (await page.getByTestId("text-task-proposal").count()) === 0,
    );
    await page
      .getByTestId("text-task-source")
      .fill("起飞到五十厘米，保持三秒，然后降落。");
    await page.getByTestId("text-task-parse").click();
    await page.getByTestId("text-task-clear").click();
    await until(
      async () =>
        (await page.evaluate(() => window.lf2s.state())).text_tasks.current ===
        null,
      "Cancellation failed",
    );
    await pause(1500);
    check(
      "cancel_does_not_restore_late_result",
      (await page.evaluate(() => window.lf2s.state())).text_tasks.current ===
        null,
    );
    check("no_renderer_errors", errors.length === 0);
    report.passed = true;
  } catch (error) {
    report.error = String(error.stack || error).replaceAll(
      process.env.LF2S_MODEL_API_KEY,
      "[redacted]",
    );
  } finally {
    if (instance) await instance.close();
    fs.writeFileSync(
      path.join(root, "evidence", `text-${suffix}-live.json`),
      JSON.stringify({ ...report, renderer_errors: errors }, null, 2),
    );
    console.log(
      JSON.stringify({
        passed: report.passed,
        checks,
        runs: report.runs.map((r) => ({
          phase: r.phase,
          provider: r.provider,
          ms: r.elapsed_ms,
        })),
        error: report.error,
      }),
    );
    if (!report.passed) process.exitCode = 1;
  }
}
main().catch((e) => {
  console.error(String(e));
  process.exitCode = 1;
});
