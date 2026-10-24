import { defineStore } from 'pinia'

export const useAppStore = defineStore('app', {
  state: () => ({
    title: '上市公司财报智能问数助手',
    subtitle: '面向任务二的自然语言问数、SQL 映射、图表展示与结论生成前端',
    modelName: 'glm-4-flash',
    databaseFile: 'financial_reports.db',
    resultFolder: './result',
    lastSync: '2026-03-30 20:18',
    sidebarCollapsed: false
  }),
  actions: {
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed
    }
  }
})
