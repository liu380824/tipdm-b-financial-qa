<script setup lang="ts">
import { computed } from 'vue'
import type { CalcOperand, CalcTrace } from '../types'

const props = defineProps<{
  trace: CalcTrace | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'openEvidence', operand: CalcOperand): void
}>()

const hasTrace = computed(() => !!props.trace)
const steps = computed(() => props.trace?.steps ?? [])
const traceTag = computed(() => {
  const type = props.trace?.traceType
  if (type === 'ratio') return '比率计算'
  if (type === 'growth') return '增速计算'
  if (type === 'derived') return '派生指标'
  if (type === 'formula') return '公式推导'
  return '结果追溯'
})

const openOperandEvidence = (operand: CalcOperand) => {
  if (!operand.evidenceIds?.length && !operand.evidences?.length) return
  emit('openEvidence', operand)
}
</script>

<template>
  <div class="ctp-overlay" @click.self="emit('close')">
    <div class="ctp-panel">
      <div class="ctp-header">
        <div class="ctp-header-main">
          <div class="ctp-badge">{{ traceTag }}</div>
          <div class="ctp-title">{{ trace?.title || '计算溯源' }}</div>
          <div v-if="trace?.reportPeriod" class="ctp-sub">{{ trace.reportPeriod }}</div>
        </div>
        <button class="ctp-close" @click="emit('close')">✕</button>
      </div>

      <div v-if="hasTrace" class="ctp-body">
        <div class="ctp-result-card">
          <div class="ctp-label">最终结果</div>
          <div class="ctp-result">{{ trace?.finalValue }}</div>
          <div v-if="trace?.summary" class="ctp-summary">{{ trace.summary }}</div>
        </div>

        <div v-if="trace?.formula" class="ctp-formula-card">
          <div class="ctp-section-title">公式</div>
          <div class="ctp-formula">{{ trace.formula }}</div>
        </div>

        <div class="ctp-steps">
          <div v-for="step in steps" :key="step.id" class="ctp-step-card">
            <div class="ctp-step-head">
              <div class="ctp-step-title">{{ step.title }}</div>
              <div class="ctp-step-result">{{ step.result }}</div>
            </div>
            <div class="ctp-step-expression">{{ step.expression }}</div>
            <div v-if="step.operands?.length" class="ctp-operands">
              <button
                v-for="operand in step.operands"
                :key="`${step.id}-${operand.label}-${operand.reportPeriod || 'na'}`"
                class="ctp-operand"
                :class="{ 'is-clickable': !!(operand.evidenceIds?.length || operand.evidences?.length) }"
                @click="openOperandEvidence(operand)"
              >
                <div class="ctp-operand-top">
                  <span class="ctp-operand-label">{{ operand.label }}</span>
                  <span v-if="operand.reportPeriod" class="ctp-operand-period">{{ operand.reportPeriod }}</span>
                </div>
                <div class="ctp-operand-value">{{ operand.displayValue }}</div>
                <div class="ctp-operand-foot">
                  <span v-if="operand.evidenceIds?.length || operand.evidences?.length">查看原文证据</span>
                  <span v-else>暂无原文证据</span>
                </div>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ctp-overlay {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(15, 23, 42, 0.4);
  display: flex;
  justify-content: flex-end;
}

.ctp-panel {
  width: min(520px, 100vw);
  height: 100vh;
  background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
  box-shadow: -12px 0 36px rgba(15, 23, 42, 0.16);
  display: flex;
  flex-direction: column;
  animation: ctpSlideIn 0.22s ease;
}

@keyframes ctpSlideIn {
  from {
    transform: translateX(48px);
    opacity: 0;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}

.ctp-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 22px 18px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.18);
  background: rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(12px);
}

.ctp-header-main {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.ctp-badge {
  align-self: flex-start;
  padding: 3px 10px;
  border-radius: 999px;
  background: rgba(79, 70, 229, 0.1);
  color: #4338ca;
  font-size: 11px;
  font-weight: 700;
}

.ctp-title {
  font-size: 18px;
  line-height: 1.35;
  font-weight: 800;
  color: #0f172a;
}

.ctp-sub {
  font-size: 12px;
  color: #64748b;
}

.ctp-close {
  width: 32px;
  height: 32px;
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.96);
  color: #64748b;
  cursor: pointer;
  transition: all 0.15s ease;
}

.ctp-close:hover {
  color: #ef4444;
  border-color: #fecaca;
  background: #fff1f2;
}

.ctp-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px 22px 28px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ctp-result-card,
.ctp-formula-card,
.ctp-step-card {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.9);
  box-shadow: 0 10px 32px rgba(15, 23, 42, 0.06);
}

.ctp-result-card {
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ctp-label,
.ctp-section-title {
  font-size: 12px;
  font-weight: 700;
  color: #6366f1;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.ctp-result {
  font-size: 28px;
  line-height: 1.2;
  font-weight: 800;
  color: #0f172a;
}

.ctp-summary {
  font-size: 13px;
  line-height: 1.75;
  color: #475569;
}

.ctp-formula-card {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ctp-formula,
.ctp-step-expression {
  font-size: 14px;
  line-height: 1.75;
  color: #1e293b;
  white-space: pre-wrap;
}

.ctp-steps {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.ctp-step-card {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ctp-step-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.ctp-step-title {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
}

.ctp-step-result {
  font-size: 13px;
  font-weight: 700;
  color: #4338ca;
}

.ctp-operands {
  display: grid;
  gap: 10px;
}

.ctp-operand {
  border: 1px solid rgba(99, 102, 241, 0.14);
  border-radius: 14px;
  padding: 12px 14px;
  background: linear-gradient(135deg, rgba(248, 250, 252, 0.96), rgba(238, 242, 255, 0.72));
  text-align: left;
  transition: all 0.15s ease;
}

.ctp-operand.is-clickable {
  cursor: pointer;
}

.ctp-operand.is-clickable:hover {
  transform: translateY(-1px);
  border-color: rgba(79, 70, 229, 0.28);
  box-shadow: 0 8px 22px rgba(99, 102, 241, 0.12);
}

.ctp-operand-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.ctp-operand-label {
  font-size: 13px;
  font-weight: 700;
  color: #334155;
}

.ctp-operand-period {
  font-size: 11px;
  color: #64748b;
}

.ctp-operand-value {
  margin-top: 6px;
  font-size: 18px;
  font-weight: 800;
  color: #111827;
}

.ctp-operand-foot {
  margin-top: 8px;
  font-size: 12px;
  color: #6366f1;
}
</style>
