// Actual Vue controls and loopback protocol, in both development and frozen bundles.
const path = require("node:path");

async function verifyS2({
  page,
  check,
  state,
  eventually,
  scenario,
  connect,
  output,
  suffix,
}) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const nav = (name) =>
    page.getByRole("button", { name, exact: false }).click();
  const button = (name) => page.getByRole("button", { name, exact: true });
  const consent = () =>
    page.getByRole("checkbox", {
      name: "我确认本次仅运行本机模拟任务（非实机许可）",
    });
  async function prepare() {
    await nav("模拟任务");
    await button("重置并准备模拟器").click();
    await eventually(
      async () =>
        (await state(page)).task_fixture &&
        (await state(page)).link.phase === "disconnected",
      "fixture did not reset",
    );
    await button("连接任务模拟器").click();
    await eventually(
      async () => (await state(page)).sensors.height.valid,
      "plant not ready",
    );
    await button("读取模拟任务配置").click();
    await eventually(
      async () => !!(await state(page)).profile,
      "fixture profile missing",
    );
  }
  async function start() {
    await consent().check();
    await button("启动模拟任务").click();
    await eventually(
      async () => (await state(page)).task.current?.stage === "TAKEOFF",
      "task did not begin",
    );
  }
  await prepare();
  check(
    "task_button_requires_explicit_new_confirmation",
    await button("启动模拟任务").isDisabled(),
  );
  await start();
  check(
    "accepted_does_not_mean_task_completed",
    (await state(page)).task.current.status === "in_progress",
  );
  await page.screenshot({
    path: path.join(output, suffix + "-task-active.png"),
    fullPage: true,
  });
  await eventually(
    async () => (await state(page)).task.current.status === "completed",
    "task did not complete",
    18000,
  );
  await page.getByTestId("task-status").filter({ hasText: "已完成" }).waitFor();
  const taskFinal = await state(page);
  check(
    "actual_buttons_complete_sample_driven_simulated_task",
    taskFinal.sensors.height.value === 0 &&
      taskFinal.qualification.lease_id === 0 &&
      taskFinal.qualification.control_allowed === false &&
      ["BEGIN", "TAKEOFF", "HOLD", "LAND", "END"].every((name) =>
        taskFinal.operations.some(
          (o) => o.name === name && o.status === "completed",
        ),
      ),
  );
  check(
    "confirmation_is_consumed_after_start",
    await button("启动模拟任务").isDisabled(),
  );
  await page.screenshot({
    path: path.join(output, suffix + "-task-completed.png"),
    fullPage: true,
  });
  const taskSession = taskFinal.identity.session_id;
  const taskRecording = taskFinal.recording.id;
  const entries = await page.evaluate(() => window.lf2s.recordings());
  const recorded = entries.recordings.find((r) => r.id === taskRecording);
  check(
    "bounded_recording_contains_multiple_frames",
    recorded.frames > 8 && recorded.bytes <= 4 * 1024 * 1024,
  );
  await nav("只读回放");
  await page
    .getByLabel("录制文件", { exact: true })
    .selectOption(taskRecording);
  const beforeReplay = (await state(page)).counters.sent;
  await button("打开只读回放").click();
  await page.getByTestId("replay-frame").waitFor();
  const slider = page.getByRole("slider");
  await slider.focus();
  await slider.press("End");
  await eventually(
    async () => (await state(page)).replay.index >= recorded.frames - 2,
    "replay slider did not seek",
  );
  await page
    .getByTestId("replay-session")
    .filter({ hasText: taskSession })
    .waitFor();
  const replayState = await state(page);
  check(
    "readonly_replay_seek_preserves_u64_without_requests",
    replayState.link.phase === "disconnected" &&
      replayState.read_only &&
      replayState.counters.sent === beforeReplay &&
      replayState.replay.state.identity.session_id === taskSession &&
      BigInt(taskSession) > BigInt(Number.MAX_SAFE_INTEGER),
  );
  const lastIndex = replayState.replay.index;
  await button("上一帧").click();
  await eventually(
    async () => (await state(page)).replay.index === lastIndex - 1,
    "previous frame did not load",
  );
  await button("播放记录").click();
  await eventually(
    async () => (await state(page)).replay.index >= lastIndex,
    "playback did not advance",
  );
  await page.screenshot({
    path: path.join(output, suffix + "-replay.png"),
    fullPage: true,
  });
  await button("退出回放").click();
  await eventually(
    async () => !(await state(page)).replay,
    "replay did not close",
  );
  check(
    "replay_exit_keeps_disconnected_without_action_replay",
    (await state(page)).link.phase === "disconnected" &&
      (await state(page)).counters.sent === beforeReplay,
  );

  await prepare();
  await start();
  await nav("监控总览");
  await scenario(page, "manual_takeover");
  await eventually(
    async () => !(await state(page)).task.running,
    "manual did not interrupt",
  );
  check(
    "manual_input_latches_before_reconnect",
    (await state(page)).qualification.manual_latched === true,
  );
  await button("断开").click();
  const priorHello = (await state(page)).operations
    .filter((o) => o.name === "HELLO")
    .at(-1).operation_id;
  await button("连接模拟设备").click();
  await eventually(
    async () =>
      (await state(page)).operations.filter((o) => o.name === "HELLO").at(-1)
        ?.operation_id !== priorHello &&
      (await state(page)).operations.filter((o) => o.name === "HELLO").at(-1)
        ?.status !== "pending",
    "HELLO not answered after manual takeover",
  );
  const afterManualReconnect = await state(page);
  const helloAfterManual = afterManualReconnect.operations
    .filter((o) => o.name === "HELLO")
    .at(-1);
  check(
    "manual_takeover_latched_across_ui_reconnect",
    !afterManualReconnect.task.running &&
      (afterManualReconnect.qualification.manual_latched === true ||
        (helloAfterManual.status === "rejected" &&
          helloAfterManual.reason === "BUSY")),
  );

  await prepare();
  await start();
  await page.evaluate(() => window.lf2s.restart());
  await eventually(
    async () => (await state(page)).service_ready,
    "service failed restart",
  );
  const finalState = await state(page);
  check(
    "restart_during_task_cannot_restore_old_actions",
    !finalState.task.running &&
      finalState.task.current === null &&
      finalState.link.phase === "disconnected",
  );
  return finalState.service_pid;
}
module.exports = { verifyS2 };
