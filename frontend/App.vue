<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { bridge, type Capture, type Sensor, type State } from "./bridge";
import metadata from "../package.json";
import TaskPanel from "./TaskPanel.vue";
import ReplayPanel from "./ReplayPanel.vue";
import BenchPanel from "./BenchPanel.vue";

const state = ref<State>({
  simulation: true,
  read_only: true,
  service_ready: false,
});
const page = ref("overview");
const busy = ref(false);
const message = ref("");
const now = ref(performance.now());
const pushedAt = ref(performance.now());
const scenario = ref("healthy");
const filter = ref("all");
const imageURL = ref("");
const imageMeta = ref<Capture | null>(null);
const imageAt = ref(0);
const imageBusy = ref(false);
const autoImage = ref(true);
const imageMessage = ref("连接模拟设备后显示合成图像");
let unsubscribe: (() => void) | undefined;
let clock: ReturnType<typeof setInterval>;
let imageTimer: ReturnType<typeof setInterval>;
let imageEpoch = 0;

const serviceReady = computed(
  () => state.value.service_ready && now.value - pushedAt.value < 1500,
);
const phase = computed(() =>
  !serviceReady.value
    ? "service_error"
    : state.value.link?.phase || "disconnected",
);
const online = computed(
  () =>
    phase.value === "online" &&
    !state.value.replay &&
    !state.value.bench?.active,
);
const benchPage = computed(
  () => page.value === "bench" || !!state.value.bench?.active,
);
const phaseNames: Record<string, string> = {
  online: "模拟链路在线",
  disconnected: "尚未连接",
  connecting: "握手中",
  stale: "链路状态已过期",
  blocked: "链路校验失败",
  service_error: "本地服务不可用",
};
const phaseName = computed(() => phaseNames[phase.value] || "未知状态");
const linkReason = computed(() =>
  !serviceReady.value
    ? state.value.service_error || "未收到后台更新，全部数据已失效"
    : state.value.link?.reason ||
      (online.value
        ? "已校验当前来源、会话与协议指纹"
        : "请连接本机模拟设备；没有连接真实飞机"),
);
const sensors = ["height", "battery", "position", "attitude"];
const sensorNames: Record<string, string> = {
  height: "高度",
  battery: "电压",
  position: "场地位置",
  attitude: "姿态",
};
function sensor(name: string): Sensor {
  const s = state.value.sensors?.[name];
  if (!online.value)
    return {
      valid: false,
      value: null,
      age_ms: s?.age_ms ?? null,
      reason: linkReason.value,
      source: "synthetic",
      unit: s?.unit || "",
    };
  return (
    s || {
      valid: false,
      value: null,
      age_ms: null,
      reason: "尚无测量",
      source: "synthetic",
      unit: "",
    }
  );
}
function sensorText(name: string) {
  const s = sensor(name);
  if (!s.valid || s.value === null) return "未知";
  if (Array.isArray(s.value))
    return s.value
      .map((n) => n.toFixed(name === "attitude" ? 3 : 2))
      .join(" / ");
  return s.value.toFixed(name === "height" ? 3 : 2);
}
const validCount = computed(
  () => sensors.filter((n) => sensor(n).valid).length,
);
const imageFresh = computed(
  () => online.value && !!imageURL.value && now.value - imageAt.value < 2000,
);
const events = computed(() =>
  [...(state.value.events || [])]
    .reverse()
    .filter(
      (e) =>
        filter.value === "all" ||
        (filter.value === "protocol"
          ? ["tx", "rx"].includes(e.kind)
          : e.kind === "warning"),
    ),
);
const latestOperation = computed(() => state.value.operations?.at(-1));
watch(page, () => {
  message.value = "";
});
const positionStyle = computed(() => {
  const s = sensor("position");
  const values = s.valid && Array.isArray(s.value) ? s.value : null;
  return values
    ? { left: `${50 - values[1] * 15}%`, top: `${50 - values[0] * 15}%` }
    : {};
});
function receive(value: State) {
  state.value = value;
  pushedAt.value = performance.now();
  if (value.scenario) scenario.value = value.scenario;
}
async function run(action: () => Promise<unknown>, success: string) {
  if (busy.value) return;
  busy.value = true;
  message.value = "";
  try {
    const result = await action();
    const op = result as { status?: string; reason?: string } | undefined;
    message.value =
      op?.status === "unknown" || op?.status === "rejected"
        ? `请求未成功：${op.reason}`
        : success;
    receive(await bridge.state());
  } catch (error) {
    message.value = String(error).replace(/^Error: /, "");
  } finally {
    busy.value = false;
  }
}
function restartService() {
  return run(bridge.restart, "本地服务已重启；请重新连接");
}
function connectDevice() {
  return run(bridge.connect, "HELLO 已应答；模拟会话已建立");
}
function disconnectDevice() {
  return run(bridge.disconnect, "连接已断开；旧会话不重放");
}
function queryProfile() {
  return run(bridge.profile, "配置查询已应答；模拟配置仍未签认");
}
function changeScenario() {
  return run(() => bridge.scenario(scenario.value), "模拟场景已切换");
}
function openLogs() {
  return run(bridge.openLogs, "日志目录已打开");
}
async function capture() {
  if (imageBusy.value || !online.value) return;
  const epoch = imageEpoch;
  imageBusy.value = true;
  try {
    const capture = await bridge.image();
    if (epoch !== imageEpoch || !online.value) return;
    const url = URL.createObjectURL(
      new Blob([Uint8Array.from(capture.bytes).buffer], { type: "image/jpeg" }),
    );
    if (imageURL.value) URL.revokeObjectURL(imageURL.value);
    imageURL.value = url;
    imageMeta.value = capture;
    imageAt.value = performance.now();
    imageMessage.value = "合成 JPEG · 仅用于显示，实机新鲜度未签认";
  } catch (error) {
    imageMessage.value = String(error);
  } finally {
    imageBusy.value = false;
  }
}
watch(online, (value) => {
  if (!value) {
    imageEpoch++;
    imageAt.value = 0;
  } else if (autoImage.value) void capture();
});
watch(
  () => state.value.bench?.active,
  (active) => {
    if (!active) return;
    imageEpoch++;
    if (imageURL.value) URL.revokeObjectURL(imageURL.value);
    imageURL.value = "";
    imageMeta.value = null;
    imageAt.value = 0;
  },
);
function timeText(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}
onMounted(async () => {
  unsubscribe = bridge.onState(receive);
  clock = setInterval(() => {
    now.value = performance.now();
  }, 200);
  imageTimer = setInterval(() => {
    if (autoImage.value) void capture();
  }, 1000);
  try {
    receive(await bridge.state());
  } catch (error) {
    message.value = String(error);
  }
});
onUnmounted(() => {
  unsubscribe?.();
  clearInterval(clock);
  clearInterval(imageTimer);
  if (imageURL.value) URL.revokeObjectURL(imageURL.value);
});
</script>

