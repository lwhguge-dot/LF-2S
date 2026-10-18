<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { bridge, type RealCapture, type State } from "./bridge";

const props = defineProps<{
  state: State;
  serviceReady: boolean;
  embedded?: boolean;
  analysisLocked?: boolean;
}>();
const emit = defineEmits<{
  refresh: [state: State];
  capture: [frame: RealCapture];
}>();
const host = ref("");
const port = ref(80);
const busy = ref(false);
const reading = ref(false);
const auto = ref(true);
const targetFps = ref(10);
const size = ref<"qvga" | "vga" | "svga">("vga");
const colour = ref<"driver" | "office">("driver");
const displayTimes = ref<number[]>([]);
const requestToDisplay = ref<number | null>(null);
let requestStarted = 0;
let imageRequestStarted = 0;
let lastRequestAt = 0;
const message = ref("");
const imageURL = ref("");
const imageMeta = ref<RealCapture | null>(null);
const imageAt = ref(0);
const now = ref(performance.now());
const telemetryArrivedAt = ref(performance.now());
watch(() => props.state, () => { telemetryArrivedAt.value = performance.now(); });
const attitudeAvailable = computed(() => {
  const attitude = props.state.sensors?.attitude;
  const elapsed = now.value - telemetryArrivedAt.value;
  return props.serviceReady && props.state.gateway?.active && props.state.link?.phase === "online" &&
    elapsed + (props.state.link.state_age_ms ?? 1000) < 1000 &&
    attitude?.source === "uart1" && attitude.valid && elapsed + (attitude.age_ms ?? 300) < 300;
});
const attitudeText = computed(() => {
  const value = props.state.sensors?.attitude?.value;
  return attitudeAvailable.value && Array.isArray(value)
    ? value.map(v => `${(v * 180 / Math.PI).toFixed(1)}°`).join(" / ") : "未知";
});
function connectUart() { return run(() => bridge.gatewayConnect(), "UART1 握手已处理，请核对链路状态"); }
function disconnectUart() { return run(() => bridge.gatewayDisconnect(), "UART1 已断开"); }
function queryUartProfile() { return run(() => bridge.gatewayProfile(), "已查询飞控只读配置"); }
let epoch = 0;
let alive = true;
let timer: ReturnType<typeof setInterval>;
const bench = computed(() => props.state.bench);
watch(() => bench.value?.host, value => {
  if (value && !host.value && !busy.value) host.value = value;
}, {immediate:true});
const health = computed(() => bench.value?.health);
const networkText = computed(() =>
  health.value?.wifi_mode === "sta"
    ? "板卡连接电脑热点"
    : health.value?.wifi_mode === "ap"
      ? "板卡提供热点"
      : "未报告",
);
const online = computed(
  () =>
    props.serviceReady &&
    !props.state.replay &&
    bench.value?.phase === "online",
);
const fresh = computed(
  () =>
    online.value &&
    !!imageURL.value &&
    (props.analysisLocked || now.value - imageAt.value < 2000),
);
const receivedFps = computed(() => {
  const recent = displayTimes.value.filter((t) => t >= now.value - 5000);
  return fresh.value && recent.length >= 2
    ? ((recent.length - 1) * 1000) / (recent.at(-1)! - recent[0]!)
    : null;
});
const settingsAvailable = computed(
  () => online.value && Number(health.value?.firmware.split(".")[2]) >= 5,
);
const focusText = computed(() =>
  !health.value?.autofocus_initialized
    ? "不可用"
    : health.value.autofocus_error
      ? "对焦状态暂不可用"
      : health.value.autofocus_focused
      ? "传感器报告已对焦"
      : health.value.autofocus_busy
        ? "传感器报告搜索中"
        : "尚未报告完成",
);
const status = computed(() =>
  !props.serviceReady
    ? "本地服务不可用"
    : {
        disconnected: "未连接实物",
        connecting: "正在读取板卡",
        recovering: "传输暂时中断 · 正在恢复",
        online: "实物相机在线",
        error: "连接已失效",
      }[bench.value?.phase || "disconnected"],
);

