<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { bridge, type State } from "./bridge";
const props = defineProps<{ state: State; ready: boolean }>();
const confirmed = ref(false),
  busy = ref(false),
  message = ref("");
const online = computed(
  () =>
    props.ready && props.state.link?.phase === "online" && !props.state.replay,
);
const task = computed(() => props.state.task?.current);
const running = computed(() => !!props.state.task?.running);
const hardware = computed(
  () => !!props.state.bench?.active || !!props.state.usb_fc?.active,
);
const steps = computed(() =>
  (props.state.operations || []).filter((o) =>
    task.value?.steps.includes(o.operation_id),
  ),
);
watch(
  () => props.state.identity?.session_id,
  () => (confirmed.value = false),
);
async function act(action: () => Promise<unknown>) {
  if (busy.value) return;
  busy.value = true;
  message.value = "";
  try {
    await action();
  } catch (error) {
    message.value = String(error);
  } finally {
    busy.value = false;
  }
}
function start() {
  const permitted = confirmed.value;
  confirmed.value = false;
  return act(() => bridge.taskStart(permitted));
}
function prepare() {
  return act(() => bridge.scenario("task_bench"));
}
</script>
<template>
  <section class="panel detail-panel task-panel">
    <span class="eyebrow">S2 · 无硬件 · 固定模拟示例</span>
    <h2>模拟任务闭环</h2>
    <p class="explanation">
      模拟起飞至 0.50 m → 保持 →
      降落并上锁。完成由新的有效合成测量和协议最终结果确认。这里没有连接真实电机，也不代表实机飞行已实现。
    </p>
    <ol class="task-guide">
      <li>
        重置并准备模拟器，然后连接并读取本次模拟配置。重置会更换 boot 和会话。
      </li>
      <li>
        勾选本次许可并启动。后台依次发送 PERMIT → ACQUIRE → BEGIN → 受限动作。
      </li>
      <li>
        可在「监控总览」切换断链、传感器停止更新、结果丢失或人工接管，观察中断。旧任务不会自动恢复。
      </li>
    </ol>
    <div class="task-buttons">
      <button
        :disabled="busy || !ready || !!state.replay || running || hardware"
        @click="prepare"
      >
        重置并准备模拟器
      </button>
      <button
        :disabled="
          busy ||
          !ready ||
          !!state.replay ||
          hardware ||
          state.link?.phase !== 'disconnected'
        "
        @click="act(bridge.connect)"
      >
        连接任务模拟器
      </button>
      <button
        :disabled="busy || !online || running"
        @click="act(bridge.profile)"
      >
        读取模拟任务配置
      </button>
    </div>
    <div class="boundary-note">
      测试资格：{{
        state.task_fixture
          ? "仅本机模型具备测试预检、着地与配置条件"
          : "尚未准备任务模拟器"
      }}。生产配置与真实硬件始终未签认，图像不具备控制资格。
    </div>
    <label class="check task-confirm"
      ><input
        v-model="confirmed"
        type="checkbox"
        :disabled="busy || running || !online || !state.task_fixture"
      />我确认本次仅运行本机模拟任务（非实机许可）</label
    >
    <div class="task-buttons">
      <button
        class="primary"
        :disabled="
          busy ||
          !online ||
          !confirmed ||
          running ||
          !state.profile ||
          !!state.task?.unknown_request_id
        "
        @click="start"
      >
        启动模拟任务
      </button>
      <button
        :disabled="busy || !running || !!state.replay"
        @click="act(bridge.taskCancel)"
      >
        取消模拟任务
      </button>
      <button
        :disabled="
          busy || running || !online || !state.task?.unknown_request_id
        "
        @click="act(bridge.taskQuery)"
      >
        查询原请求结果
      </button>
    </div>
    <p v-if="message" role="alert">{{ message }}</p>
    <div class="task-status" data-testid="task-status">
      <h3>
        {{
          task ? `任务 #${task.task_id} · ${task.stage}` : "尚未启动模拟任务"
        }}
      </h3>
      <p>
        {{
          task?.reason ||
          "ACCEPTED / IN_PROGRESS 仅表示接受 / 进行中，COMPLETED 才是匹配的最终完成结果。"
        }}
      </p>
      <code v-if="task"
        >状态 {{ task.status }} · session {{ task.session_id }} · request
        {{ task.request_id ?? "尚未分配" }}</code
      >
      <p v-if="task?.queried_result">
        原请求 #{{ task.queried_result.request_id }}：{{
          task.queried_result.phase
        }}
        / {{ task.queried_result.reason }}。查询不会继续旧流程。
      </p>
    </div>
    <div class="operation-table">
      <div v-for="step in steps" :key="step.operation_id">
        <strong>{{ step.name }}</strong
        ><code>#{{ step.request_id }}</code
        ><span>{{ step.protocol_phase || step.status }}</span
        ><span>{{ step.reason }}</span>
      </div>
    </div>
    <p class="fine-print">
      模拟器人工接管同样锁存。普通重连、恢复健康场景都不能解除；只有显式重置整个模拟器才创建新的测试启动代数。这不是实际遥控器解除条件。
    </p>
  </section>
</template>
