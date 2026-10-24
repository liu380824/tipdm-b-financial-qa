<script setup lang="ts">
defineProps<{
  title: string
  value: string
  subtitle: string
}>()
</script>

<template>
  <div class="metric-card">
    <div class="metric-title">{{ title }}</div>
    <div class="metric-value">{{ value }}</div>
    <div class="metric-subtitle">{{ subtitle }}</div>
  </div>
</template>