function clearImage() {
  epoch++;
  if (imageURL.value) URL.revokeObjectURL(imageURL.value);
  imageURL.value = "";
  imageMeta.value = null;
  imageAt.value = 0;
  displayTimes.value = [];
  requestToDisplay.value = null;
  lastRequestAt = 0;
}
async function run(action: () => Promise<unknown>, success: string) {
  if (busy.value) return;
  busy.value = true;
  message.value = "";
  try {
    await action();
    if (!alive) return;
    emit("refresh", await bridge.state());
    message.value = success;
  } catch (error) {
    if (alive)
      message.value = String(error).split("Error: ").at(-1) || "请求未完成";
  } finally {
    busy.value = false;
  }
}
function connect() {
  clearImage();
  return run(
    () => bridge.benchConnect(host.value.trim(), port.value),
    "实物台架已连接，飞控数据仍为未知",
  );
}
function disconnect() {
  clearImage();
  return run(bridge.benchDisconnect, "已断开实物；需手动选择后续连接");
}
async function preview() {
  if (reading.value || busy.value || !online.value || props.analysisLocked) return;
  const requestEpoch = epoch;
  const boardEpoch = bench.value?.epoch;
  reading.value = true;
  requestStarted = performance.now();
  lastRequestAt = requestStarted;
  try {
    const frame = await bridge.benchPreview();
    if (
      !alive ||
      requestEpoch !== epoch ||
      !online.value ||
      props.analysisLocked ||
      frame.epoch !== boardEpoch ||
      frame.stream_id !== health.value?.camera_stream_id
    )
      return;
    const url = URL.createObjectURL(
      new Blob([Uint8Array.from(frame.bytes).buffer], { type: "image/jpeg" }),
    );
    try {
      const decoded = new Image();
      decoded.src = url;
      await decoded.decode();
    } catch {
      URL.revokeObjectURL(url);
      throw new Error("浏览器无法解码这张 JPEG");
    }
    if (!alive || requestEpoch !== epoch || !online.value || props.analysisLocked) {
      URL.revokeObjectURL(url);
      return;
    }
    if (imageURL.value) URL.revokeObjectURL(imageURL.value);
    imageURL.value = url;
    imageMeta.value = frame;
    imageRequestStarted = requestStarted;
    imageAt.value = performance.now();
    emit("capture", frame);
    message.value = "";
  } catch (error) {
    if (alive && requestEpoch === epoch) {
      clearImage();
      message.value = String(error).split("Error: ").at(-1) || "图像请求未完成";
    }
  } finally {
    reading.value = false;
  }
}
function imageLoaded() {
  if (!fresh.value || !imageMeta.value) return;
  const at = performance.now();
  displayTimes.value = [
    ...displayTimes.value.filter((t) => t >= at - 5000),
    at,
  ];
  requestToDisplay.value = Math.round(at - imageRequestStarted);
}
async function waitForPreview() {
  const end = performance.now() + 12000;
  while (reading.value && alive && performance.now() < end)
    await new Promise((resolve) => setTimeout(resolve, 25));
  if (!alive || reading.value) throw new Error("请稍后重新调整相机预览");
}
function settings() {
  clearImage();
  const selectedSize = size.value;
  const selectedColour = colour.value;
  return run(async () => {
    await waitForPreview();
    return bridge.benchSettings(selectedSize, selectedColour);
  }, "预览设置已核对，继续读取新画面");
}
function focus() {
  return run(bridge.benchFocus, "重新对焦请求已接受；请观察画面与传感器状态");
}
watch(
  [online, () => bench.value?.epoch, () => health.value?.camera_stream_id],
  () => {
    clearImage();
    if (online.value && auto.value) void preview();
  },
);
watch(
  () => props.analysisLocked,
  (locked) => {
    auto.value = !locked;
    if (!locked && online.value) void preview();
  },
);
onMounted(() => {
  timer = setInterval(() => {
    now.value = performance.now();
    if (auto.value && now.value - lastRequestAt >= 1000 / targetFps.value)
      void preview();
  }, 25);
});
watch(
  [() => health.value?.preview_width, () => health.value?.preview_colour],
  () => {
    const value = health.value;
    if (!value) return;
    size.value =
      value.preview_width === 320
        ? "qvga"
        : value.preview_width === 800
          ? "svga"
          : "vga";
    colour.value = value.preview_colour === 2 ? "office" : "driver";
  },
);
onUnmounted(() => {
  alive = false;
  clearInterval(timer);
  clearImage();
});
</script>

