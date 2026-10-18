<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { State, TaskPresentation } from "./bridge";
import BenchPanel from "./BenchPanel.vue";
import TextTasksPanel from "./TextTasksPanel.vue";
import TextResultPanel from "./TextResultPanel.vue";
import SyntheticPreview from "./SyntheticPreview.vue";
const props = defineProps<{
  state: State;
  ready: boolean;
  active: boolean;
  initialSource?: string;
  initialImageSource?: string;
}>();
const emit = defineEmits<{
  refresh: [state: State];
  tools: [page: string];
  draft: [text: string];
  imageSource: [source: string];
}>();
const source = ref(props.initialImageSource || "camera");
watch(source, (value) => emit("imageSource", value));
const assistant = ref<InstanceType<typeof TextTasksPanel> | null>(null);
const view = ref<TaskPresentation>({
  current: null,
  status: "等待输入",
  source: props.initialSource || "",
  error: "",
});
function updateView(value: TaskPresentation) {
  view.value = value;
  emit("draft", value.source);
}
const current = computed(() =>
  props.ready && props.active && !props.state.replay
    ? view.value.current
    : null,
);
const visibleView = computed<TaskPresentation>(() => ({
  ...view.value,
  current: current.value,
  status: current.value ? view.value.status : "等待输入",
}));
const intent = computed(() => current.value?.result?.proposal?.intent);
const cash = computed(() => intent.value === "inspect_currency_at_counter");
const resultFields = computed(() =>
  cash.value
    ? ["纸币张数", "面额明细", "合计金额"]
    : intent.value === "cross_road" || intent.value === "observe_traffic_signal"
      ? ["信号灯", "是否闪烁", "斑马线通道"]
      : [],
);
const runningTask = computed(() =>
  props.ready &&
  props.state.mode === "simulation" &&
  !props.state.bench?.active &&
  !props.state.usb_fc?.active &&
  !props.state.replay
    ? props.state.task?.current
    : null,
);
function suggest(text: string) {
  assistant.value?.setSource(text);
}
</script>
<template>
  <main class="workbench" data-testid="workbench">
    <h1 class="visually-hidden">任务工作台</h1>
    <div class="workspace">
      <section class="camera-card" aria-labelledby="camera-heading">
        <div class="section-bar">
          <h2 id="camera-heading">摄像头画面</h2>
          <div class="camera-actions">
            <select
              v-model="source"
              aria-label="工作台图像来源"
              :disabled="!!state.replay"
            >
              <option value="camera">实时相机</option>
              <option value="synthetic">本机模拟</option></select
            ><button
              class="button small"
              @click="emit('tools', source === 'camera' ? 'bench' : 'overview')"
            >
              {{ source === "camera" ? "相机设置" : "模拟连接" }}
            </button>
          </div>
        </div>
        <div v-if="state.replay" class="empty-camera replay-camera">
          <span class="empty-kicker">只读回放</span>
          <h3>历史图像仅录制了元数据</h3>
          <p>这里不使用实时画面冒充历史图像。</p>
          <button class="button" @click="emit('tools', 'replay')">
            查看回放与退出
          </button>
        </div>
        <BenchPanel
          v-else-if="active && source === 'camera'"
          :state="state"
          :service-ready="ready"
          embedded
          @refresh="emit('refresh', $event)"
        />
        <SyntheticPreview
          v-else-if="active"
          :state="state"
          :ready="ready"
          @tools="emit('tools', 'overview')"
        />
        <div class="camera-footer">
          <span>{{
            source === "camera" ? "前向安装待核实" : "合成来源 · 非实拍"
          }}</span
          ><span>实时识别尚未接入</span>
        </div>
      </section>
      <aside class="task-column">
        <section class="task-card" aria-labelledby="task-heading">
          <div class="section-bar">
            <h2 id="task-heading">当前任务</h2>
            <span class="subtle-label">{{
              current ? "建议审阅" : "等待指令"
            }}</span>
          </div>
          <div v-if="!current && !view.source" class="idle-task">
            <div class="ready-mark">准备开始</div>
            <h3>这次需要完成什么？</h3>
            <p>在下方输入一句日常指令，也可以选择一个场景填入原文。</p>
            <div class="scenario-buttons">
              <button
                class="button"
                @click="suggest('去马路边上，然后自己过马路。')"
              >
                过马路</button
              ><button
                class="button"
                @click="suggest('去收银台，看看一共有多少钱。')"
              >
                清点纸币
              </button>
            </div>
            <p class="sample-note">填入后先核对理解，不会自动启动。</p>
          </div>
          <div v-else class="review-task">
            <p v-if="!current" class="draft-state">
              原文已编辑，等待重新解析。
            </p>
            <TextResultPanel :view="visibleView" />
            <p v-if="view.error" role="alert">{{ view.error }}</p>
          </div>
          <div v-if="runningTask" class="simulation-task">
            <span class="subtle-label">本机模拟任务</span
            ><strong>{{ runningTask.status }}</strong>
            <p>
              {{ runningTask.stage }} ·
              {{ runningTask.reason || "结果来自本机模型" }}
            </p>
            <button class="button small" @click="emit('tools', 'tasks')">
              查看模拟任务与取消
            </button>
          </div>
        </section>
        <section class="result-card" aria-labelledby="result-heading">
          <div class="section-bar">
            <h2 id="result-heading">识别结果</h2>
            <span class="subtle-label">尚无有效结果</span>
          </div>
          <div class="result-body">
            <dl v-if="resultFields.length" class="result-fields">
              <div v-for="field in resultFields" :key="field">
                <dt>{{ field }}</dt>
                <dd>未知</dd>
              </div>
            </dl>
            <p v-else class="result-placeholder">结果会与对应画面一起出现。</p>
            <p class="sample-note">相机显示与任务建议均不产生识别结果。</p>
          </div>
        </section>
        <div class="status-strip">
          <span
            >相机
            <b>{{
              ready && state.bench?.phase === "online" ? "在线" : "未连接"
            }}</b></span
          ><span
            >飞控
            <b>{{
              ready && state.usb_fc?.phase === "online" ? "USB只读" : "未接入"
            }}</b></span
          ><span
            >模型
            <b>{{
              state.text_tasks?.configured ? "已配置" : "未配置"
            }}</b></span
          >
        </div>
      </aside>
    </div>
    <div class="assistant">
      <TextTasksPanel
        ref="assistant"
        :state="state"
        :ready="ready && active"
        :initial-source="initialSource"
        compact
        @view="updateView"
      />
    </div>
  </main>
