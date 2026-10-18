// Actual Electron, actual isolated Python and a local model-response fixture.
// No physical ESP32/FC/microphone, company model or hardware task execution.
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const crypto = require("node:crypto");
const root = path.resolve(__dirname, "..");
const { _electron } = require("playwright");
const packaged = process.argv.includes("--packaged");
const suffix =
  process.env.LF2S_WORKBENCH_MODE || (packaged ? "package" : "dev");
const checks = [],
  errors = [],
  requests = [];
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const report = {
  application_version: require("../package.json").version,
  scope:
    "Real Electron/backend/IPC; local HTTP model fixture, synthetic loopback picture only; no company API/hardware/microphone/control",
  checks,
  errors,
  requests,
  passed: false,
};
function check(name, value) {
  checks.push({ name, passed: !!value });
  if (!value) throw new Error(name);
}
async function until(test, name, timeout = 12000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await test()) return;
    await pause(60);
  }
  throw new Error(name);
}
async function state(page) {
  return page.evaluate(() => window.lf2s.state());
}
function value(intent, target) {
  return {
    intent,
    target,
    height_m: null,
    hold_s: null,
    clarification: null,
    requires_confirmation: intent !== "observe_traffic_signal",
  };
}
let held = null;
let heldVision = null;
let holdVisionNext = false;
const model = http.createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    try {
      const body = JSON.parse(Buffer.concat(chunks));
      const content = body.messages.at(-1).content;
      if (Array.isArray(content)) {
        const imageURL = content.find((item) => item.type === "image_url")
          ?.image_url?.url;
        const image = Buffer.from((imageURL || "").split(",").at(-1), "base64");
        requests.push({
          kind: "vision",
          model: body.model,
          thinking: body.chat_template_kwargs.enable_thinking,
          image_sha256: crypto.createHash("sha256").update(image).digest("hex"),
          image_bytes: image.length,
        });
        const observation = {
          signals: [],
          zebra_visible: true,
          checkout_visible: false,
          currency: { visible_notes_complete: null, notes: [] },
        };
        const send = () => {
          if (res.destroyed) return;
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              model: "qwen38",
              choices: [
                {
                  finish_reason: "stop",
                  message: {
                    role: "assistant",
                    content: JSON.stringify(observation),
                    reasoning_content: null,
                  },
                },
              ],
            }),
          );
        };
        if (holdVisionNext) {
          holdVisionNext = false;
          heldVision = send;
        } else send();
        return;
      }
      const text = content;
      requests.push({
        kind: "text",
        text,
        model: body.model,
        thinking: body.chat_template_kwargs.enable_thinking,
      });
      const proposal = text.includes("收银台")
        ? value("inspect_currency_at_counter", "checkout_counter")
        : text.includes("B点")
          ? value("navigate_to_landmark", "checkout_counter")
          : value("cross_road", "roadside");
      const send = () => {
        if (res.destroyed) return;
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            model: "qwen38",
            choices: [
              {
                finish_reason: "stop",
                message: {
                  content: JSON.stringify(proposal),
                  reasoning_content: null,
                },
              },
            ],
          }),
        );
      };
      if (text === "请去路边过马路") held = send;
      else send();
    } catch (error) {
      errors.push({ kind: "model_fixture_parse", error: String(error) });
      res.writeHead(400);
      res.end();
    }
  });
});
(async () => {
  await new Promise((r) => model.listen(0, "127.0.0.1", r));
  const distribution = packaged
    ? process.env.LF2S_TEST_DISTRIBUTION ||
      path.join(
        root,
        "release",
        JSON.parse(fs.readFileSync(path.join(root, "release/latest.json")))
          .directory,
      )
    : root;
  const env = {
    ...process.env,
    LF2S_MODEL_BASE_URL: `http://127.0.0.1:${model.address().port}/v1`,
    LF2S_MODEL_API_KEY: "workbench-local-fixture",
    LF2S_MODEL_NAME: "qwen38",
    LF2S_STATION_USER_DATA: path.join(
      root,
      `.work/qa-workbench-${suffix}-${Date.now()}`,
    ),
  };
  for (const k of [
    "ELECTRON_RUN_AS_NODE",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "NODE_OPTIONS",
    "NODE_PATH",
    "LF2S_ASR_BASE_URL",
    "LF2S_ASR_API_KEY",
  ])
    delete env[k];
  if (packaged) env.PATH = path.join(process.env.SystemRoot, "System32");
  let app;
  try {
    app = await _electron.launch({
      executablePath: packaged
        ? path.join(distribution, "LF2S-Station.exe")
        : require("electron"),
      args: packaged ? [] : [root],
      cwd: root,
      env,
    });
    const page = await app.firstWindow();
    page.on("pageerror", (e) => errors.push(e.message));
    await page.setViewportSize({ width: 1440, height: 1024 });
    await until(async () => (await state(page)).service_ready, "backend ready");
    check(
      "default_is_workbench_with_no_automatic_connection",
      (await page.getByTestId("workbench").isVisible()) &&
        (await state(page)).link.phase === "disconnected" &&
        !(await state(page)).bench.active,
    );
    check(
      "initial_camera_empty_and_model_has_no_startup_request",
      (await page.getByTestId("real-image-empty").isVisible()) &&
        requests.length === 0 &&
        (await state(page)).text_tasks.current === null,
    );
    const dimensions = await page.evaluate(() => ({
      viewport: [innerWidth, innerHeight],
      document: [
        document.documentElement.scrollWidth,
        document.documentElement.scrollHeight,
      ],
      camera: document.querySelector(".camera-card").getBoundingClientRect()
        .width,
      workspace: document.querySelector(".workspace").getBoundingClientRect()
        .width,
      assistantBottom: document
        .querySelector(".assistant")
        .getBoundingClientRect().bottom,
    }));
    report.layout = dimensions;
    check(
      "camera_is_primary_and_no_horizontal_overflow",
      dimensions.camera / dimensions.workspace > 0.65 &&
        dimensions.document[0] <= dimensions.viewport[0],
    );
    check(
      "assistant_camera_and_results_share_desktop_view",
      dimensions.assistantBottom <= dimensions.viewport[1] + 1,
    );
    await page.screenshot({
      path: path.join(root, "evidence", `workbench-${suffix}-idle.png`),
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileLayout = await page.evaluate(() => ({
      viewport: [innerWidth, innerHeight],
      document: [
        document.documentElement.scrollWidth,
        document.documentElement.scrollHeight,
      ],
      camera: document.querySelector(".camera-card").getBoundingClientRect(),
      assistant: document.querySelector(".assistant").getBoundingClientRect(),
    }));
    report.mobile_layout = {
      viewport: mobileLayout.viewport,
      document: mobileLayout.document,
      camera_width: mobileLayout.camera.width,
      camera_height: mobileLayout.camera.height,
      assistant_width: mobileLayout.assistant.width,
    };
    check(
      "mobile_workbench_stacks_without_horizontal_overflow",
      mobileLayout.document[0] <= mobileLayout.viewport[0] &&
        mobileLayout.camera.width >= mobileLayout.viewport[0] - 60 &&
        mobileLayout.assistant.width >= mobileLayout.viewport[0] - 60,
    );
    await page.screenshot({
      path: path.join(root, "evidence", `workbench-${suffix}-mobile.png`),
      fullPage: true,
    });
    await page.setViewportSize({ width: 1440, height: 1024 });
    await page.getByRole("button", { name: "过马路", exact: true }).click();
    check(
      "scenario_only_fills_source_without_request",
      (await page.getByTestId("text-task-source").inputValue()).includes(
        "过马路",
      ) && requests.length === 0,
    );
    await page.getByTestId("text-task-parse").click();
    await until(
      async () => (await state(page)).text_tasks.current?.phase === "review",
      "road proposal",
    );
    await page.getByTestId("text-task-proposal").waitFor();
    check(
      "new_road_intent_and_exact_source_reach_right_panel",
      (await state(page)).text_tasks.current.result.proposal.intent ===
        "cross_road" &&
        (await page.getByTestId("text-task-proposal").innerText()).includes(
          "路边",
        ),
    );
    check(
      "proposal_sends_no_protocol_or_permission",
      (await state(page)).counters.sent === 0 &&
        (await state(page)).task.current === null &&
        !(await state(page)).text_tasks.current.result.permission_granted,
    );
    check(
      "image_recognition_does_not_come_from_text_suggestion",
      (await page.locator(".result-fields dd").allTextContents()).join(",") ===
        "未知,未知,未知",
    );
    await page.screenshot({
      path: path.join(root, "evidence", `workbench-${suffix}-road.png`),
      fullPage: true,
    });
    await page
      .getByTestId("text-task-source")
      .fill("去收银台，看看一共有多少钱。");
    await until(
      async () => (await page.getByTestId("text-task-proposal").count()) === 0,
      "editing clears current proposal",
    );
    check(
      "editing_clears_previous_proposal",
      (await page.getByTestId("text-task-proposal").count()) === 0,
    );
    await page.getByTestId("text-task-parse").click();
    await until(
      async () => (await state(page)).text_tasks.current?.phase === "review",
      "cash proposal",
    );
    await until(
      async () =>
        (await page.locator(".result-fields dt").allTextContents()).includes(
          "合计金额",
        ),
      "cash result labels reached renderer",
    );
    check(
      "cash_intent_has_unknown_count_and_total",
      (await state(page)).text_tasks.current.result.proposal.intent ===
        "inspect_currency_at_counter" &&
        (await page.locator(".result-fields dt").allTextContents()).includes(
          "合计金额",
        ) &&
        (await page.locator(".result-fields dd").allTextContents()).every(
          (t) => t === "未知",
        ),
    );
    await page.screenshot({
      path: path.join(root, "evidence", `workbench-${suffix}-cash.png`),
      fullPage: true,
    });
    await page.getByTestId("text-task-source").fill("请去路边过马路");
    await page.getByTestId("text-task-parse").click();
    await until(() => held !== null, "held request");
    await page.getByTestId("text-task-source").fill("新的原文尚未解析");
    held();
    held = null;
    await pause(200);
    check(
      "late_model_result_does_not_replace_edited_source",
      (await page.getByTestId("text-task-proposal").count()) === 0,
    );
    await page.getByTestId("text-task-clear").click();
    await page.getByTestId("workspace-tools").click();
    await page
      .getByRole("navigation", { name: "主导航" })
      .getByRole("button", { name: "监控总览" })
      .click();
    check(
      "detailed_connection_and_controller_tools_still_work",
      (await page.getByTestId("controller-mode").isVisible()) &&
        (await page
          .getByRole("button", { name: "连接模拟设备", exact: true })
          .isVisible()),
    );
    await page
      .getByRole("button", { name: "连接模拟设备", exact: true })
      .click();
    await until(
      async () => (await state(page)).link.phase === "online",
      "synthetic connected",
    );
    await page.getByTestId("workspace-return").click();
    await page.getByLabel("工作台图像来源").selectOption("synthetic");
    await page.getByTestId("synthetic-image").waitFor();
    check(
      "synthetic_picture_really_decodes_and_remains_labelled",
      (await page
        .getByTestId("synthetic-image")
        .evaluate((e) => e.complete && e.naturalWidth > 0)) &&
        (await page.locator(".camera-card").innerText()).includes(
          "模拟图像 · 非实拍",
        ),
    );
    holdVisionNext = true;
    await page.getByTestId("vision-analyze").click();
    try {
      await until(() => heldVision !== null, "held frame-observation request");
    } catch (error) {
      const current = await state(page);
      const alerts = await page.getByRole("alert").allInnerTexts();
      throw new Error(
        `${error.message}; vision=${JSON.stringify(current.vision)}; ` +
          `button_disabled=${await page.getByTestId("vision-analyze").isDisabled()}; ` +
          `alerts=${JSON.stringify(alerts)}`,
      );
    }
    await until(
      async () => (await state(page)).vision.current?.phase === "pending",
      "pending single-frame observation",
    );
    await page.getByTestId("vision-frame-image").waitFor();
    check(
      "explicit_analysis_sends_only_the_selected_synthetic_jpeg",
      requests.at(-1).kind === "vision" &&
        requests.at(-1).image_bytes > 4 &&
        requests.at(-1).model === "qwen38" &&
        requests.at(-1).thinking === false &&
        (await page.getByTestId("vision-frame-image").count()) === 1,
    );
    await page.getByTestId("vision-cancel").click();
    await until(
      async () => (await state(page)).vision.current === null,
      "cancelled observation cleared",
    );
    heldVision();
    heldVision = null;
    await pause(200);
    check(
      "cancelled_model_reply_cannot_restore_observation",
      (await state(page)).vision.current === null &&
        (await page.getByTestId("vision-review").count()) === 0,
    );
    await until(
      async () =>
        !(await page.getByTestId("vision-analyze").isDisabled()),
      "fresh synthetic frame after cancelling analysis",
    );
    await page.evaluate(() => {
      window.__lf2sTestBlobs = new Map();
      const createObjectURL = URL.createObjectURL.bind(URL);
      URL.createObjectURL = (blob) => {
        const url = createObjectURL(blob);
        window.__lf2sTestBlobs.set(url, blob);
        return url;
      };
    });
    await page.getByTestId("vision-analyze").click();
    await until(
      async () => (await state(page)).vision.current?.phase === "ready",
      "single-frame model observation",
    );
    const visual = await state(page);
    const reviewedImageHash = await page.evaluate(async () => {
      const img = document.querySelector('[data-testid="vision-frame-image"]');
      const blob = window.__lf2sTestBlobs.get(img.src);
      if (!blob) throw new Error("Frozen review image Blob is missing");
      const bytes = await blob.arrayBuffer();
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest), (v) =>
        v.toString(16).padStart(2, "0"),
      ).join("");
    });
    check(
      "observation_hash_matches_model_input_and_frozen_display_frame",
      visual.vision.current.frame.image_sha256 === reviewedImageHash &&
        visual.vision.current.frame.image_sha256 ===
          requests.filter((r) => r.kind === "vision").at(-1).image_sha256 &&
        String(visual.vision.current.frame.frame_id) ===
          (await page.getByTestId("vision-frame-id").innerText()),
    );
    check(
      "model_observation_is_visibly_unverified_and_has_no_action_permission",
      (await page.getByTestId("vision-status").innerText()) === "待人工核对" &&
        (await page.getByTestId("vision-review").innerText()).includes(
          "本机合成 · 非实拍",
        ) &&
        (await page.getByTestId("vision-review").innerText()).includes(
          "许可：无",
        ) &&
        visual.vision.current.observation.crossing_allowed === false &&
        visual.vision.current.observation.hardware_control_allowed === false,
    );
    await page.screenshot({
      path: path.join(root, "evidence", `workbench-${suffix}-vision.png`),
      fullPage: true,
    });
    await page.getByTestId("vision-resume").click();
    await until(
      async () => (await state(page)).vision.current === null,
      "new frame invalidates previous observation",
    );
    await page.getByTestId("text-task-source").fill("现在去到B点");
    await page.getByTestId("text-task-parse").click();
    await until(
      async () => (await state(page)).text_tasks.current?.phase === "review",
      "goto B",
    );
    check(
      "goto_B_is_not_automatically_money_counting",
      (await state(page)).text_tasks.current.result.proposal.intent ===
        "navigate_to_landmark",
    );
    await page.getByTestId("workspace-tools").click();
    await page.getByTestId("workspace-return").click();
    check(
      "tools_return_keeps_original_text_without_resuming_proposal",
      (await page.getByTestId("text-task-source").inputValue()) ===
        "现在去到B点" &&
        (await page.getByTestId("text-task-proposal").count()) === 0,
    );
    check(
      "source_choice_survives_tools_switch",
      (await page.getByLabel("工作台图像来源").inputValue()) === "synthetic",
    );
    await page.getByTestId("workspace-voice-toggle").click();
    await page.getByTestId("voice-panel").waitFor();
    check(
      "voice_input_opens_existing_review_flow",
      await page.getByTestId("voice-panel").isVisible(),
    );
    await page.getByTestId("workspace-tools").click();
    await page.getByTestId("workspace-return").click();
    check(
      "leaving_workspace_closes_voice_input",
      (await page.getByTestId("voice-panel").count()) === 0,
    );
    const pid = (await state(page)).service_pid;
    process.kill(pid);
    await until(
      async () => !(await state(page)).service_ready,
      "backend stopped",
    );
    await until(
      async () => (await page.getByTestId("synthetic-image").count()) === 0,
      "stale picture cleared",
    );
    check(
      "backend_loss_clears_picture_and_suggestion",
      (await page.getByTestId("synthetic-image").count()) === 0 &&
        (await page.getByTestId("text-task-proposal").count()) === 0,
    );
    check(
      "requests_only_use_qwen38_with_thinking_disabled",
      requests.length >= 3 &&
        requests.every((r) => r.model === "qwen38" && r.thinking === false),
    );
    check("renderer_has_no_errors", errors.length === 0);
    report.passed = true;
  } catch (error) {
    report.error = error.stack;
    if (app) {
      try {
        const failedPage = await app.firstWindow();
        report.failure_state = await state(failedPage);
        report.failure_text = await failedPage.locator("body").innerText();
        await failedPage.screenshot({ path: path.join(root, "evidence", `workbench-${suffix}-failure.png`), fullPage: true });
      } catch (diagnosticError) {
        report.diagnostic_error = String(diagnosticError);
      }
    }
    throw error;
  } finally {
    if (held) held();
    if (heldVision) heldVision();
    if (app) await app.close().catch(() => {});
    await new Promise((r) => model.close(r));
    fs.writeFileSync(
      path.join(root, "evidence", `workbench-${suffix}.json`),
      JSON.stringify(report, null, 2) + "\n",
    );
  }
  console.log(
    JSON.stringify({
      passed: report.passed,
      version: report.application_version,
      checks: checks.length,
      mode: suffix,
    }),
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
