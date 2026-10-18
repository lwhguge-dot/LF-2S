<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  bridge,
  type Capture,
  type RealCapture,
  type State,
  type TaskPresentation,
  type VisionCapture,
} from "./bridge";
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
const analysisLocked = ref(false);
const currentCapture = ref<Capture | RealCapture | null>(null);
const analysisImage = ref("");
const visionError = ref("");
const visionRequestId = ref("");
const now = ref(Date.now());
const telemetryAt = ref(Date.now());
watch(() => props.state, () => { telemetryAt.value = Date.now(); });
const uartAttitude = computed(() => {
  const sensor = props.state.sensors?.attitude;
  const elapsed = now.value - telemetryAt.value;
  if (!props.ready || props.state.replay || props.state.link?.phase !== "online" ||
      elapsed + (props.state.link.state_age_ms ?? 1000) >= 1000 ||
      !sensor?.valid || sensor.source !== "uart1" || elapsed + (sensor.age_ms ?? 300) >= 300 ||
      !Array.isArray(sensor.value)) return "未知";
  return sensor.value.map(v => `${(v * 180 / Math.PI).toFixed(1)}°`).join(" / ");
});
const uartAgeText = computed(() => {
  const sensor = props.state.sensors?.attitude;
  return sensor?.source === "uart1" && sensor?.age_ms != null ? `${sensor.age_ms} ms` : "未知";
});
let clock: ReturnType<typeof setInterval>;
watch(source, (value) => {
  void clearVision();
  currentCapture.value = null;
  emit("imageSource", value);
});
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
const vision = computed(() => props.state.vision);
const visionCurrent = computed(() => vision.value?.current || null);
const visionObservation = computed(() => visionCurrent.value?.observation || null);
const captureFresh = computed(
  () =>
    !!currentCapture.value &&
    now.value - currentCapture.value.received_at >= 0 &&
    now.value - currentCapture.value.received_at < 2000,
);
const cameraOnline = computed(() =>
  source.value === "camera"
    ? props.state.bench?.active === true && props.state.bench.phase === "online"
    : props.state.link?.phase === "online" &&
      !props.state.replay &&
      !props.state.bench?.active &&
      !props.state.usb_fc?.active,
);
const analysisStatus = computed(() =>
  !vision.value?.configured
    ? "模型未配置"
    : vision.value.running
      ? "正在观察"
      : visionCurrent.value?.phase === "ready"
        ? "待人工核对"
        : visionCurrent.value?.phase === "error"
          ? "观察不可用"
          : "尚无结果",
);
const visualCurrency = computed(() => visionObservation.value?.currency || null);
function moneyText(fen: number | null | undefined) {
  return typeof fen === "number" ? `¥${(fen / 100).toFixed(2)}` : "未知";
}
const signalText = (light: string) =>
  ({
    red: "红灯",
    yellow: "黄灯",
    green: "绿灯",
    off: "未亮",
    ambiguous: "颜色不明确",
    unknown: "未知",
  })[light] || "未知";
const visibleText = (value: boolean | null | undefined) =>
  value === true ? "模型报告可见" : value === false ? "模型报告未见" : "未知";
