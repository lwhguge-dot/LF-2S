<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { bridge, type State, type TaskPresentation } from "./bridge";
import TextResultPanel from "./TextResultPanel.vue";
import VoicePanel from "./VoicePanel.vue";

const props = defineProps<{
  state: State;
  ready: boolean;
  compact?: boolean;
  initialSource?: string;
}>();
const emit = defineEmits<{ view: [value: TaskPresentation] }>();
const voiceExpanded = ref(false);
const source = ref(props.initialSource || "");
const busy = ref(false);
const error = ref("");
const activeId = ref<string | null>(null);
let revision = 0;
const service = computed(() => props.state.text_tasks);
const current = computed(() =>
  props.ready &&
  !props.state.replay &&
  service.value?.current?.id === activeId.value &&
  service.value?.current?.source_text === source.value
    ? service.value.current
    : null,
);
const status = computed(() =>
  current.value
    ? {
        pending: "正在解析",
        review: "建议待审阅",
        clarify: "需要补充信息",
        rejected: "未通过原文核对",
        error: "解析失败",
      }[current.value.phase]
    : "等待输入",
);
watch(
  [current, status, source, error],
  () =>
    emit("view", {
      current: current.value,
      status: status.value,
      source: source.value,
      error: error.value,
    }),
  { immediate: true },
);
watch([() => props.ready, () => !!props.state.replay], ([ready, replay]) => {
  if (!ready || replay) {
    revision++;
    activeId.value = null;
    voiceExpanded.value = false;
    bridge.textClear().catch(() => {});
  }
});
function setSource(text: string) {
  source.value = text;
}
defineExpose({ setSource });
watch(source, () => {
  revision++;
  activeId.value = null;
  error.value = "";
  if (service.value?.current) bridge.textClear().catch(() => {});
});
async function parse() {
  if (busy.value || !source.value.trim()) return;
  busy.value = true;
  error.value = "";
  const version = revision;
  try {
    await bridge.textClear();
    if (version === revision) {
      const state = await bridge.textParse(source.value);
      if (version === revision) activeId.value = state.current?.id || null;
    }
    if (version !== revision) await bridge.textClear();
  } catch (failure) {
    if (version === revision)
      error.value = String(failure).replace(/^Error: /, "");
  } finally {
    busy.value = false;
  }
}
async function clear() {
  revision++;
  activeId.value = null;
  source.value = "";
  try {
    await bridge.textClear();
  } catch (failure) {
    error.value = String(failure);
  }
}
function adoptVoice(text: string) {
  revision++;
  activeId.value = null;
  source.value = text;
  error.value = "";
  bridge.textClear().catch(() => {});
}
onUnmounted(() => {
  revision++;
  bridge.textClear().catch(() => {});
});
</script>
<template>
  <section
    class="panel detail-panel text-tasks"
    :class="{ 'compact-assistant': compact }"
    data-testid="text-tasks-panel"
  >
    <div class="text-heading">
      <div>
        <span v-if="!compact" class="eyebrow">中文任务 · 原文审阅</span>
        <h2>{{ compact ? "任务助手" : "文本任务建议" }}</h2>
      </div>
      <span class="chip">无思考 · {{ service?.model || "未配置模型" }}</span
      ><button
        v-if="compact"
        @click="voiceExpanded = !voiceExpanded"
        :aria-expanded="voiceExpanded"
        data-testid="workspace-voice-toggle"
      >
        语音输入
      </button>
    </div>
    <p v-if="!compact">
      输入日常任务并核对模型理解；此处生成建议，尚未连接真实飞行执行。
    </p>
    <VoicePanel
      v-if="!compact || voiceExpanded"
      :state="state"
      :ready="ready"
      @adopt="adoptVoice"
    />
    <label for="task-source" :class="{ 'visually-hidden': compact }"
      >任务原文</label
    >
    <div class="task-composer">
      <textarea
        id="task-source"
        v-model="source"
        maxlength="512"
        :rows="compact ? 2 : 4"
        placeholder="例如：去马路边上，然后自己过马路。"
        :disabled="!ready || !!state.replay"
        data-testid="text-task-source"
      />
      <div class="text-controls">
        <button
          :disabled="busy || !ready || !!state.replay || !source.trim()"
          @click="parse"
          data-testid="text-task-parse"
        >
          解析任务建议</button
        ><button
          :disabled="!ready || (!source && !service?.current)"
          @click="clear"
          data-testid="text-task-clear"
        >
          取消并清空</button
        ><small>{{ source.length }} / 512</small>
      </div>
    </div>
    <p v-if="!service?.configured" class="text-muted">
      {{
        service?.configuration_error ||
        "公司模型尚未配置。缺参数检查可直接使用；模型连接按服务说明启动。"
      }}
    </p>
    <p v-if="state.replay" class="notice">
      回放期间不解析实时任务，旧建议已清空。
    </p>
    <p v-if="error" role="alert" class="notice">{{ error }}</p>
    <TextResultPanel
      v-if="!compact"
      :view="{ current, status, source, error }"
    />
  </section>
