<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { bridge, type State, type Capture } from "./bridge";
const props = defineProps<{ state: State; ready: boolean }>();
const emit = defineEmits<{ tools: [] }>();
const url = ref("");
const meta = ref<Capture | null>(null);
const at = ref(0);
const now = ref(performance.now());
const busy = ref(false);
const error = ref("");
let epoch = 0;
let alive = true;
let timer: ReturnType<typeof setInterval>;
const online = computed(
  () =>
    props.ready &&
    !props.state.replay &&
    !props.state.bench?.active &&
    !props.state.usb_fc?.active &&
    props.state.link?.phase === "online",
);
const context = computed(() =>
  [props.state.identity?.boot_id, props.state.identity?.session_id].join(":"),
);
const fresh = computed(
  () => online.value && !!url.value && now.value - at.value < 2000,
);
function clear() {
  epoch++;
  if (url.value) URL.revokeObjectURL(url.value);
  url.value = "";
  meta.value = null;
  at.value = 0;
}
async function capture() {
  if (busy.value || !online.value) return;
  const requestEpoch = epoch;
  const original = context.value;
  busy.value = true;
  try {
    const result = await bridge.image();
    if (
      !alive ||
      requestEpoch !== epoch ||
      original !== context.value ||
      !online.value
    )
      return;
    const next = URL.createObjectURL(
      new Blob([Uint8Array.from(result.bytes).buffer], { type: "image/jpeg" }),
    );
    if (url.value) URL.revokeObjectURL(url.value);
    url.value = next;
    meta.value = result;
    at.value = performance.now();
    error.value = "";
  } catch (failure) {
    if (alive && requestEpoch === epoch) {
      clear();
      error.value = String(failure).replace(/^Error: /, "");
    }
  } finally {
    busy.value = false;
  }
}
watch([online, context], () => {
  clear();
  if (online.value) void capture();
});
onMounted(() => {
  timer = setInterval(() => {
    now.value = performance.now();
    void capture();
  }, 1000);
  if (online.value) void capture();
});
onUnmounted(() => {
  alive = false;
  clearInterval(timer);
  clear();
});
</script>
<template>
  <div class="synthetic-preview">
    <div class="synthetic-stage">
      <img
        v-if="fresh"
        :src="url"
        alt="本机生成的合成测试场地图像，非真实相机"
        data-testid="synthetic-image"
      />
      <div v-else class="synthetic-empty">
        <span>本机模拟画面</span>
        <h3>{{ url ? "图像已过期" : "等待模拟图像" }}</h3>
        <p>在设备与调试中显式连接模拟设备。</p>
        <button @click="emit('tools')">打开模拟连接</button>
      </div>
      <span class="synthetic-label">模拟图像 · 非实拍</span>
    </div>
    <div class="synthetic-footer">
      <span>{{
        meta
          ? `帧 ${meta.frame_id} · ${fresh ? "显示有效" : "显示过期"}`
          : "尚无有效图像"
      }}</span
      ><span v-if="error" role="alert">{{ error }}</span
      ><span>仅用于软件验证</span>
    </div>
  </div>
</template>
<style scoped>
.synthetic-stage {
  position: relative;
  width: 100%;
  height: var(--workspace-picture-height);
  background: #edf1f4;
  display: grid;
  place-items: center;
  overflow: hidden;
}
.synthetic-stage img {
  height: 100%;
  width: 100%;
  object-fit: contain;
  background: #17242b;
}
.synthetic-empty {
  text-align: center;
  padding: 28px;
  color: #748792;
}
.synthetic-empty > span {
  font-size: 11px;
  letter-spacing: 2px;
}
.synthetic-empty h3 {
  font-size: 21px;
  color: #536c7a;
  font-weight: 500;
  margin: 18px 0 12px;
}
.synthetic-empty p {
  font-size: 13px;
}
.synthetic-empty button {
  background: #087e83;
  color: white;
  border-color: #087e83;
  margin-top: 10px;
}
.synthetic-label {
  position: absolute;
  bottom: 18px;
  left: 20px;
  color: #607987;
  background: #f7fafbe8;
  padding: 5px 9px;
  border-radius: 5px;
  font-size: 11px;
}
.synthetic-footer {
  min-height: 43px;
  padding: 12px 20px;
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #74838e;
  font-size: 11px;
}
</style>
