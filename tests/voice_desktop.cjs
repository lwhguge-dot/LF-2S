// Actual audio graph, narrow IPC and HTTP. Microphone device is a Chromium file fixture.
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
const { createHash, randomBytes } = require("node:crypto");
const { _electron } = require("playwright");
const root = path.resolve(__dirname, "..");
const workspace = path.resolve(root, "../..");
const modelId = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17";
const model = path.join(workspace, ".codex-tools/asr-models", modelId);
const reference = path.join(model, "test_wavs/zh.wav");
const packaged = process.argv.includes("--packaged");
const realAsr = process.argv.includes("--real-asr");
const companyModel = process.argv.includes("--company-model");
const suffix = packaged ? "package" : "dev";
const text = "起飞到五十厘米，保持三秒，然后降落。";
const asrToken = randomBytes(24).toString("hex");
const cpuToken = randomBytes(24).toString("hex");
const checks = [],
  rendererErrors = [],
  calls = [];
let mode = "mock",
  cpu,
  cpuPort,
  instance,
  relay;
const report = {
  application_version: require("../package.json").version,
  passed: false,
  scope:
    "Real Electron PCM capture from a Chromium fake microphone, WAV import and review; optional actual local CPU ASR reference audio. No human recording, company Linux deployment or hardware control.",
  fake_microphone: true,
  real_local_asr: realAsr,
  company_asr: false,
  company_model: companyModel,
  checks,
  reference: null,
};
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function check(name, value) {
  checks.push({ name, passed: !!value });
  if (!value) throw new Error(name);
}
async function until(test, message, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await test()) return;
    await pause(80);
  }
  throw new Error(message);
}
function listen(server) {
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve(server.address().port)),
  );
}
function cpuRequest(route, method, raw) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: cpuPort,
        path: route,
        method,
        headers: {
          Authorization: "Bearer " + cpuToken,
          ...(raw
            ? { "Content-Type": "audio/wav", "Content-Length": raw.length }
            : {}),
        },
      },
      (res) => {
        const body = [];
        res.on("data", (part) => body.push(part));
        res.on("end", () =>
          resolve({ status: res.statusCode, body: Buffer.concat(body) }),
        );
      },
    );
    req.on("error", reject);
    req.setTimeout(40000, () =>
      req.destroy(new Error("Local CPU request timeout")),
    );
    req.end(raw);
  });
}
async function startCpu() {
  const probe = http.createServer();
  cpuPort = await listen(probe);
  await new Promise((resolve) => probe.close(resolve));
  const log = fs.openSync(
    path.join(root, ".work", `voice-${suffix}-cpu.log`),
    "w",
  );
  cpu = spawn(
    path.join(workspace, ".codex-tools/asr-cpu/Scripts/python.exe"),
    [
      "-B",
      path.join(workspace, "P1/09_语音服务/scripts/serve_local_owned.py"),
      String(cpuPort),
    ],
    {
      env: {
        ...process.env,
        LF2S_ASR_TOKEN: cpuToken,
        LF2S_ASR_MODEL_DIR: model,
        LF2S_ASR_THREADS: "4",
      },
      windowsHide: true,
      stdio: ["pipe", log, log],
    },
  );
  fs.closeSync(log);
  await until(
    async () => {
      if (cpu.exitCode !== null) throw new Error("Local CPU owner exited");
      try {
        return JSON.parse((await cpuRequest("/health", "GET")).body).ready;
      } catch {
        return false;
      }
    },
    "Local CPU did not start",
    90000,
  );
}
async function startRelay() {
  relay = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    if (request.url === "/v1/chat/completions") {
      const payload = JSON.parse(raw);
      calls.push({
        kind: "model",
        thinking: payload.chat_template_kwargs?.enable_thinking,
        model: payload.model,
      });
      response.setHeader("Content-Type", "application/json");
      response.end(
        JSON.stringify({
          model: "qwen38",
          choices: [
            {
              finish_reason: "stop",
              message: {
                content: JSON.stringify({
                  intent: "takeoff_hold_land",
                  height_m: 0.5,
                  hold_s: 3,
                  target: null,
                  clarification: null,
                  requires_confirmation: true,
                }),
              },
            },
          ],
        }),
      );
      return;
    }
    const selected = mode;
    const call = {
      kind: "asr",
      mode: selected,
      bytes: raw.length,
      rate: raw.readUInt32LE(24),
      channels: raw.readUInt16LE(22),
      width: raw.readUInt16LE(34),
      duration_ms: Math.round((raw.readUInt32LE(40) / 2 / 16000) * 1000),
      audio_sha256: createHash("sha256").update(raw).digest("hex"),
    };
    calls.push(call);
    if (selected === "slow") await pause(1200);
    if (selected === "real") {
      const reply = await cpuRequest("/v1/transcriptions", "POST", raw);
      response.writeHead(reply.status, { "Content-Type": "application/json" });
      response.end(reply.body);
      return;
    }
    response.setHeader("Content-Type", "application/json");
    response.end(
      JSON.stringify({
        request_id: "a".repeat(24),
        model: modelId,
        provider: "cpu",
        language: "zh",
        audio_sha256: call.audio_sha256,
        duration_ms: call.duration_ms,
        processing_ms: 0,
        transcript: text,
        transcript_approved: false,
        permission_granted: false,
        hardware_control_allowed: false,
      }),
    );
  });
  return listen(relay);
}
async function state(page) {
  return page.evaluate(() => window.lf2s.state());
}
async function main() {
  if (
    companyModel &&
    (!process.env.LF2S_MODEL_BASE_URL || !process.env.LF2S_MODEL_API_KEY)
  )
    throw new Error("Explicit company model configuration is required");
  fs.mkdirSync(path.join(root, ".work"), { recursive: true });
  if (realAsr) await startCpu();
  const port = await startRelay();
  const distribution = packaged
    ? path.join(
        root,
        "release",
        JSON.parse(fs.readFileSync(path.join(root, "release/latest.json")))
          .directory,
      )
    : root;
  const env = {
    ...process.env,
    LF2S_STATION_USER_DATA: path.join(
      root,
      ".work",
      `qa-voice-${suffix}-${Date.now()}`,
    ),
    LF2S_ASR_BASE_URL: `http://127.0.0.1:${port}`,
    LF2S_ASR_TOKEN: asrToken,
    LF2S_MODEL_BASE_URL: companyModel
      ? process.env.LF2S_MODEL_BASE_URL
      : `http://127.0.0.1:${port}/v1`,
    LF2S_MODEL_API_KEY: companyModel
      ? process.env.LF2S_MODEL_API_KEY
      : "isolated-test-model-key",
    LF2S_MODEL_NAME: "qwen38",
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
  instance = await _electron.launch({
    executablePath: packaged
      ? path.join(distribution, "LF2S-Station.exe")
      : require("electron"),
    args: [
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${reference}`,
      ...(packaged ? [] : [root]),
    ],
    env,
    cwd: root,
  });
  const page = await instance.firstWindow();
  page.on("pageerror", (error) => rendererErrors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") rendererErrors.push(message.text());
  });
  await until(
    async () => (await state(page)).service_ready,
    "Station not ready",
    25000,
  );
  await page.evaluate(() => {
    window.__voiceQaTracks = [];
    const original = navigator.mediaDevices.getUserMedia.bind(
      navigator.mediaDevices,
    );
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await original(constraints);
      window.__voiceQaTracks.push(...stream.getTracks());
      return stream;
    };
  });
  check("no_startup_audio_or_model_request", calls.length === 0);
  check(
    "microphone_denied_before_record_click",
    await page.evaluate(async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: true,
        });
        stream.getTracks().forEach((t) => t.stop());
        return false;
      } catch {
        return true;
      }
    }),
  );
  await page.getByTestId("workspace-tools").click();
  await page
    .getByRole("button", { name: "✎ 文本任务建议", exact: true })
    .click();
  await page.getByTestId("voice-record").click();
  await until(
    async () => (await page.getByTestId("voice-recording").count()) === 1,
    "Recording did not start",
  );
  check(
    "video_permission_stays_denied",
    await page.evaluate(async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: true,
        });
        stream.getTracks().forEach((t) => t.stop());
        return false;
      } catch {
        return true;
      }
    }),
  );
  await pause(900);
  await page.getByTestId("voice-stop").click();
  await page.getByTestId("voice-preview").waitFor();
  check(
    "recording_is_local_until_explicit_submit",
    calls.length === 0 &&
      (await page.evaluate(() =>
        window.__voiceQaTracks.every((track) => track.readyState === "ended"),
      )),
  );
  const savedAudio = path.join(
    root,
    ".work",
    `voice-${suffix}-saved-${Date.now()}.wav`,
  );
  await instance.evaluate(({ session }, target) => {
    globalThis.__voiceQaDownloadState = "waiting";
    session.defaultSession.once("will-download", (_event, item) => {
      item.setSavePath(target);
      item.once("done", (_doneEvent, state) => {
        globalThis.__voiceQaDownloadState = state;
      });
    });
  }, savedAudio);
  await page.getByTestId("voice-save").click();
  await until(
    async () =>
      await instance.evaluate(
        () => globalThis.__voiceQaDownloadState === "completed",
      ),
    "Explicit WAV save failed",
  );
  const savedHash = createHash("sha256")
    .update(fs.readFileSync(savedAudio))
    .digest("hex");
  await page.getByTestId("voice-transcribe").click();
  await until(
    async () => (await state(page)).voice.current?.phase === "review",
    "Captured PCM did not reach review",
  );
  const captured = calls.find((call) => call.kind === "asr");
  check(
    "explicit_save_matches_submitted_wav",
    captured.audio_sha256 === savedHash,
  );
  check(
    "audio_graph_and_ipc_produce_bounded_mono_pcm",
    captured.rate === 16000 &&
      captured.channels === 1 &&
      captured.width === 16 &&
      captured.duration_ms >= 500 &&
      captured.duration_ms < 3000,
  );
  check(
    "transcription_does_not_parse_or_authorize",
    (await state(page)).text_tasks.current === null &&
      (await state(page)).task.current === null &&
      (await state(page)).counters.sent === 0,
  );
  await page.getByTestId("voice-adopt").click();
  check(
    "adoption_only_fills_editable_original",
    (await page.getByTestId("text-task-source").inputValue()) === text &&
      (await state(page)).text_tasks.current === null,
  );
  await page.getByTestId("text-task-parse").click();
  await until(
    async () => (await state(page)).text_tasks.current?.phase === "review",
    "Explicit text parsing failed",
  );
  check(
    "explicit_parse_keeps_non_thinking_and_no_action",
    (companyModel
      ? (await state(page)).text_tasks.thinking_enabled === false
      : calls.some(
          (call) =>
            call.kind === "model" &&
            call.model === "qwen38" &&
            call.thinking === false,
        )) &&
      (await state(page)).counters.sent === 0 &&
      (await state(page)).task.current === null,
  );
  await page.getByTestId("voice-clear").click();
  check(
    "permission_revoked_after_recording",
    await page.evaluate(async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: true,
        });
        stream.getTracks().forEach((t) => t.stop());
        return false;
      } catch {
        return true;
      }
    }),
  );
  mode = "slow";
  await page.getByTestId("voice-file").setInputFiles(reference);
  await page.getByTestId("voice-preview").waitFor();
  await page.getByTestId("voice-transcribe").click();
  await until(
    async () => (await state(page)).voice.current?.phase === "pending",
    "Slow request not pending",
  );
  await page.getByTestId("voice-clear").click();
  await pause(1500);
  check(
    "cancel_drops_late_transcript_and_audio",
    (await state(page)).voice.current === null &&
      (await page.getByTestId("voice-review").count()) === 0 &&
      (await page.getByTestId("voice-preview").count()) === 0,
  );
  if (realAsr) {
    mode = "real";
    await page.getByTestId("voice-file").setInputFiles(reference);
    await page.getByTestId("voice-preview").waitFor();
    await page.getByTestId("voice-transcribe").click();
    await until(
      async () => (await state(page)).voice.current?.phase === "review",
      "Actual CPU reference did not reach review",
      40000,
    );
    const current = (await state(page)).voice.current;
    report.reference = {
      input: "upstream test_wavs/zh.wav",
      audio_sha256: current.audio_sha256,
      duration_ms: current.duration_ms,
      elapsed_ms: current.elapsed_ms,
      processing_ms: current.processing_ms,
      transcript: current.transcript,
      independent_truth_available: false,
    };
    check(
      "actual_local_cpu_reference_reaches_vue_review",
      current.transcript?.length > 0 &&
        current.duration_ms === 5592 &&
        (await state(page)).counters.sent === 0,
    );
    await page.screenshot({
      path: path.join(root, "evidence", `voice-${suffix}-review.png`),
      fullPage: true,
    });
    await page.getByTestId("voice-transcript").fill(text);
    await page.getByTestId("voice-adopt").click();
    check(
      "reviewer_can_correct_text_before_parse",
      (await page.getByTestId("text-task-source").inputValue()) === text &&
        (await state(page)).counters.sent === 0,
    );
    check(
      "new_audio_adoption_hides_previous_text_suggestion",
      (await page.getByTestId("text-task-proposal").count()) === 0,
    );
    if (companyModel) {
      await page.getByTestId("text-task-parse").click();
      await until(
        async () => (await state(page)).text_tasks.current?.phase === "review",
        "Corrected text did not reach company model",
        18000,
      );
      const task = (await state(page)).text_tasks.current;
      report.corrected_reference_task = {
        user_corrected_source: true,
        company_model: true,
        original_reference_is_not_a_flight_command: true,
        elapsed_ms: task.elapsed_ms,
        phase: task.phase,
        thinking_enabled: false,
      };
      check(
        "explicit_corrected_text_reaches_actual_company_model",
        task.result.proposal.height_m === 0.5 &&
          task.result.proposal.hold_s === 3 &&
          (await state(page)).task.current === null &&
          (await state(page)).counters.sent === 0,
      );
    }
  }
  mode = "mock";
  await page.getByTestId("voice-record").click();
  await page.getByTestId("voice-recording").waitFor();
  await page.getByRole("button", { name: "监控总览", exact: false }).click();
  await until(
    async () =>
      (await state(page)).voice.current === null &&
      (await page.evaluate(() =>
        window.__voiceQaTracks.every((track) => track.readyState === "ended"),
      )),
    "Page switch left microphone active",
  );
  check(
    "page_unmount_stops_audio_and_discards_transcript",
    (await state(page)).voice.current === null,
  );
  await page
    .getByRole("button", { name: "✎ 文本任务建议", exact: true })
    .click();
  check(
    "returning_does_not_restore_audio_or_transcript",
    (await page.getByTestId("voice-preview").count()) === 0 &&
      (await page.getByTestId("voice-review").count()) === 0,
  );
  await page.getByTestId("voice-record").click();
  await page.getByTestId("voice-recording").waitFor();
  await until(
    async () => (await page.getByTestId("voice-preview").count()) === 1,
    "15 second recording did not finish",
    19000,
  );
  const duration = await page.locator("audio").evaluate(async (audio) => {
    if (Number.isFinite(audio.duration)) return audio.duration;
    await new Promise((resolve) =>
      audio.addEventListener("loadedmetadata", resolve, { once: true }),
    );
    return audio.duration;
  });
  check(
    "fifteen_second_limit_stops_device",
    duration <= 15 &&
      duration >= 14 &&
      (await page.evaluate(() =>
        window.__voiceQaTracks.every((track) => track.readyState === "ended"),
      )),
  );
  await page.getByTestId("voice-record").click();
  await page.getByTestId("voice-recording").waitFor();
  await page.evaluate(() => window.lf2s.restart());
  await until(
    async () =>
      (await state(page)).service_ready &&
      (await page.evaluate(() =>
        window.__voiceQaTracks.every((track) => track.readyState === "ended"),
      )),
    "Backend restart left audio active",
    25000,
  );
  check(
    "backend_restart_discards_audio_and_old_results",
    (await state(page)).voice.current === null &&
      (await page.getByTestId("voice-preview").count()) === 0,
  );
  check("renderer_has_no_errors", rendererErrors.length === 0);
  report.passed = true;
}
main()
  .catch((error) => {
    report.error = String(error.stack || error);
    if (process.env.LF2S_MODEL_API_KEY)
      report.error = report.error.replaceAll(
        process.env.LF2S_MODEL_API_KEY,
        "[redacted]",
      );
  })
  .finally(async () => {
    if (instance) await instance.close();
    if (relay) await new Promise((resolve) => relay.close(resolve));
    if (cpu && cpu.exitCode === null) {
      cpu.stdin.end("shutdown\n");
      await until(
        () => cpu.exitCode !== null,
        "CPU owner did not close",
        15000,
      ).catch((error) => {
        report.error = String(error);
        report.passed = false;
        cpu.kill();
      });
    }
    report.renderer_errors = rendererErrors;
    report.requests = calls;
    fs.writeFileSync(
      path.join(root, "evidence", `voice-${suffix}-live.json`),
      JSON.stringify(report, null, 2),
    );
    console.log(
      JSON.stringify({
        passed: report.passed,
        checks,
        reference: report.reference,
        error: report.error,
        rendererErrors,
      }),
    );
    if (!report.passed) process.exitCode = 1;
  });