<template>
  <div class="shell">
    <aside class="rail">
      <div class="brand">
        <span class="brand-mark">✣</span>
        <div>LF-2S<small>无人机上位机</small></div>
      </div>
      <div class="rail-label">工作台</div>
      <nav aria-label="主导航">
        <button
          v-for="item in [
            ['overview', '◫', '监控总览'],
            ['bench', '▣', 'ESP32 实物台架'],
            ['protocol', '⌘', '协议详情'],
            ['logs', '≡', '事件日志'],
            ['tasks', '▷', '模拟任务'],
            ['replay', '◷', '只读回放'],
            ['roadmap', '↗', '后续路线'],
          ]"
          :key="item[0]"
          :class="{ selected: page === item[0] }"
          @click="page = item[0]"
          :aria-current="page === item[0] ? 'page' : undefined"
        >
          <span class="nav-icon">{{ item[1] }}</span
          >{{ item[2] }}
        </button>
      </nav>
      <div class="rail-bottom">
        <span class="edition">v{{ metadata.version }}</span>
        <p>自用与教学<br />Windows 桌面版</p>
        <div class="readonly-dot">
          {{ benchPage ? "实物相机台架" : "本机模拟" }}
        </div>
      </div>
    </aside>
    <main>
      <header class="topbar">
        <div>
          <span class="eyebrow">LF-2S / 本机工作台</span>
          <h1>
            {{
              {
                overview: "监控总览",
                bench: "ESP32 实物台架",
                protocol: "协议详情",
                logs: "事件日志",
                tasks: "模拟任务",
                replay: "只读回放",
                roadmap: "后续路线",
              }[page]
            }}
          </h1>
        </div>
        <div class="top-status">
          <span
            :class="[
              'status-dot',
              {
                good:
                  online || (serviceReady && state.bench?.phase === 'online'),
              },
            ]"
          />{{
            state.bench?.active
              ? state.bench.phase === "online" && serviceReady
                ? "实物相机在线 · 飞控未接入"
                : "实物连接未就绪"
              : phaseName
          }}<span class="version">MAVLink 2</span>
        </div>
      </header>
      <div
        v-if="benchPage"
        class="simulation-banner"
        data-testid="bench-banner"
      >
        <span class="tag">实物台架</span><strong>ESP32 相机与板卡诊断</strong
        ><span>真实图像仅供显示 · STM32 未接入 · 飞行动作关闭</span>
      </div>
      <div v-else class="simulation-banner" data-testid="simulation-banner">
        <span class="tag">模拟模式</span
        ><strong>{{ state.replay ? "只读历史回放" : "模拟任务预览版" }}</strong
        ><span>设备、动作与图像均由本机模型生成 · 未连接真实飞机</span>
      </div>
      <div v-if="message" class="notice" role="status" data-testid="notice">
        {{ message
        }}<button @click="message = ''" aria-label="关闭提示">×</button>
      </div>
      <div
        v-if="!serviceReady"
        class="service-warning"
        role="alert"
        data-testid="service-error"
      >
        {{ linkReason }}
        <button :disabled="busy" @click="restartService">重启本地服务</button>
      </div>

      <BenchPanel
        v-if="page === 'bench'"
        :state="state"
        :service-ready="serviceReady"
        @refresh="receive"
      />

      <div v-show="page === 'overview'" class="overview-grid">
        <section class="visual-area">
          <article class="panel image-panel">
            <div class="panel-heading">
              <div>
                <span class="section-number">01</span>
                <h2>图像监视</h2>
              </div>
              <span class="chip teal">合成 JPEG</span>
            </div>
            <div
              class="image-stage"
              :class="{ expired: imageURL && !imageFresh }"
            >
              <img
                v-if="imageURL"
                :src="imageURL"
                alt="本机生成的合成测试场地图像，非真实相机"
                data-testid="synthetic-image"
              />
              <div v-else class="image-placeholder">
                <div class="crosshair">＋</div>
                <strong>等待合成图像</strong
                ><span>连接后显示本机生成的测试场地</span>
              </div>
              <span class="image-watermark">模拟图像 · 非实拍</span>
              <span
                v-if="imageURL && !imageFresh"
                class="image-expired"
                data-testid="image-expired"
                >图像已过期 · 不可用于控制</span
              >
              <div v-if="imageMeta" class="frame-strip">
                <span>帧 {{ String(imageMeta.frame_id).padStart(6, "0") }}</span
                ><span>960 × 540</span
                ><span>{{ imageFresh ? "显示有效" : "显示过期" }}</span>
              </div>
            </div>
            <div class="image-tools">
              <label class="check"
                ><input v-model="autoImage" type="checkbox" />自动刷新</label
              ><span>{{ imageMessage }}</span>
              <button :disabled="!online || imageBusy" @click="capture">
                {{ imageBusy ? "读取中…" : "读取合成图像" }}
              </button>
            </div>
          </article>
          <div class="metrics">
            <article
              v-for="name in sensors"
              :key="name"
              class="panel metric"
              :data-testid="'sensor-' + name"
            >
              <div class="metric-label">
                {{ sensorNames[name]
                }}<span
                  :class="['tiny-status', { good: sensor(name).valid }]"
                  >{{ sensor(name).valid ? "合成有效" : "未知 / 无效" }}</span
                >
              </div>
              <div
                class="metric-value"
                :class="{ compact: name === 'position' || name === 'attitude' }"
              >
                {{ sensorText(name)
                }}<small v-if="sensor(name).valid">{{
                  sensor(name).unit
                }}</small>
              </div>
              <div class="metric-reason">
                {{
                  sensor(name).valid
                    ? `样本年龄 ${sensor(name).age_ms ?? "未知"} ms`
                    : sensor(name).reason
                }}
              </div>
            </article>
          </div>
          <article class="panel field-panel">
            <div class="field-description">
              <span class="eyebrow">LOCAL FLU · 合成坐标</span>
              <h2>场地局部位置</h2>
              <p>X 前 · Y 左 · Z 上，单位 m。<br />仅展示有效合成样本。</p>
              <span class="chip">{{ validCount }} / 4 项有效</span>
            </div>
            <div class="local-field" aria-label="合成局部坐标位置">
              <span class="axis-x">X 前 ↑</span
              ><span class="axis-y">← Y 左</span><i class="origin" />
              <span
                v-if="sensor('position').valid"
                class="vehicle-point"
                :style="positionStyle"
                title="合成位置"
                >✣</span
              ><span v-else class="field-unknown">位置未知</span>
            </div>
          </article>
        </section>
        <aside class="connection-area">
          <section class="panel connection-panel">
            <div class="panel-heading">
              <div>
                <span class="section-number">02</span>
                <h2>连接与场景</h2>
              </div>
              <span class="chip">本机</span>
            </div>
            <div class="connection-state" data-testid="link">
              <span :class="['status-dot', { good: online }]" /><strong>{{
                phaseName
              }}</strong>
            </div>
            <p class="explanation">{{ linkReason }}</p>
            <div class="connection-buttons">
              <button
                class="primary"
                :disabled="
                  busy ||
                  !serviceReady ||
                  !!state.bench?.active ||
                  !!state.replay ||
                  phase !== 'disconnected'
                "
                @click="connectDevice"
              >
                连接模拟设备
              </button>
              <button
                :disabled="busy || !serviceReady || phase === 'disconnected'"
                @click="disconnectDevice"
              >
                断开
              </button>
            </div>
            <label class="input-label" for="scenario">模拟场景</label
            ><select
              id="scenario"
              v-model="scenario"
              :disabled="
                busy || !serviceReady || !!state.replay || !!state.bench?.active
              "
              @change="changeScenario"
            >
              <option
                v-for="(label, name) in state.scenarios || {
                  healthy: '合成测量正常',
                }"
                :key="name"
                :value="name"
              >
                {{ label }}
              </option>
            </select>
            <p class="fine-print">
              场景只影响本机模型。真实设备配置将在 S3 独立登记。
            </p>
            <dl class="compact-list">
              <dt>后台服务</dt>
              <dd>{{ serviceReady ? "已就绪" : "不可用" }}</dd>
              <dt>状态年龄</dt>
              <dd>{{ state.link?.state_age_ms ?? "未知" }} ms</dd>
              <dt>数据源</dt>
              <dd>合成生产者</dd>
              <dt>接收 / 拒绝</dt>
              <dd>
                {{ state.counters?.received ?? 0 }} /
                {{ state.counters?.rejected ?? 0 }}
              </dd>
            </dl>
          </section>
          <section class="panel qualification">
            <div class="panel-heading">
              <div>
                <span class="section-number">03</span>
                <h2>资格与边界</h2>
              </div>
            </div>
            <div class="qualification-row">
              <span>传感器</span
              ><strong :class="{ tealtext: validCount === 4 }"
                >{{ validCount }} / 4 合成有效</strong
              >
            </div>
            <div class="qualification-row">
              <span>生产配置</span
              ><strong class="ambertext">{{
                state.qualification?.configuration === "unsigned"
                  ? "未签认"
                  : "尚未读取"
              }}</strong>
            </div>
            <div class="qualification-row">
              <span>相机新鲜度</span><strong class="ambertext">未签认</strong>
            </div>
            <div class="qualification-row">
              <span>允许实机控制</span><strong>否 · 实机未开放</strong>
            </div>
            <div class="boundary-note">
              链路在线与传感器有效，均不代表允许飞行。实机故障保护仍待固件和实物验证。
            </div>
            <button
              class="wide"
              :disabled="!online || busy || state.task?.running"
              @click="queryProfile"
            >
              读取协议配置
            </button>
            <p
              v-if="latestOperation"
              class="operation-text"
              data-testid="operation"
            >
              {{ latestOperation.name }} #{{ latestOperation.request_id }} ·
              {{ latestOperation.protocol_phase || latestOperation.status }}
            </p>
          </section>
          <section class="panel session-summary">
            <span class="eyebrow">当前会话 · u64 字符串</span
            ><code data-testid="u64">{{
              state.identity?.session_id || "尚未建立"
            }}</code
            ><button class="text-button" @click="page = 'protocol'">
              查看协议与请求详情 →
            </button>
          </section>
        </aside>
      </div>

      <section v-show="page === 'protocol'" class="panel detail-panel">
        <div class="panel-heading">
          <div>
            <span class="section-number">01</span>
            <h2>协议与连接身份</h2>
          </div>
          <button
            :disabled="!online || busy || state.task?.running"
            @click="queryProfile"
          >
            查询配置
          </button>
        </div>
        <p class="explanation">
          复用 2.0.0-draft1；业务编号保持原定义。64 位字段经 JSON
          使用十进制字符串。
        </p>
        <dl class="identity-list">
          <dt>通信路线</dt>
          <dd>{{ state.link?.transport || "本地 UDP 模型" }}</dd>
          <dt>本机 UDP 端口（PC / ESP / FC）</dt>
          <dd>{{ state.link?.ports?.join(" / ") || "未知" }}</dd>
          <dt>期望协议指纹</dt>
          <dd>
            {{ state.identity?.expected_dialect_hash || "5889088805303263" }}
          </dd>
          <dt>收到的协议指纹</dt>
          <dd>{{ state.identity?.observed_dialect_hash || "未知" }}</dd>
          <dt>启动代数 boot_id</dt>
          <dd>{{ state.identity?.boot_id || "未知" }}</dd>
          <dt>会话 session_id</dt>
          <dd data-testid="protocol-session">
            {{ state.identity?.session_id || "未知" }}
          </dd>
          <dt>随机编号 client_nonce</dt>
          <dd>{{ state.identity?.client_nonce || "未知" }}</dd>
        </dl>
        <h3>
          模拟配置原始字段
          <span class="chip amber">{{
            state.task_fixture
              ? "仅模型测试资格 · 非生产配置"
              : "config_valid=0 / validated_mask=0"
          }}</span>
        </h3>
        <pre data-testid="profile-json">{{
          state.profile
            ? JSON.stringify(state.profile, null, 2)
            : "尚未查询当前会话配置"
        }}</pre>
        <h3>应用操作与协议请求</h3>
        <pre>{{ JSON.stringify(state.operations || [], null, 2) }}</pre>
      </section>

      <section v-show="page === 'logs'" class="panel detail-panel">
        <div class="panel-heading">
          <div>
            <span class="section-number">01</span>
            <h2>事件与协议日志</h2>
          </div>
          <button @click="openLogs">打开日志目录</button>
        </div>
        <div class="log-toolbar">
          <label for="log-filter">筛选</label
          ><select id="log-filter" v-model="filter">
            <option value="all">全部事件</option>
            <option value="protocol">协议请求与应答</option>
            <option value="warning">异常与拒绝</option></select
          ><span>界面保留最近 240 条 · 磁盘按大小轮转</span>
        </div>
        <div class="event-list" data-testid="events">
          <details v-for="event in events" :key="event.id" class="event-row">
            <summary>
              <time>{{ timeText(event.time) }}</time
              ><span :class="['event-kind', event.kind]">{{
                event.kind.toUpperCase()
              }}</span
              ><span>{{ event.text }}</span
              ><code v-if="event.operation_id">{{ event.operation_id }}</code>
            </summary>
            <pre>{{ JSON.stringify(event, null, 2) }}</pre>
          </details>
          <p v-if="!events.length">暂无匹配事件</p>
        </div>
      </section>

      <TaskPanel
        v-if="page === 'tasks'"
        :state="state"
        :ready="serviceReady && !state.bench?.active"
      />
      <ReplayPanel
        v-if="page === 'replay'"
        :state="state"
        :ready="serviceReady"
      />

      <section v-show="page === 'roadmap'" class="panel detail-panel roadmap">
        <span class="eyebrow">分阶段交付 · 完整目标保留</span>
        <h2>从模拟监控到真实主线</h2>
        <p>
          当前已交付模拟监控、模拟任务闭环与只读回放。S3–S5
          尚未完成，不代表实机或飞行验收。
        </p>
        <article
          v-for="item in [
            [
              'S2',
              '模拟任务闭环与回放',
              '本机示例：现场许可 → ACQUIRE → BEGIN → 模拟起飞/保持/降落；结果查询、接管锁存与只读回放。',
            ],
            [
              'S3',
              '真实 ESP32 / STM32 主线',
              '固定已有硬件；保留商家内环与原遥控接管。先实物只读，再按条件拆桨与飞行验证。',
            ],
            [
              'S4',
              '独立图像、中文语音与视觉',
              '真实 ImageGate 新鲜度、中文优先选型、候选任务确认、独立有界推理进程。',
            ],
            [
              'S5',
              'Mind+ 补充线',
              'Mind+ → 有线手柄 → NRF → 飞控，接同一权限与任务入口。QGroundControl 兼容后续评估。',
            ],
          ]"
          :key="item[0]"
        >
          <span>{{ item[0] }}</span>
          <div>
            <h3>{{ item[1] }}</h3>
            <p>{{ item[2] }}</p>
          </div>
          <span class="chip">{{
            item[0] === "S2" ? "本机模拟已实施" : "待实施"
          }}</span>
        </article>
      </section>
      <footer>
        <span>ESP32 台架 / 模拟任务 / 只读回放</span
        ><span>飞控、视觉控制与飞行验收待后续联调</span>
      </footer>
    </main>
  </div>
</template>
