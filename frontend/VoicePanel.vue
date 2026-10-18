<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { bridge, type State } from "./bridge";
import { recordPcm, type PcmRecorder } from "./audio";

const props = defineProps<{ state: State; ready: boolean }>();
const emit = defineEmits<{ adopt: [text: string] }>();
const busy = ref(false);
const recording = ref(false);
const error = ref("");
const transcript = ref("");
const clip = ref<ArrayBuffer | null>(null);
const clipURL = ref("");
const clipName = ref("");
const activeId = ref<string | null>(null);
const adopted = ref(false);
let revision = 0;
let abort: AbortController | undefined;
let recorder: PcmRecorder | undefined;
const voice = computed(() => props.state.voice);
const available = computed(() => props.ready && !props.state.replay);
const current = computed(() =>
  available.value && voice.value?.current?.id === activeId.value
    ? voice.value.current
    : null,
);
const pending = computed(() => current.value?.phase === "pending");
watch(
  () => current.value?.transcript,
  (value) => {
    transcript.value = value || "";
  },
);
watch(available, (value) => {
  if (!value) void clear();
});

function discardClip() {
  if (clipURL.value) URL.revokeObjectURL(clipURL.value);
  clipURL.value = "";
  clip.value = null;
  clipName.value = "";
}
function reset() {
  revision++;
  abort?.abort();
  abort = undefined;
  recorder?.cancel();
  recorder = undefined;
  recording.value = false;
  busy.value = false;
  activeId.value = null;
  transcript.value = "";
  adopted.value = false;
  error.value = "";
  discardClip();
  void bridge.microphone(false).catch(() => {});
}
async function clear() {
  reset();
  await bridge.voiceClear().catch(() => {});
}
function setClip(raw: ArrayBuffer, name: string) {
  discardClip();
  clip.value = raw;
  clipName.value = name;
  clipURL.value = URL.createObjectURL(new Blob([raw], { type: "audio/wav" }));
}
async function record() {
  if (!available.value || busy.value || recording.value) return;
  reset();
  const version = revision;
  busy.value = true;
  abort = new AbortController();
  const signal = abort.signal;
  try {
    await bridge.voiceClear();
    if (version !== revision) return;
    await bridge.microphone(true);
    if (version !== revision) return;
    const active = await recordPcm(signal);
    if (version !== revision) {
      active.cancel();
      return;
    }
    recorder = active;
    recording.value = true;
    busy.value = false;
    try {
      const raw = await active.finished;
      if (version === revision) setClip(raw, "本次普通话录音");
    } finally {
      if (version === revision) {
        recording.value = false;
        recorder = undefined;
      }
    }
  } catch (failure) {
    if (version === revision)
      error.value = "录音失败：" + String(failure).replace(/^Error: /, "");
  } finally {
    if (version === revision) {
      busy.value = false;
      void bridge.microphone(false).catch(() => {});
    }
  }
}
async function importWav(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file || !available.value) return;
  reset();
  const version = revision;
  busy.value = true;
  try {
    if (file.size < 44 || file.size > 500000)
      throw new Error("请选择不超过 500KB 的 PCM WAV");
    await bridge.voiceClear();
    const raw = await file.arrayBuffer();
    if (version === revision) setClip(raw, file.name);
  } catch (failure) {
    if (version === revision)
      error.value = String(failure).replace(/^Error: /, "");
  } finally {
    if (version === revision) busy.value = false;
  }
}
async function transcribe() {
  if (
    !clip.value ||
    !available.value ||
    !voice.value?.configured ||
    busy.value ||
    pending.value
  )
    return;
  const version = revision;
  busy.value = true;
  error.value = "";
  adopted.value = false;
  activeId.value = null;
  try {
    await bridge.voiceClear();
    if (version !== revision) return;
    const state = await bridge.voiceTranscribe(clip.value);
    if (version === revision) activeId.value = state.current?.id || null;
  } catch (failure) {
    if (version === revision)
      error.value = String(failure).replace(/^Error: /, "");
  } finally {
    if (version === revision) busy.value = false;
  }
}
function adopt() {
  if (
    !available.value ||
    current.value?.phase !== "review" ||
    !transcript.value.trim()
  )
    return;
  emit("adopt", transcript.value.trim());
  adopted.value = true;
}
onUnmounted(() => {
  void clear();
});
</script>