<template>
  <section class="bench-layout" :class="{'bench-embedded':embedded}" data-testid="real-bench">
    <article class="panel bench-camera">
      <div class="panel-heading">
        <h2>OV5640 实物预览</h2>
        <span class="chip teal">真实相机 · 仅显示</span>
      </div>
      <div class="bench-stage">
        <img
          v-if="fresh"
          :src="imageURL"
          alt="ESP32 OV5640 实际拍摄画面"
          data-testid="real-image"
          @error="clearImage"
          @load="imageLoaded"
        />
        <div v-else class="bench-placeholder" data-testid="real-image-empty">
          <strong>{{ imageURL ? "图像已过期" : "等待实物图像" }}</strong>
          <span>{{
            bench?.reason || "先确认两端 Wi-Fi 和板卡当前地址，再连接实物台架"
          }}</span>
        </div>
        <span class="image-watermark">实拍 · 未标定 · 不用于飞行控制</span>
      </div>
      <div class="bench-frame" data-testid="real-frame">
        <template v-if="imageMeta"
          >{{ imageMeta.width }} × {{ imageMeta.height }} · 帧
          {{ imageMeta.frame_id }} ·
          {{ fresh ? "近期收到" : "显示过期" }}</template
        >
        <template v-else>尚无有效图像</template>
      </div>
      <div class="image-tools">
        <label class="check"
          ><input v-model="auto" type="checkbox" />连续预览</label
        >
        <label class="preview-option"
          >预览频率
          <select v-model.number="targetFps" aria-label="预览频率">
            <option :value="10">尽快读取 · 上限 10 FPS</option>
            <option :value="5">上限 5 FPS</option>
            <option :value="1">1 FPS · 静态查看</option>
          </select>
        </label>
        <button :disabled="!online || reading || busy" @click="preview">
          读取实物图像
        </button>
        <button
          :disabled="
            !online || reading || busy || !health?.autofocus_initialized
          "
          @click="focus"
        >
          重新对焦
        </button>
      </div>
      <div class="bench-metrics" data-testid="preview-metrics">
        <span
          >实际显示
          <strong
            >{{
              receivedFps === null ? "—" : receivedFps.toFixed(1)
            }}
            FPS</strong
          ></span
        >
        <span
          >请求至显示
          <strong
            >{{ requestToDisplay === null ? "—" : requestToDisplay }} ms</strong
          ></span
        >
        <span
          >JPEG
          <strong
            >{{
              imageMeta ? (imageMeta.bytes.length / 1024).toFixed(1) : "—"
            }}
            KiB</strong
          ></span
        >
        <span
          >板端等帧
          <strong
            >{{
              imageMeta?.camera_wait_ms == null
                ? "—"
                : Math.round(imageMeta.camera_wait_ms)
            }}
            ms</strong
          ></span
        >
      </div>
      <div class="preview-settings">
        <label
          >画面尺寸
          <select
            v-model="size"
            :disabled="!settingsAvailable || busy"
            aria-label="画面尺寸"
          >
            <option value="qvga">320 × 240 · 优先流畅</option>
            <option value="vga">640 × 480 · 均衡</option>
            <option value="svga">800 × 600 · 查看细节</option>
          </select>
        </label>
        <label
          >白平衡
          <select
            v-model="colour"
            :disabled="!settingsAvailable || busy"
            aria-label="白平衡"
          >
            <option value="driver">自动</option>
            <option value="office">室内灯光</option>
          </select>
        </label>
        <button :disabled="!settingsAvailable || busy" @click="settings">
          应用画面设置
        </button>
      </div>
      <p class="bench-note">
        实际显示帧率取最近 5
        秒内的到图间隔；上限不保证达到。请求至显示包含拍摄等待、传输与解码，不等于曝光时延。静态查看可选
        1 FPS，运动识别需要更多连续画面。
      </p>
    </article>
    <div class="bench-sidebar">
      <article class="panel bench-connection">
        <div class="panel-heading">
          <h2>ESP32 单板连接</h2>
          <span class="chip">Wi-Fi</span>
        </div>
        <p class="bench-status" data-testid="bench-status">{{ status }}</p>
        <p data-testid="bench-network-help">
          测试时让 ESP32 连接电脑的 2.4 GHz 移动热点，电脑继续上网。
          若上游是手机热点，也使用 2.4 GHz。USB 继续供电。
        </p>
        <p>
          从电脑热点设备列表或板卡串口查看当前地址；热点重开后地址可能变化。
        </p>
        <label
          >板卡 IPv4 地址<input
            v-model="host"
            :disabled="busy || bench?.active"
            maxlength="15"
            placeholder="填写板卡实际 IPv4 地址"
            aria-label="ESP32 IPv4 地址"
        /></label>
        <label
          >HTTP 端口<input
            v-model.number="port"
            :disabled="busy || bench?.active"
            type="number"
            min="1"
            max="65535"
            aria-label="ESP32 HTTP 端口"
        /></label>
        <div class="connection-buttons">
          <button
            class="primary"
            :disabled="
              !serviceReady || busy || !!bench?.active || !!state.replay
            "
            @click="connect"
          >
            连接实物台架
          </button>
          <button
            :disabled="!serviceReady || busy || !bench?.active"
            @click="disconnect"
          >
            断开实物台架
          </button>
        </div>
        <p v-if="bench?.reason" class="bench-error" role="alert">
          {{ bench.reason }}
        </p>
        <p v-if="message" class="bench-note" role="status">{{ message }}</p>
      </article>
      <article class="panel" data-testid="uart1-telemetry">
        <div class="panel-heading"><h2>飞控 UART1 遥测</h2><span class="chip">真实 · 只读</span></div>
        <p>{{ state.gateway?.active ? state.link?.reason || `链路：${state.link?.phase}` : "相机连接后，可单独建立飞控遥测会话" }}</p>
        <div class="connection-buttons">
          <button :disabled="!online || busy || !!state.gateway?.active" @click="connectUart">连接 UART1 遥测</button>
          <button :disabled="!serviceReady || busy || !state.gateway?.active" @click="disconnectUart">断开 UART1</button>
          <button :disabled="!serviceReady || busy || !state.gateway?.active || state.link?.phase !== 'online'" @click="queryUartProfile">读取飞控配置</button>
        </div>
        <dl v-if="state.gateway?.active" data-testid="uart1-values">
          <dt>姿态 · 横滚 / 俯仰 / 相对偏航</dt>
          <dd>{{ attitudeText }}</dd>
          <dt>启动 / 会话</dt><dd>{{ state.identity?.boot_id || '未知' }} / {{ state.identity?.session_id || '未知' }}</dd>
          <dt>采样年龄</dt><dd>{{ attitudeAvailable ? `${state.sensors?.attitude?.age_ms} ms` : '未知' }}</dd>
        </dl>
        <p>高度、定位和电压尚未核实的项目保持未知。这里只读取数据，飞行动作关闭。</p>
      </article>
      <article class="panel bench-diagnostics">
        <div class="panel-heading">
          <h2>板卡诊断</h2>
          <span class="chip">只读</span>
        </div>
        <dl>
          <dt>固件</dt>
          <dd>{{ health?.firmware || "未知" }}</dd>
          <dt>协议</dt>
          <dd>{{ health?.protocol || "未知" }}</dd>
          <dt>Wi-Fi 连接方式</dt>
          <dd data-testid="wifi-mode">{{ networkText }}</dd>
          <dt>板卡报告地址</dt>
          <dd data-testid="wifi-ipv4">{{ health?.wifi_ipv4 || "未知" }}</dd>
          <dt>Wi-Fi 信号 / 信道</dt>
          <dd data-testid="wifi-signal">
            {{
              health?.wifi_rssi_dbm != null
                ? `${health.wifi_rssi_dbm} dBm`
                : "未知"
            }}
            / {{ health?.wifi_channel ?? "未知" }}
          </dd>
          <dt>自动对焦</dt>
          <dd data-testid="focus-state">{{ focusText }}</dd>
          <dt>AF 原始状态</dt>
          <dd>
            {{ health ? `0x${health.autofocus_status.toString(16)}` : "未知" }}
          </dd>
          <dt>传输异常 / 恢复</dt>
          <dd>
            {{ bench?.transport_failures ?? 0 }} / {{ bench?.recoveries ?? 0 }}
          </dd>
          <dt>相机忙响应</dt>
          <dd>{{ bench?.camera_busy_responses ?? 0 }}</dd>
          <dt v-if="bench?.last_error">最近失败用时</dt>
          <dd v-if="bench?.last_error">{{ bench.last_error.elapsed_ms }} ms</dd>
          <dt>图像尺寸</dt>
          <dd>
            {{
              health
                ? `${health.preview_width} × ${health.preview_height}`
                : "未知"
            }}
          </dd>
          <dt>PSRAM</dt>
          <dd>{{ health ? `${health.psram_bytes / 1048576} MiB` : "未知" }}</dd>
          <dt>当前模拟增益</dt>
          <dd>
            {{
              health?.sensor_diagnostics?.valid
                ? `${(health.sensor_diagnostics.gain_q4 / 16).toFixed(1)}×`
                : "未知"
            }}
          </dd>
          <dt>UDP 校验 / 拒绝</dt>
          <dd>
            {{
              health
                ? `${health.udp_validated} / ${health.udp_rejected}`
                : "未知"
            }}
          </dd>
          <dt>流编号</dt>
          <dd class="bench-id">{{ health?.camera_stream_id || "未知" }}</dd>
        </dl>
      </article>
      <article class="panel bench-boundary">
        <h2>飞控尚未接入</h2>
        <p>
          高度、姿态、位置、电压：未知。相机页面不提供飞控测量； 真实 UART
          和机载协议仍待联调。当前飞行动作入口关闭。
        </p>
      </article>
    </div>
  </section>