function captureKey(frame: Capture | RealCapture) {
  return [frame.synthetic, frame.stream_id, frame.frame_id].join(":");
}
function visionCapture(frame: Capture | RealCapture): VisionCapture {
  const common = {
    bytes: Uint8Array.from(frame.bytes),
    frame_id: frame.frame_id,
    stream_id: frame.stream_id,
    service_received_ms: frame.service_received_ms,
  };
  return frame.synthetic
    ? { ...common, synthetic: true }
    : {
        ...common,
        synthetic: false,
        epoch: frame.epoch,
        capture_us: frame.capture_us,
      };
}
function receiveCapture(frame: Capture | RealCapture) {
  if (analysisLocked.value) return;
  const prior = currentCapture.value;
  if (prior && captureKey(prior) !== captureKey(frame) && visionCurrent.value)
    void clearVision();
  currentCapture.value = frame;
}
function clearAnalysisImage() {
  if (analysisImage.value) URL.revokeObjectURL(analysisImage.value);
  analysisImage.value = "";
}
async function clearVision() {
  const requestId = visionRequestId.value || visionCurrent.value?.request_id || "";
  visionRequestId.value = "";
  visionError.value = "";
  clearAnalysisImage();
  if (requestId && props.ready) {
    try {
      await bridge.visionCancel(requestId);
    } catch {
      /* A closed or restarted service has already discarded its observations. */
    }
  }
}
async function analyzeCurrentFrame() {
  const frame = currentCapture.value;
  if (!frame || !captureFresh.value || !cameraOnline.value || !vision.value?.configured)
    return;
  analysisLocked.value = true;
  visionError.value = "";
  clearAnalysisImage();
  analysisImage.value = URL.createObjectURL(
    new Blob([Uint8Array.from(frame.bytes).buffer], { type: "image/jpeg" }),
  );
  try {
    const result = await bridge.visionAnalyze(visionCapture(frame));
    const requestId = result.current?.request_id;
    if (!requestId) throw new Error("后台没有接受当前图像");
    visionRequestId.value = requestId;
  } catch (error) {
    analysisLocked.value = false;
    clearAnalysisImage();
    visionError.value = String(error).replace(/^Error: /, "");
  }
}
async function cancelAnalysis() {
  const requestId = visionRequestId.value || visionCurrent.value?.request_id;
  if (requestId) await clearVision();
  analysisLocked.value = false;
}
async function resumePreview() {
  await clearVision();
  analysisLocked.value = false;
}
watch(
  [() => props.ready, () => props.active, () => !!props.state.replay, cameraOnline],
  ([ready, active, replay, online]) => {
    if (!ready || !active || replay || !online) {
      void clearVision();
      analysisLocked.value = false;
      currentCapture.value = null;
    }
  },
);
watch(
  [
    () => props.state.bench?.epoch,
    () => props.state.bench?.health?.camera_stream_id,
    () => props.state.identity?.boot_id,
    () => props.state.identity?.session_id,
  ],
  () => {
    if (visionCurrent.value) {
      void clearVision();
      analysisLocked.value = false;
      currentCapture.value = null;
    }
  },
);
function suggest(text: string) {
  assistant.value?.setSource(text);
}
onMounted(() => {
  clock = setInterval(() => (now.value = Date.now()), 200);
});
onUnmounted(() => {
  clearInterval(clock);
  void clearVision();
});
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
            <button
              v-if="!analysisLocked"
              class="button small analyze-button"
              data-testid="vision-analyze"
              :disabled="
                !ready || !cameraOnline || !captureFresh || !vision?.configured
              "
              @click="analyzeCurrentFrame"
            >
              分析当前画面
            </button>
            <button
              v-else-if="vision?.running"
              class="button small"
              data-testid="vision-cancel"
              @click="cancelAnalysis"
            >
              取消分析
            </button>
            <button
              v-else
              class="button small"
              data-testid="vision-resume"
              @click="resumePreview"
            >
              继续实时画面
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
          :analysis-locked="analysisLocked"
          @capture="receiveCapture"
        />
        <SyntheticPreview
          v-else-if="active"
          :state="state"
          :ready="ready"
          :analysis-locked="analysisLocked"
          @tools="emit('tools', 'overview')"
          @capture="receiveCapture"
        />
        <div class="camera-footer">
          <span>{{
            source === "camera" ? "前向安装待核实" : "合成来源 · 非实拍"
          }}</span
          ><span>单帧观察 · 显式触发</span>
        </div>
      </section>
      <aside class="task-column" :class="{ 'uart-connected': state.gateway?.active }">
        <section v-if="state.gateway?.active" class="task-card uart1-card" data-testid="workbench-uart1">
          <div class="section-bar"><h2>飞控实时状态</h2><span class="subtle-label">UART1 · 只读</span></div>
          <div class="uart1-body">
            <p>{{ ready && state.link?.phase === 'online' ? '遥测在线' : '遥测待恢复' }} · 样本年龄 {{ uartAgeText }}</p>
            <p>横滚 / 俯仰 / 相对偏航</p>
            <strong data-testid="workbench-uart1-attitude">{{ uartAttitude }}</strong>
            <small v-if="state.link?.reason">{{ state.link.reason }}</small>
          </div>
        </section>
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
            <span class="subtle-label" data-testid="vision-status">{{ analysisStatus }}</span>
          </div>
          <div class="result-body">
            <dl v-if="resultFields.length" class="result-fields">
              <div v-for="field in resultFields" :key="field">
                <dt>{{ field }}</dt>
                <dd>未知</dd>
              </div>
            </dl>
            <p v-else class="result-placeholder">结果会与对应画面一起出现。</p>
            <section
              v-if="analysisImage && visionCurrent"
              class="vision-review"
              data-testid="vision-review"
              aria-label="单帧图像观察结果"
            >
              <img
                :src="analysisImage"
                alt="与模型观察绑定的冻结相机帧"
                data-testid="vision-frame-image"
              />
              <div class="vision-frame-label">
                <strong data-testid="vision-frame-id">{{ visionCurrent.frame?.frame_id || "待关联" }}</strong>
                <span>{{ visionCurrent.frame?.source === "synthetic" ? "本机合成 · 非实拍" : "ESP32 实拍显示" }}</span>
              </div>
              <p v-if="visionCurrent.phase === 'pending'" role="status">
                正在分析这张画面，预览已暂停。
              </p>
              <template v-else-if="visionObservation?.phase === 'ready'">
                <dl class="vision-fields">
                  <div>
                    <dt>信号灯</dt>
                    <dd v-if="visionObservation.signals.length">
                      <span v-for="(signal, index) in visionObservation.signals" :key="index">
                        灯组 {{ index + 1 }}：{{ signalText(signal.light) }}
                      </span>
                    </dd>
                    <dd v-else>未知（未报告可辨认灯组）</dd>
                  </div>
                  <div>
                    <dt>闪烁</dt>
                    <dd>单帧无法判断</dd>
                  </div>
                  <div>
                    <dt>斑马线</dt>
                    <dd>{{ visibleText(visionObservation.zebra_visible) }}</dd>
                  </div>
                  <div>
                    <dt>收银台</dt>
                    <dd>{{ visibleText(visionObservation.checkout_visible) }}</dd>
                  </div>
                  <template v-if="visualCurrency">
                    <div>
                      <dt>已识别纸币</dt>
                      <dd>{{ visualCurrency.recognized_count ?? "未知" }} 张</dd>
                    </div>
                    <div>
                      <dt>可见范围合计</dt>
                      <dd>
                        {{ moneyText(visualCurrency.visible_total_fen) }}
                        <span v-if="visualCurrency.visible_total_fen !== null">· 暂定</span>
                      </dd>
                    </div>
                    <div>
                      <dt>柜台总额</dt>
                      <dd>未知</dd>
                    </div>
                  </template>
                </dl>
                <p class="vision-evidence">
                  qwen38 · {{ visionObservation.model_elapsed_ms ?? "—" }} ms ·
                  许可：无 · 飞行动作：未连接
                </p>
              </template>
              <p v-else role="alert">
                观察不可用：{{ visionCurrent.error || visionObservation?.reason || "未知原因" }}
                <span v-if="visionObservation?.provider_error">（{{ visionObservation.provider_error }}）</span>
              </p>
            </section>
            <p v-if="visionError" role="alert">{{ visionError }}</p>
            <p class="sample-note">
              模型报告须人工核对；单帧结果不判断闪烁、通行许可或柜台总额。
            </p>
          </div>
        </section>
        <div class="status-strip" data-testid="workbench-connection-strip">
          <span
            >相机
            <b>{{
              ready && state.bench?.phase === "online" ? "在线" : "未连接"
            }}</b></span
          ><span
            >飞控
            <b>{{
              ready && state.gateway?.active
                ? state.link?.phase === "online" ? "UART1只读" : "UART1待恢复"
                : ready && state.usb_fc?.phase === "online" ? "USB只读" : "未接入"
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
.camera-actions {
  flex-wrap: wrap;
  justify-content: end;
}
.analyze-button:disabled {
  cursor: not-allowed;
  color: #8b989f;
  background: #f4f6f7;
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
.task-column .task-card {
  flex: 0 1 45%;
}
.task-column .result-card {
  flex: 1 1 55%;
  min-height: 0;
  overflow-y: auto;
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
  overflow-x: hidden;
}
.vision-review {
  display: grid;
  gap: 8px;
  padding-top: 12px;
  margin-top: 12px;
  border-top: 1px solid #e7edef;
}
.vision-review > img {
  display: block;
  width: 100%;
  max-height: 118px;
  object-fit: contain;
  background: #1c2930;
  border-radius: 4px;
}
.vision-frame-label {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: baseline;
  color: #526a76;
  font-size: 11px;
}
.vision-frame-label span {
  color: #778994;
  text-align: right;
}
.vision-review > p {
  margin: 0;
  color: #6b7d87;
  font-size: 11px;
  line-height: 1.6;
}
.vision-fields {
  display: grid;
  gap: 8px;
  margin: 0;
}
.vision-fields > div {
  display: grid;
  grid-template-columns: minmax(88px, 0.8fr) minmax(0, 1.2fr);
  gap: 8px;
  align-items: start;
  font-size: 11px;
}
.vision-fields dt {
  color: #74848d;
}
.vision-fields dd {
  display: grid;
  gap: 3px;
  margin: 0;
  color: #304b59;
  text-align: right;
  overflow-wrap: anywhere;
}
.vision-evidence {
  color: #819099 !important;
  font-size: 10px !important;
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
  .task-column .task-card,
  .task-column .result-card {
    flex: initial;
  }
  .camera-actions {
    gap: 5px;
  }
  .camera-actions select {
    max-width: 105px;
  }
}
</style>