<template>
  <section class="voice-input" data-testid="voice-panel">
    <div class="voice-heading">
      <h3>普通话语音输入</h3>
      <span>中文优先 · 最长15秒</span>
    </div>
    <p>录音或导入后可先试听，再提交识别。核对转写后填入下方任务原文。</p>
    <div class="voice-controls">
      <button
        :disabled="!available || busy || recording"
        @click="record"
        data-testid="voice-record"
      >
        开始录音
      </button>
      <button
        v-if="recording"
        @click="recorder?.stop()"
        data-testid="voice-stop"
      >
        结束录音
      </button>
      <label class="voice-file"
        >导入 WAV<input
          type="file"
          accept=".wav,audio/wav"
          :disabled="!available || busy || recording"
          @change="importWav"
          data-testid="voice-file"
      /></label>
      <button
        :disabled="
          !available ||
          !clip ||
          busy ||
          recording ||
          pending ||
          !voice?.configured
        "
        @click="transcribe"
        data-testid="voice-transcribe"
      >
        提交语音识别
      </button>
      <button :disabled="!available" @click="clear" data-testid="voice-clear">
        取消并清除语音
      </button>
    </div>
    <p v-if="recording" role="status" data-testid="voice-recording">
      正在录音，最长15秒；结束后可试听，尚未上传。
    </p>
    <div v-if="clipURL" class="voice-preview" data-testid="voice-preview">
      <span>{{ clipName }}</span
      ><audio :src="clipURL" controls preload="metadata" />
      <a :href="clipURL" download="lf2s-voice.wav" data-testid="voice-save"
        >保存 WAV 用于评测</a
      >
    </div>
    <p v-if="!voice?.configured" class="voice-muted">
      {{
        voice?.configuration_error ||
        "语音接口尚未配置。可先录音或导入，提交识别需启动独立语音服务。"
      }}
    </p>
    <p v-if="pending" role="status">正在识别，可取消；结果不会自动提交任务。</p>
    <p v-if="current?.phase === 'empty'" role="status">
      未识别出文字，请检查音量后重新录音。
    </p>
    <p v-if="error || current?.error" role="alert" class="voice-error">
      {{ error || current?.error }}
    </p>
    <div v-if="current?.phase === 'review'" data-testid="voice-review">
      <label for="voice-transcript"
        >核对转写，特别注意高度、单位、时长和否定词</label
      >
      <textarea
        id="voice-transcript"
        v-model="transcript"
        maxlength="512"
        rows="3"
        data-testid="voice-transcript"
      />
      <div class="voice-controls">
        <button
          :disabled="!transcript.trim() || !available"
          @click="adopt"
          data-testid="voice-adopt"
        >
          采用核对后的文字</button
        ><small>{{
          current.elapsed_ms === null
            ? ""
            : (current.elapsed_ms / 1000).toFixed(2) + " 秒"
        }}</small>
      </div>
      <p v-if="adopted" role="status" data-testid="voice-adopted">
        已填入任务原文；请继续核对，再点击“解析任务建议”。
      </p>
    </div>
  </section>
</template>

<style scoped>
.voice-input {
  border: 1px solid var(--line, #29414e);
  border-radius: 10px;
  padding: 18px;
  margin: 20px 0;
}
.voice-heading,
.voice-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.voice-heading {
  justify-content: space-between;
}
.voice-heading h3 {
  margin: 0;
}
.voice-heading span,
.voice-muted,
small {
  color: var(--muted, #92a6b2);
}
.voice-controls button {
  padding: 9px 12px;
}
.voice-file {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 220px;
}
.voice-file input {
  max-width: 100%;
}
.voice-preview {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  margin: 15px 0;
  overflow-wrap: anywhere;
}
.voice-preview audio {
  width: min(100%, 340px);
}
.voice-input textarea {
  display: block;
  width: 100%;
  box-sizing: border-box;
  margin: 10px 0;
  background: var(--bg, #0b171f);
  color: var(--text, #dbe9ee);
  border: 1px solid var(--line, #29414e);
  border-radius: 8px;
  padding: 12px;
  font: inherit;
  resize: vertical;
}
.voice-error {
  color: #ffc38d;
}
</style>