</template>
<style scoped src="./workbench.css"></style>
<style scoped>
.workbench {
  width: 100%;
  min-width: 0;
  --workspace-picture-height: clamp(300px, calc(100dvh - 540px), 580px);
  font-family: "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
  color: #23343f;
  --muted: #6c7d89;
  --line: #e3e9ed;
  --accent: #087e83;
  --accent-soft: #eef8f7;
  --shadow: 0 3px 16px #1d3a5010;
  max-width: 1740px;
  padding: 26px 32px 30px;
  margin: auto;
}
.assistant {
  padding: 0;
  border: 0;
  box-shadow: none;
  background: transparent;
}
.camera-actions select {
  font-size: 11px;
  background: #fbfcfd;
  border-color: #dbe3e8;
  color: #617b88;
  max-width: 125px;
}
.camera-card :deep(.bench-embedded) {
  display: block;
}
.camera-card :deep(.bench-embedded .bench-camera) {
  border: 0;
  border-radius: 0;
  box-shadow: none;
  background: white;
}
.camera-card :deep(.bench-embedded .bench-camera > .panel-heading),
.camera-card :deep(.bench-embedded .bench-sidebar),
.camera-card :deep(.bench-embedded .image-tools),
.camera-card :deep(.bench-embedded .bench-metrics),
.camera-card :deep(.bench-embedded .preview-settings),
.camera-card :deep(.bench-embedded .bench-note) {
  display: none;
}
.camera-card :deep(.bench-embedded .bench-stage) {
  height: var(--workspace-picture-height);
  background: #edf1f4;
}
.camera-card :deep(.bench-embedded .bench-placeholder strong) {
  font-size: 21px;
  font-weight: 500;
  color: #536c7a;
}
.camera-card :deep(.bench-embedded .bench-placeholder span) {
  color: #748792;
}
.camera-card :deep(.bench-embedded .image-watermark) {
  font-size: 10px;
  background: #f5f8facf;
  color: #68808d;
  border-color: #dce8ed;
}
.camera-card :deep(.bench-embedded .bench-frame) {
  height: 43px;
  padding: 12px 20px;
  font-size: 11px;
  color: #74838e;
  background: white;
  border: 0;
}
.camera-card > .camera-footer {
  border-top: 1px solid #edf1f4;
  padding: 7px 20px;
  font-size: 10px;
}
.task-column {
  height: calc(var(--workspace-picture-height) + 134px);
}
.idle-task {
  padding: 21px;
}
.idle-task h3 {
  font-size: 18px;
  margin: 0 0 10px;
}
.idle-task p {
  font-size: 12px;
  line-height: 1.85;
  color: #6c7f8b;
  margin: 0 0 18px;
}
.idle-task .sample-note {
  margin-top: 12px;
  font-size: 11px;
}
.review-task {
  padding: 10px 18px;
}
.review-task :deep(.text-result) {
  border: 0;
  padding: 0;
  margin: 0;
  background: transparent;
  color: #354f5d;
}
.review-task :deep(h3) {
  font-size: 16px;
}
.review-task :deep(.text-fields) {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
}
.review-task :deep(.text-fields div) {
  font-size: 12px;
}
.review-task :deep(.text-fields dd) {
  margin: 3px 0 0;
  color: #354f5d;
  font-size: 13px;
}
.review-task :deep(.text-evidence) {
  font-size: 11px;
}
.draft-state {
  font-size: 12px;
  color: #6c7f8b;
}
.simulation-task {
  padding: 16px 20px;
  border-top: 1px solid #e3e9ed;
  display: grid;
  gap: 7px;
  font-size: 12px;
}
.simulation-task p {
  font-size: 11px;
  margin: 0;
}
.replay-camera {
  height: var(--workspace-picture-height);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
}
.task-column .task-card {
  min-height: 0;
  overflow-y: auto;
}
.task-column .result-card {
  overflow: hidden;
}
.workbench .button {
  color: #344d5c;
  background: white;
  border-color: #dbe3e8;
}
@media (max-width: 760px) {
  .workbench {
    padding: 18px 14px;
    --workspace-picture-height: 300px;
  }
  .task-column {
    height: auto;
  }
  .camera-actions {
    gap: 5px;
  }
  .camera-actions select {
    max-width: 105px;
  }
}
</style>
