<script setup lang="ts">
import { computed } from 'vue'
import { ChatDotRound, Connection, DataAnalysis, FolderOpened } from '@element-plus/icons-vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '../store/app'

const route = useRoute()
const appStore = useAppStore()

const pageTitle = computed(() => String(route.meta.title || appStore.title))

const menuItems = [
  { path: '/assistant', label: '智能对话', icon: ChatDotRound },
  { path: '/database', label: '数据库管理', icon: Connection }
]

const showResultFolder = () => {
  window.alert(`当前为前端原型，结果目录将映射至 ${appStore.resultFolder}`)
}
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand-card">
        <div class="brand-icon">
          <el-icon><DataAnalysis /></el-icon>
        </div>
        <div>
          <div class="brand-title">智能问数助手</div>
          <div class="brand-subtitle">上市公司财报任务二前端</div>
        </div>
      </div>

      <el-menu :default-active="route.path" router class="nav-menu">
        <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-panel">
        <div class="sidebar-label">当前配置</div>
        <div class="sidebar-tags">
          <el-tag type="success" effect="dark">{{ appStore.modelName }}</el-tag>
          <el-tag effect="plain">{{ appStore.databaseFile }}</el-tag>
          <el-tag type="warning" effect="plain">{{ appStore.resultFolder }}</el-tag>
        </div>
      </div>
    </aside>

    <main class="workspace">
      <section class="header-card">
        <div class="header-copy">
          <div class="header-title">{{ pageTitle }}</div>
          <div class="header-subtitle">{{ appStore.subtitle }}</div>
        </div>

        <div class="header-actions">
          <el-tag type="success">数据库已接入</el-tag>
          <el-tag type="info">最近同步 {{ appStore.lastSync }}</el-tag>
          <el-button type="primary" plain @click="showResultFolder">
            <el-icon><FolderOpened /></el-icon>
            查看结果目录
          </el-button>
        </div>
      </section>

      <router-view />
    </main>
  </div>
</template>
