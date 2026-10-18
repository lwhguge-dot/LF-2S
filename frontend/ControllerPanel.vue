<script setup lang="ts">
import { computed } from "vue";
import type { ControllerStatus } from "./bridge";

const props = defineProps<{
  report?: ControllerStatus;
  current: boolean;
  unavailableReason: string;
}>();
const value = computed(() =>
  props.current && props.report?.available ? props.report : null,
);
const modes: Record<string, string> = {
  IDLE: "空闲",
  READY: "就绪",
  AUTO: "自动",
  HOLD: "保持",
  LANDING: "降落",
  FAILSAFE_HOLD: "失效保持",
  FAILSAFE_LAND: "失效降落",
  FAULT: "故障",
  MANUAL: "人工",
};
const reasons: Record<string, string> = {
  OK: "未上报失效原因",
  BAD_FIELD: "字段错误",
  WRONG_TARGET: "目标不匹配",
  BAD_SESSION: "会话不匹配",
  BUSY: "当前不可受理",
  SCHEMA: "协议不匹配",
  STALE: "请求过期",
  DUPLICATE_CONFLICT: "重复请求冲突",
  FRESHNESS: "状态新鲜度条件未满足",
  NO_PERMIT: "缺少许可",
  NO_AUTHORITY: "缺少控制权",
  CONFIG: "配置条件未满足",
  SENSOR: "传感器数据条件未满足",
  MANUAL: "人工接管",
  LIMIT: "超出约束",
  TASK: "任务条件未满足",
  NOT_COMPLETE: "尚未完成",
  TIMEOUT: "已超时",
  UNKNOWN_RESULT: "结果未知",
  LOW_BATTERY: "低电压",
  RESET: "定位状态已重置",
  UNSUPPORTED: "尚不支持",
};
function label(
  code: number | null | undefined,
  name: string | null | undefined,
  names: Record<string, string>,
) {
  if (code == null) return "未知";
  return (name && names[name]) || `未识别（${code}）`;
}
function flag(value: boolean | null | undefined, yes: string, no: string) {
  return value == null ? "未知" : value ? yes : no;
}
const warning = computed(
  () =>
    !!value.value &&
    (["FAULT", "FAILSAFE_HOLD", "FAILSAFE_LAND"].includes(
      value.value.mode_name || "",
    ) ||
      value.value.failsafe_reason_code !== 0),
);
</script>

<template>
  <section
    class="panel controller-panel"
    :class="{ 'controller-warning': warning }"
    data-testid="controller-status"
    aria-label="飞控协议状态"
  >
    <div class="panel-heading">
      <h2>飞控协议状态</h2>
      <span class="chip">{{ value ? "当前会话上报" : "暂无当前状态" }}</span>
    </div>
    <div class="controller-grid">
      <div>
        <span>上报模式</span
        ><strong data-testid="controller-mode">{{
          label(value?.mode_code, value?.mode_name, modes)
        }}</strong>
      </div>
      <div class="controller-reason">
        <span>上报原因</span
        ><strong data-testid="controller-reason">{{
          label(
            value?.failsafe_reason_code,
            value?.failsafe_reason_name,
            reasons,
          )
        }}</strong>
      </div>
      <div>
        <span>上报预检</span
        ><strong data-testid="controller-preflight">{{
          flag(value?.preflight, "满足", "未满足")
        }}</strong>
      </div>
      <div>
        <span>人工接管锁存</span
        ><strong data-testid="controller-manual">{{
          flag(value?.manual_latched, "已锁存", "未锁存")
        }}</strong>
      </div>
      <div>
        <span>上报控制者</span
        ><strong data-testid="controller-owner">{{
          value
            ? value.owner_system || value.owner_component
              ? `${value.owner_system} / ${value.owner_component}`
              : "无"
            : "未知"
        }}</strong>
      </div>
      <div>
        <span>权限租期 / 任务</span
        ><strong data-testid="controller-lease-task">{{
          value
            ? `${value.lease_id || "无"} / ${value.active_task || "无"}`
            : "未知 / 未知"
        }}</strong>
      </div>
    </div>
    <p data-testid="controller-detail">
      {{
        value
          ? "模式和原因来自状态包；不表示动作已执行或取得实机控制资格。"
          : report?.reason || unavailableReason
      }}
    </p>
  </section>
</template>

<style scoped>
.controller-panel {
  padding: 18px 22px;
}
.controller-warning {
  border-color: #c8925d;
}
.controller-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px 20px;
  margin: 14px 0;
}
.controller-grid span {
  display: block;
  color: #92a6b5;
  font-size: 12px;
}
.controller-grid strong {
  display: block;
  overflow-wrap: anywhere;
  font-size: 14px;
  font-weight: 550;
}
.controller-warning .controller-reason strong {
  color: #f3bd80;
}
.controller-panel p {
  font-size: 12px;
}
@media (max-width: 980px) {
  .controller-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
