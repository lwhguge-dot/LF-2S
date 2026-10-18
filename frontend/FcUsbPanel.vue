<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { bridge, type State, type UsbMeasurement } from "./bridge";

const props = defineProps<{ state: State; serviceReady: boolean }>();
const emit = defineEmits<{ refresh: [state: State] }>();
const busy = ref(false);
const message = ref("");
const now = ref(performance.now());
const receivedAt = ref(performance.now());
const timer = setInterval(() => (now.value = performance.now()), 100);
onUnmounted(() => clearInterval(timer));
watch(
  () => props.state,
  () => (receivedAt.value = performance.now()),
);
const fc = computed(() => props.state.usb_fc);
const status = computed(() =>
  !props.serviceReady
    ? "本地服务不可用"
    : {
        disconnected: "未连接",
        connecting: "正在读取设备",
        online: "USB 数据在线",
        stale: "接收已过期",
        error: "连接异常",
      }[fc.value?.phase || "disconnected"],
);
function current(field?: UsbMeasurement) {
  return (
    props.serviceReady &&
    !props.state.replay &&
    fc.value?.active &&
    fc.value.phase === "online" &&
    field?.available &&
    field.received_age_ms !== null &&
    field.received_age_ms + now.value - receivedAt.value <= 1000
  );
}
function value(field: UsbMeasurement | undefined, key: string, decimals = 0) {
  const number = field?.value?.[key];
  return current(field) && typeof number === "number"
    ? number.toFixed(decimals)
    : "未知";
}
const channels = computed(() => {
  const raw = fc.value?.rc_raw.value?.rc_channels_raw;
  return current(fc.value?.rc_raw) && Array.isArray(raw) ? raw : null;
});
const tail = computed(() => {
  const raw = fc.value?.status_raw.value;
  return current(fc.value?.status_raw) && raw
    ? [raw.status_byte10_raw, raw.status_byte11_raw]
        .map((n) => Number(n).toString(16).padStart(2, "0").toUpperCase())
        .join(" ")
    : "未知";
});
const history = computed(() =>
  current(fc.value?.attitude) ? fc.value?.history || [] : [],
);
const range = computed(() => {
  const numbers = history.value.flatMap((s) => [s.roll_deg, s.pitch_deg]);
  return numbers.length
    ? [
        Math.floor(Math.min(...numbers) / 5) * 5 - 5,
        Math.ceil(Math.max(...numbers) / 5) * 5 + 5,
      ]
    : [-10, 10];
});
function points(key: "roll_deg" | "pitch_deg") {
  const samples = history.value;
  if (samples.length < 2) return "";
  const start = samples[0].elapsed_s;
  const duration = Math.max(1, samples.at(-1)!.elapsed_s - start);
  return samples
    .map(
      (s) =>
        `${((s.elapsed_s - start) / duration) * 700 + 10},${
          165 -
          ((s[key] - range.value[0]) / (range.value[1] - range.value[0])) * 150
        }`,
    )
    .join(" ");
}
async function run(action: () => Promise<unknown>, success: string) {
  if (busy.value) return;
  busy.value = true;
  message.value = "";
  try {
    await action();
    emit("refresh", await bridge.state());
    message.value = success;
  } catch (error) {
    message.value = String(error).replace(/^Error: /, "");
    try {
      emit("refresh", await bridge.state());
    } catch {
      // Keep the original request error if the local worker also became unavailable.
    }
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="fc-usb" data-testid="fc-usb-panel">
    <section class="panel usb-toolbar">
      <div>
        <span class="chip teal">{{ status }}</span>
        <h2>原厂飞控 · USB 只读遥测</h2>
        <p>读取姿态、IMU 和遥控上传值。此连接没有飞行控制出口。</p>
      </div>
      <div class="usb-actions">
        <button
          data-testid="fc-usb-connect"
          :disabled="busy || !serviceReady || fc?.active || !!state.replay"
          @click="run(bridge.fcUsbConnect, 'USB 读取已打开；当前接收状态见上方')"
        >
          连接飞控 USB
        </button>
        <button
          data-testid="fc-usb-disconnect"
          :disabled="busy || !fc?.active"
          @click="run(bridge.fcUsbDisconnect, 'USB 已断开；需要手动重新连接')"
        >
          断开
        </button>
        <button @click="bridge.openLogs">打开日志目录</button>
      </div>
    </section>
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <p
      v-if="fc?.reason"
      class="notice"
      role="alert"
      data-testid="fc-usb-reason"
    >
      {{ fc.reason }}
    </p>
    <p class="usb-explanation">
      角度采用厂商上传轴向；主机接收年龄可检查，传感器采样时刻和轴向标定尚未核实。下列值仅供台架观察。
    </p>
    <div class="usb-angle-grid">
      <article
        v-for="[key, label] in [
          ['roll_deg', '横滚'],
          ['pitch_deg', '俯仰'],
          ['yaw_deg', '偏航'],
        ]"
        :key="key"
        class="panel usb-angle"
        :data-testid="`fc-usb-${key}`"
      >
        <span>{{ label }} · 厂商角度</span>
        <strong
          >{{ value(fc?.attitude, key, 2)
          }}<small v-if="current(fc?.attitude)">°</small></strong
        >
      </article>
    </div>
    <section class="panel usb-chart">
      <div class="usb-heading">
        <h2>最近姿态变化</h2>
        <span>蓝：横滚　红：俯仰　·　主机接收时间</span>
      </div>
      <svg
        v-if="history.length > 1"
        viewBox="0 0 720 180"
        role="img"
        aria-label="最近上传横滚与俯仰角曲线"
        data-testid="fc-usb-chart"
      >
        <line x1="10" y1="165" x2="710" y2="165" stroke="#d5dce5" />
        <polyline
          :points="points('roll_deg')"
          fill="none"
          stroke="#2563eb"
          stroke-width="2"
        />
        <polyline
          :points="points('pitch_deg')"
          fill="none"
          stroke="#dc2626"
          stroke-width="2"
        />
      </svg>
      <p v-else class="usb-empty">等待当前连接的姿态数据</p>
      <div v-if="history.length" class="usb-heading usb-subtle">
        <span>角度范围 {{ range[0] }}～{{ range[1] }}°</span>
        <span
          >{{ history[0].elapsed_s.toFixed(1) }}～{{
            history.at(-1)!.elapsed_s.toFixed(1)
          }}
          秒</span
        >
      </div>
    </section>
    <div class="usb-columns">
      <section class="panel usb-table-panel">
        <h2>IMU 上传原始值</h2>
        <table>
          <thead>
            <tr>
              <th>轴</th>
              <th>加速度计计数</th>
              <th>陀螺仪计数</th>
              <th>磁力字段原值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="axis in ['x', 'y', 'z']" :key="axis">
              <th>{{ axis.toUpperCase() }}</th>
              <td>{{ value(fc?.imu_raw, `acc_${axis}_raw`) }}</td>
              <td>{{ value(fc?.imu_raw, `gyro_${axis}_raw`) }}</td>
              <td>{{ value(fc?.imu_raw, `mag_${axis}_raw`) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="usb-subtle">
          磁力字段在供货源码中为占位；显示零不代表测量有效。原始计数尚未换算为 g
          或 °/s。
        </p>
      </section>
      <section class="panel usb-table-panel">
        <h2>遥控上传通道</h2>
        <div class="usb-channels" data-testid="fc-usb-channels">
          <div v-for="index in 10" :key="index">
            <span>通道 {{ index }}</span
            ><strong>{{ channels?.[index - 1] ?? "未知" }}</strong>
          </div>
        </div>
        <p class="usb-subtle">
          摇杆映射需拆桨逐项确认。上传值更新不能证明无线接收新鲜度、人工接管或失联保护已通过。
        </p>
      </section>
    </div>
    <section class="panel usb-table-panel">
      <h2>采集与设备记录</h2>
      <div class="usb-diagnostics">
        <div>
          <span>设备</span><strong>{{ fc?.device?.product || "未知" }}</strong>
        </div>
        <div>
          <span>完整帧</span
          ><strong data-testid="fc-usb-frames">{{
            fc?.counters.frames ?? 0
          }}</strong>
        </div>
        <div>
          <span>校验失败</span
          ><strong>{{ fc?.counters.checksum_failures ?? 0 }}</strong>
        </div>
        <div>
          <span>报告格式错误</span
          ><strong>{{ fc?.counters.report_errors ?? 0 }}</strong>
        </div>
        <div>
          <span>读取错误</span
          ><strong>{{ fc?.counters.read_errors ?? 0 }}</strong>
        </div>
        <div>
          <span>状态尾字节原值</span><strong>{{ tail }}</strong>
        </div>
      </div>
      <p class="usb-subtle">
        电池电压、高度、位置、应用固件版本及 MCU 型号：未知。没有建立 MAVLink
        会话，状态尾字节暂不解释为解锁状态。
      </p>
      <p class="usb-subtle">
        原始 HID 报告保存至日志目录 fc-usb.jsonl，单份最多 8 MiB、保留 3
        份备份；不等同于完整飞行记录。日志丢弃：{{ fc?.raw_log.dropped ?? 0 }}。
      </p>
      <p v-if="fc?.raw_log.error" role="alert" class="notice">
        原始日志写入异常：{{ fc.raw_log.error }}
      </p>
    </section>
  </div>
</template>

<style scoped>
.fc-usb {
  display: grid;
  gap: 18px;
}
.usb-toolbar,
.usb-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
}
.usb-toolbar,
.usb-table-panel,
.usb-chart {
  padding: 24px;
}
.usb-toolbar h2 {
  margin: 12px 0 8px;
}
.usb-toolbar p,
.usb-explanation {
  margin: 0;
  color: #657383;
  line-height: 1.65;
}
.usb-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.usb-angle-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 18px;
}
.usb-angle {
  padding: 24px;
  display: grid;
  gap: 12px;
}
.usb-angle > span,
.usb-subtle,
.usb-heading > span {
  color: #657383;
  font-size: 13px;
}
.usb-angle strong {
  font-size: 34px;
  font-variant-numeric: tabular-nums;
}
.usb-angle small {
  font-size: 18px;
  padding-left: 8px;
}
.usb-chart svg {
  width: 100%;
  max-height: 220px;
  margin-top: 12px;
}
.usb-empty {
  min-height: 140px;
  display: grid;
  place-items: center;
  color: #657383;
}
.usb-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.usb-table-panel h2,
.usb-chart h2 {
  margin: 0 0 16px;
  font-size: 17px;
}
.usb-table-panel table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  text-align: left;
}
th,
td {
  padding: 12px 6px;
  border-bottom: 1px solid #e7ebf0;
  font-variant-numeric: tabular-nums;
}
.usb-channels {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 15px;
}
.usb-channels div,
.usb-diagnostics div {
  display: grid;
  gap: 8px;
}
.usb-channels span,
.usb-diagnostics span {
  font-size: 12px;
  color: #657383;
}
.usb-diagnostics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}
.usb-subtle {
  line-height: 1.65;
}
@media (max-width: 1100px) {
  .usb-columns {
    grid-template-columns: 1fr;
  }
  .usb-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
