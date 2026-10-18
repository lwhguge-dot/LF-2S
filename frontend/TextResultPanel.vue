<script setup lang="ts">
import { computed } from "vue";
import type { TaskPresentation } from "./bridge";
const props = defineProps<{ view: TaskPresentation }>();
const proposal = computed(() => props.view.current?.result?.proposal);
const demo = computed(() =>
  [
    "cross_road",
    "navigate_to_landmark",
    "inspect_currency_at_counter",
    "observe_traffic_signal",
  ].includes(proposal.value?.intent || ""),
);
const plannedSteps = computed(() =>
  proposal.value?.intent === "cross_road"
    ? ["视觉寻找路口", "接近路边并减速", "等待稳定绿灯", "沿斑马线到对岸"]
    : proposal.value?.intent === "inspect_currency_at_counter"
      ? [
          "视觉寻找收银台",
          "取得清晰可读画面",
          "逐张清点与复核",
          "显示数量和总金额",
        ]
      : [],
);
const labels: Record<string, string> = {
  query_status: "查询状态",
  takeoff_hold_land: "起飞、保持后降落",
  find_target: "寻找红卡目标（历史任务）",
  cross_road: "沿斑马线过马路",
  navigate_to_landmark: "视觉寻找并前往地点",
  inspect_currency_at_counter: "在收银台清点人民币",
  observe_traffic_signal: "观察信号灯",
  cancel: "取消意图",
  clarify: "需要澄清",
  unsupported: "不支持的请求",
};
const targets: Record<string, string> = {
  red_card: "红卡（历史任务）",
  roadside: "路边 / 路口",
  checkout_counter: "收银台",
};
</script>
<template>
  <div class="text-result" data-testid="text-task-result">
    <div class="text-heading">
      <h3>{{ view.status }}</h3>
      <small
        v-if="
          view.current?.elapsed_ms !== null &&
          view.current?.elapsed_ms !== undefined
        "
        >{{ (view.current.elapsed_ms / 1000).toFixed(2) }} 秒 ·
        {{
          view.current.provider === "company_model"
            ? "公司模型"
            : "本地原文检查"
        }}</small
      >
    </div>
    <p v-if="view.current?.phase === 'pending'">
      可继续编辑或取消，旧请求不会自动重发。
    </p>
    <ul
      v-if="view.current?.result?.questions.length"
      data-testid="text-task-questions"
    >
      <li v-for="question in view.current.result.questions" :key="question">
        {{ question }}
      </li>
    </ul>
    <ul
      v-if="view.current?.result?.errors.length"
      data-testid="text-task-errors"
    >
      <li v-for="item in view.current.result.errors" :key="item">{{ item }}</li>
    </ul>
    <dl v-if="proposal" class="text-fields" data-testid="text-task-proposal">
      <div>
        <dt>意图</dt>
        <dd>{{ labels[proposal.intent] || proposal.intent }}</dd>
      </div>
      <div v-if="!demo">
        <dt>离地高度</dt>
        <dd>
          {{ proposal.height_m === null ? "未指定" : `${proposal.height_m} m` }}
        </dd>
      </div>
      <div v-if="!demo">
        <dt>保持时长</dt>
        <dd>
          {{ proposal.hold_s === null ? "未指定" : `${proposal.hold_s} s` }}
        </dd>
      </div>
      <div>
        <dt>目标</dt>
        <dd>{{ targets[proposal.target || ""] || "未指定" }}</dd>
      </div>
    </dl>
    <div v-if="plannedSteps.length" class="planned-steps">
      <h4>建议流程 · 未执行</h4>
      <ol>
        <li v-for="step in plannedSteps" :key="step">{{ step }}</li>
      </ol>
    </div>
    <div
      v-if="view.current?.result?.source_evidence.length"
      class="text-evidence"
    >
      <h4>原文依据</h4>
      <p
        v-for="(item, index) in view.current.result.source_evidence"
        :key="index"
      >
        <q>{{ item.quote }}</q
        ><span>
          → {{ item.value
          }}{{
            item.field === "height_m"
              ? " m"
              : item.field === "hold_s"
                ? " s"
                : ""
          }}</span
        >
      </p>
    </div>
    <p v-if="proposal?.clarification">{{ proposal.clarification }}</p>
    <p class="text-muted">
      当前仅供审阅。人工许可、实际配置、传感器有效性与机载检查仍须在执行链路逐项通过。
    </p>
  </div>
</template>
<style scoped>
.planned-steps {
  font-size: 12px;
  color: #617985;
}
.planned-steps h4 {
  font-size: 11px;
  font-weight: 500;
  margin: 12px 0;
}
.planned-steps ol {
  padding-left: 20px;
  line-height: 2.2;
  margin: 8px 0;
}
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
</style>
