<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { bridge, type State } from "./bridge";
import metadata from "../package.json";
import EngineeringView from "./EngineeringView.vue";
import Workbench from "./Workbench.vue";
const state = ref<State>({
  simulation: true,
  read_only: true,
  service_ready: false,
});
const toolsOpen = ref(false);
const toolsPage = ref("bench");
const draftText = ref("");
const cameraSource = ref("camera");
const now = ref(performance.now());
const pushedAt = ref(0);
let unsubscribe: (() => void) | undefined;
let timer: ReturnType<typeof setInterval>;
const ready = computed(
  () => state.value.service_ready && now.value - pushedAt.value < 1500,
);
const connection = computed(() =>
  !ready.value
    ? "本地服务不可用"
    : state.value.replay
      ? "只读回放"
      : state.value.mode === "uart_readonly"
        ? state.value.link?.phase === "online" ? "飞控 UART1 遥测在线" : "飞控 UART1 遥测待恢复"
      : state.value.bench?.active
        ? state.value.bench.phase === "online"
          ? "相机已连接"
          : "相机连接待恢复"
        : state.value.usb_fc?.active
          ? "飞控USB只读"
          : state.value.link?.phase === "online"
            ? "本机模拟在线"
            : "设备未连接",
);
function receive(value: State) {
  state.value = value;
  pushedAt.value = performance.now();
}
function openTools(page = "bench") {
  toolsPage.value = page;
  toolsOpen.value = true;
}
onMounted(async () => {
  unsubscribe = bridge.onState(receive);
  timer = setInterval(() => (now.value = performance.now()), 200);
  try {
    receive(await bridge.state());
  } catch {
    /* backend reports startup errors */
  }
});
onUnmounted(() => {
  unsubscribe?.();
  clearInterval(timer);
});
</script>
<template>
  <div class="station-frame">
    <div class="station-preview">
      <strong>开发预览 {{ metadata.version }}</strong
      ><span>真实飞行控制尚未开放 · 数据来源在画面和任务中分别标注</span>
    </div>
    <header class="station-header">
      <div class="station-brand">
        LF<span>2S</span><span class="station-brand-divider"></span
        ><strong>视觉任务工作台</strong>
      </div>
      <div class="station-header-actions">
        <span class="station-connection" data-testid="workspace-connection">{{
          connection
        }}</span
        ><button
          v-if="toolsOpen"
          class="station-button"
          @click="toolsOpen = false"
          data-testid="workspace-return"
        >
          返回工作台</button
        ><button
          v-else
          class="station-button"
          @click="openTools()"
          data-testid="workspace-tools"
        >
          设备与调试
        </button>
      </div>
    </header>
    <Workbench
      v-if="!toolsOpen"
      :state="state"
      :ready="ready"
      active
      :initial-source="draftText"
      :initial-image-source="cameraSource"
      @draft="draftText = $event"
      @image-source="cameraSource = $event"
      @refresh="receive"
      @tools="openTools"
    />
    <EngineeringView v-if="toolsOpen" :initial-page="toolsPage" />
  </div>
</template>
<style>
.station-frame {
  min-height: 100vh;
  background: #f5f7f9;
  color: #23343f;
  font-family: "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
}
.station-preview {
  min-height: 32px;
  padding: 6px 28px;
  background: #edf3f6;
  font-size: 11px;
  color: #5a6f7c;
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.station-preview strong {
  font-weight: 650;
}
.station-header {
  height: 76px;
  padding: 0 32px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: white;
  border-bottom: 1px solid #e3e9ed;
}
.station-brand {
  display: flex;
  align-items: center;
  font-size: 23px;
  font-weight: 800;
  letter-spacing: 0;
}
.station-brand > span:first-child {
  color: #087e83;
}
.station-brand-divider {
  height: 23px;
  width: 1px;
  background: #e3e9ed;
  margin: 0 20px;
}
.station-brand strong {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0;
}
.station-header-actions {
  display: flex;
  align-items: center;
  gap: 24px;
}
.station-connection {
  font-size: 12px;
  color: #6c7d89;
}
.station-button {
  border: 1px solid #dbe3e8;
  border-radius: 8px;
  padding: 10px 15px;
  min-height: 42px;
  background: #f7f9fa;
  color: #344d5c;
}
.station-frame .app-shell {
  min-height: calc(100vh - 108px);
}
@media (max-width: 760px) {
  .station-header {
    height: 64px;
    padding: 0 16px;
  }
  .station-brand-divider {
    margin: 0 10px;
  }
  .station-brand strong {
    font-size: 12px;
  }
  .station-connection {
    display: none;
  }
  .station-preview {
    padding: 7px 16px;
    font-size: 10px;
  }
  .station-header-actions {
    gap: 10px;
  }
}
</style>
