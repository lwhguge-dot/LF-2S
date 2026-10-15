<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { bridge, type State, type Recording } from "./bridge";
const props = defineProps<{ state: State; ready: boolean }>();
const files = ref<Recording[]>([]),
  selected = ref(""),
  busy = ref(false),
  playing = ref(false),
  message = ref("");
const frame = computed(() => props.state.replay);
let timer: ReturnType<typeof setInterval>;
async function list() {
  try {
    files.value = (await bridge.recordings()).recordings;
    if (!selected.value || !files.value.some((f) => f.id === selected.value))
      selected.value = files.value[0]?.id || "";
  } catch (e) {
    message.value = String(e);
  }
}
async function seek(index: number, open = false) {
  if (busy.value || !props.ready) return;
  busy.value = true;
  message.value = "";
  try {
    await (open ? bridge.replayOpen : bridge.replaySeek)(
      open ? selected.value : frame.value!.recording_id,
      index,
    );
  } catch (e) {
    message.value = String(e);
    playing.value = false;
  } finally {
    busy.value = false;
  }
}
async function close() {
  playing.value = false;
  busy.value = true;
  try {
    await bridge.replayClose();
  } catch (e) {
    message.value = String(e);
  } finally {
    busy.value = false;
  }
}
function text(value: unknown) {
  return value === null
    ? "未知"
    : Array.isArray(value)
      ? value.map((v) => Number(v).toFixed(3)).join(" / ")
      : Number(value).toFixed(3);
}
onMounted(() => {
  void list();
  timer = setInterval(() => {
    if (!playing.value || busy.value || !frame.value) return;
    if (frame.value.index >= frame.value.frames - 1) {
      playing.value = false;
      return;
    }
    void seek(frame.value.index + 1);
  }, 650);
});
onUnmounted(() => clearInterval(timer));
</script>
<template>
  <section class="panel detail-panel replay-panel">
    <span class="eyebrow">S2 · 历史数据 · 只读</span>
    <h2>记录与回放</h2>
    <p class="explanation">
      打开回放会断开当前会话并停止任务续租。回放和拖动时间轴只读取记录，不发送动作；退出后仍需手动建立新会话。
    </p>
    <div class="task-buttons">
      <button :disabled="busy || !ready" @click="list">刷新录制列表</button>
      <select
        v-model="selected"
        aria-label="录制文件"
        :disabled="busy || !ready"
      >
        <option v-for="file in files" :key="file.id" :value="file.id">
          {{ file.started || file.id }} ·
          {{ file.complete ? "已关闭" : "录制中 / 部分记录" }} ·
          {{ file.frames ?? "?" }} 帧
        </option>
      </select>
      <button
        :disabled="busy || !ready || !selected"
        @click="
          playing = false;
          seek(0, true);
        "
      >
        打开只读回放
      </button>
    </div>
    <p class="fine-print">
      快照最多 2 Hz，单份 4 MiB，保留最近 6
      份；满后停止该份录制并累计丢弃数。队列最多 256 条，图像仅存元数据。
    </p>
    <p>
      本次录制：{{ state.recording?.frames ?? 0 }} 帧 · 丢弃
      {{ state.recording?.dropped ?? 0 }} ·
      {{ state.recording?.truncated ? "已达容量，后续丢弃" : "有界写入" }}
      {{ state.recording?.error }}
    </p>
    <p v-if="message" role="alert">{{ message }}</p>
    <div v-if="frame" data-testid="replay-frame">
      <div class="boundary-note">
        <strong>只读回放 · 历史合成数据</strong> ·
        以下有效性是录制当时的判定，不是当前测量。
      </div>
      <div class="task-buttons">
        <button
          :disabled="busy || frame.index <= 0"
          @click="
            playing = false;
            seek(frame.index - 1);
          "
        >
          上一帧
        </button>
        <button
          :disabled="busy || frame.index >= frame.frames - 1"
          @click="playing = !playing"
        >
          {{ playing ? "暂停回放" : "播放记录" }}
        </button>
        <button
          :disabled="busy || frame.index >= frame.frames - 1"
          @click="
            playing = false;
            seek(frame.index + 1);
          "
        >
          下一帧
        </button>
        <button :disabled="busy" @click="close">退出回放</button>
      </div>
      <label class="input-label" for="replay-position"
        >历史帧 {{ frame.index + 1 }} / {{ frame.frames }} · 服务相对时间
        {{ frame.state.service_time_ms }} ms</label
      >
      <input
        id="replay-position"
        type="range"
        min="0"
        :max="frame.frames - 1"
        :value="frame.index"
        :disabled="busy"
        @input="playing = false"
        @change="seek(Number(($event.target as HTMLInputElement).value))"
      />
      <dl class="identity-list">
        <dt>录制编号</dt>
        <dd>{{ frame.recording_id }}</dd>
        <dt>历史会话 u64</dt>
        <dd data-testid="replay-session">
          {{ frame.state.identity?.session_id || "尚未建立" }}
        </dd>
        <dt>历史链路</dt>
        <dd>{{ frame.state.link?.phase }}</dd>
        <dt>历史任务</dt>
        <dd>
          {{ frame.state.task?.current?.stage || "无" }} ·
          {{ frame.state.task?.current?.status || "无" }}
        </dd>
      </dl>
      <div class="replay-measures">
        <article v-for="(sensor, name) in frame.state.sensors" :key="name">
          <strong>{{
            {
              height: "高度",
              position: "位置",
              attitude: "姿态",
              battery: "电压",
            }[name] || name
          }}</strong>
          <p>
            {{ sensor.valid ? text(sensor.value) : "未知" }}
            {{ sensor.valid ? sensor.unit : "" }}
          </p>
          <small
            >录制时 {{ sensor.valid ? "合成有效" : sensor.reason }} · 年龄
            {{ sensor.age_ms ?? "未知" }} ms</small
          >
        </article>
      </div>
      <p class="fine-print">
        {{ frame.image_policy }}。硬件新鲜度资格始终为 false。
      </p>
      <h3>截至当前帧的最近事件（最多 60 条）</h3>
      <div class="event-list">
        <details
          v-for="event in [...frame.events].reverse()"
          :key="event.id"
          class="event-row"
        >
          <summary>{{ event.time }} · {{ event.text }}</summary>
          <pre>{{ JSON.stringify(event, null, 2) }}</pre>
        </details>
      </div>
    </div>
  </section>
</template>