</template>
<style scoped>
.text-tasks {
  max-width: 1050px;
}
.text-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  flex-wrap: wrap;
}
.text-heading h2,
.text-heading h3 {
  margin: 8px 0;
}
.text-tasks label {
  display: block;
  margin: 22px 0 9px;
  color: var(--text);
}
.text-tasks textarea {
  display: block;
  box-sizing: border-box;
  width: 100%;
  resize: vertical;
  padding: 16px;
  border: 1px solid #dbe3e8;
  border-radius: 9px;
  background: #fbfcfd;
  color: #2d4552;
  font: inherit;
  line-height: 1.7;
}
.text-controls {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  margin: 16px 0;
}
.text-controls small {
  margin-left: auto;
}
.text-result {
  margin-top: 24px;
  padding: 22px;
  border: 1px solid #dbe5e9;
  border-radius: 10px;
  background: #f7fafb;
}
.text-fields {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 18px;
}
.text-fields dt,
.text-muted,
.text-controls small {
  color: #6c7f8b;
}
.text-fields dd {
  margin: 8px 0;
  color: #354f5d;
  font-size: 20px;
}
.text-evidence {
  border-top: 1px solid #2b414d;
  margin-top: 20px;
  padding-top: 12px;
}
.text-evidence p {
  margin: 8px 0;
}
.text-evidence span {
  color: #73cabb;
}
@media (max-width: 1000px) {
  .text-fields {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.compact-assistant {
  min-height: 0;
  max-width: none;
  padding: 16px 20px;
  background: white;
  border-color: #e3e9ed;
  border-radius: 12px;
  box-shadow: 0 3px 16px #1d3a5010;
}
.compact-assistant .text-heading {
  gap: 12px;
  flex-wrap: nowrap;
  margin-bottom: 10px;
}
.compact-assistant .text-heading h2 {
  font-size: 13px;
  margin: 0;
}
.compact-assistant .text-heading > .chip {
  margin-right: auto;
  font-size: 10px;
  background: transparent;
  color: #718491;
  border: 0;
}
.compact-assistant .task-composer {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 12px;
  background: #fbfcfd;
  border: 1px solid #dbe5e9;
  border-radius: 9px;
}
.compact-assistant textarea {
  min-height: 50px;
  max-height: 140px;
  resize: vertical;
  margin: 0;
  padding: 4px;
  border: 0;
  background: transparent;
  color: #2d4552;
  font-size: 13px;
  line-height: 1.7;
  flex: 1;
  min-width: 0;
}
.compact-assistant .text-controls {
  display: grid;
  grid-template-columns: 1fr;
  gap: 5px;
  margin: 0;
  flex-shrink: 0;
}
.compact-assistant .text-controls button {
  font-size: 11px;
  min-height: 32px;
  background: #087e83;
  border-color: #087e83;
  color: white;
}
.compact-assistant .text-controls button + button {
  background: transparent;
  border-color: transparent;
  color: #617985;
  min-height: 24px;
}
.compact-assistant .text-controls small {
  display: none;
}
.compact-assistant > .text-muted {
  font-size: 11px;
  margin: 9px 0 0;
}
.compact-assistant > .notice {
  font-size: 12px;
  padding: 8px;
  margin: 8px 0 0;
}
@media (max-width: 650px) {
  .compact-assistant .task-composer {
    flex-direction: column;
    align-items: stretch;
  }
  .compact-assistant .text-controls {
    display: flex;
    justify-content: end;
    flex-wrap: wrap;
  }
}
</style>
