<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'

const props = withDefaults(
  defineProps<{
    option?: Record<string, any>
    height?: string
  }>(),
  {
    height: '320px'
  }
)

const chartRef = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

const renderChart = () => {
  if (!chartRef.value || !props.option) {
    return
  }

  if (!chart) {
    chart = echarts.init(chartRef.value)
  }

  const animatedOption = {
    ...props.option,
    animation: true,
    animationDuration: 1200,
    animationDurationUpdate: 600,
    animationEasing: 'cubicOut' as const,
    animationDelay: (idx: number) => idx * 80,
  }
  chart.setOption(animatedOption, true)
}

const resizeChart = () => {
  chart?.resize()
}

watch(
  () => props.option,
  () => {
    renderChart()
  },
  { deep: true }
)

onMounted(() => {
  renderChart()
  window.addEventListener('resize', resizeChart)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div v-if="option" ref="chartRef" class="chart-shell" :style="{ height }"></div>
  <el-empty v-else description="暂未生成图表" />
</template>