</template>

<style scoped>
.bench-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 330px;
  gap: 20px;
}
.bench-camera {
  align-self: start;
  overflow: hidden;
}
.bench-stage {
  position: relative;
  background: #060c11;
  height: clamp(220px, 45vh, 600px);
  display: grid;
  place-items: center;
}
.bench-stage img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  position: absolute;
}
.bench-placeholder {
  display: flex;
  flex-direction: column;
  text-align: center;
  gap: 12px;
  padding: 30px;
  color: #9aaebc;
}
.bench-placeholder strong {
  font-size: 20px;
  color: #dce8ee;
}
.bench-frame {
  padding: 10px 18px;
  border-top: 1px solid #23313d;
  color: #9dbecc;
}
.bench-note {
  padding: 12px 18px;
  font-size: 12px;
}
.bench-metrics,
.preview-settings {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
  padding: 12px 18px;
  border-top: 1px solid #23313d;
  color: #9dbecc;
  font-size: 12px;
}
.bench-metrics strong {
  color: #dce8ee;
  font-weight: 500;
}
.preview-settings {
  align-items: end;
}
.preview-option,
.preview-settings label {
  display: grid;
  gap: 5px;
}
select {
  background: #101c26;
  border: 1px solid #31424d;
  border-radius: 5px;
  padding: 7px 10px;
  color: #dce8ee;
}
.bench-sidebar {
  display: grid;
  gap: 20px;
  align-content: start;
}
.bench-connection > p,
.bench-boundary {
  padding: 12px 18px;
}
.bench-status {
  color: #9ae7d5;
  font-size: 16px;
}
.bench-connection label {
  padding: 6px 18px;
  display: grid;
  gap: 5px;
  color: #a9bdc9;
}
.bench-connection input {
  width: 100%;
  background: #101c26;
  border: 1px solid #31424d;
  border-radius: 5px;
  padding: 7px 10px;
  color: #dce8ee;
}
.connection-buttons {
  padding: 12px 18px;
}
.bench-error {
  color: #ffbba0;
}
dl {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr);
  padding: 0 18px 18px;
  gap: 10px;
}
dt {
  color: #92a6b5;
}
dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.bench-id {
  font-family: Consolas, monospace;
  font-size: 11px;
}
.bench-boundary h2 {
  color: #ffc2a3;
  margin-bottom: 8px;
}
@media (max-width: 1100px) {
  .bench-layout {
    grid-template-columns: 1fr;
  }
  .bench-sidebar {
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  }
}
</style>
