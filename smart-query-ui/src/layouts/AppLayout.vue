<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, RouterLink, RouterView } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, Delete, Connection, Reading, House, Setting } from '@element-plus/icons-vue'
import { useChatStore } from '../store/chat'
import { useAppStore } from '../store/app'
import { exportTask2Results, exportTask2WithFile, fetchTask2Questions, exportTask3Results, exportTask3WithFile, fetchTask3Questions } from '../services/api'

const route = useRoute()
const chat = useChatStore()
const app = useAppStore()
const isHome = computed(() => route.path === '/')
const isAssistant = computed(() => route.path.startsWith('/assistant'))
const isDatabase = computed(() => route.path.startsWith('/database'))
const isKnowledge = computed(() => route.path.startsWith('/knowledge'))
const isSettings = computed(() => route.path.startsWith('/settings'))
const isProfile = computed(() => route.path.startsWith('/profile'))

// ── Batch state ──────────────────────────────────────────────────────────────
const showBatch = ref(false)
const batchRunning = ref(false)
const batchDone = ref(false)
const batchCount = ref(0)
const questionCount = ref(0)
const selectedFile = ref<File | null>(null)
const fileInputRef = ref<HTMLInputElement>()

const onFileChange = (e: Event) => {
  const input = e.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
}

// ── Task 3 batch state ────────────────────────────────────────────────────────
const showBatch3 = ref(false)
const batch3Running = ref(false)
const batch3Done = ref(false)
const batch3Count = ref(0)
const question3Count = ref(0)
const selectedFile3 = ref<File | null>(null)
const fileInput3Ref = ref<HTMLInputElement>()

const onFile3Change = (e: Event) => {
  const input = e.target as HTMLInputElement
  selectedFile3.value = input.files?.[0] ?? null
}

const runBatch3 = async () => {
  if (batch3Running.value) return
  batch3Running.value = true
  batch3Done.value = false
  try {
    const result = selectedFile3.value
      ? await exportTask3WithFile(selectedFile3.value)
      : await exportTask3Results()
    batch3Count.value = result.questionCount
    batch3Done.value = true
    ElMessage.success(`任务三处理完成，共 ${result.questionCount} 题`)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '任务三处理失败')
  } finally {
    batch3Running.value = false
  }
}

const runBatch = async () => {
  if (batchRunning.value) return
  batchRunning.value = true
  batchDone.value = false
  try {
    const result = selectedFile.value
      ? await exportTask2WithFile(selectedFile.value)
      : await exportTask2Results()
    batchCount.value = result.questionCount
    batchDone.value = true
    ElMessage.success(`批量处理完成，共 ${result.questionCount} 题`)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量处理失败')
  } finally {
    batchRunning.value = false
  }
}

const openBatchDrawer = () => {
  showBatch3.value = false
  showBatch.value = true
}

const openBatch3Drawer = () => {
  showBatch.value = false
  showBatch3.value = true
}

onMounted(async () => {
  window.addEventListener('assistant-open-batch-task2', openBatchDrawer as EventListener)
  window.addEventListener('assistant-open-batch-task3', openBatch3Drawer as EventListener)
  try {
    const data = await fetchTask2Questions()
    questionCount.value = data.count
  } catch { /* ignore */ }
  try {
    const data3 = await fetchTask3Questions()
    question3Count.value = data3.count
  } catch { /* ignore */ }
})

onBeforeUnmount(() => {
  window.removeEventListener('assistant-open-batch-task2', openBatchDrawer as EventListener)
  window.removeEventListener('assistant-open-batch-task3', openBatch3Drawer as EventListener)
})
</script>

<template>
  <div class="app-layout">
    <!-- ── Sidebar ── -->
    <aside class="sidebar" :class="{ 'sidebar--collapsed': app.sidebarCollapsed }">
      <div class="brand">
        <div class="brand-icon" :class="{ 'brand-icon--clickable': app.sidebarCollapsed }" @click="app.sidebarCollapsed && app.toggleSidebar()">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z"/></svg>
        </div>
        <div class="brand-text">
          <div class="brand-name">智能问数</div>
          <div class="brand-sub">Financial AI</div>
        </div>
        <button class="sidebar-toggle" @click="app.toggleSidebar" title="收起/展开侧栏">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="18" height="18" rx="3" /><line x1="9" y1="3" x2="9" y2="21" /></svg>
        </button>
      </div>

      <nav class="sidebar-nav">
        <RouterLink class="nav-item" to="/" :class="{ 'nav-item--active': isHome }">
          <span class="nav-item-icon">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 12l8.954-8.955c.44-.439 1.152-.439 1.591 0L21.75 12M4.5 9.75v10.125c0 .621.504 1.125 1.125 1.125H9.75v-4.875c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21h4.125c.621 0 1.125-.504 1.125-1.125V9.75M8.25 21h8.25"/></svg>
          </span>
          <span class="nav-label">首页</span>
        </RouterLink>
        <RouterLink class="nav-item" to="/assistant" :class="{ 'nav-item--active': isAssistant }">
          <span class="nav-item-icon">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 01-.825-.242m9.345-8.334a2.126 2.126 0 00-.476-.095 48.64 48.64 0 00-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0011.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155"/></svg>
          </span>
          <span class="nav-label">智能对话</span>
        </RouterLink>
        <RouterLink class="nav-item" to="/database" :class="{ 'nav-item--active': isDatabase }">
          <span class="nav-item-icon">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125"/></svg>
          </span>
          <span class="nav-label">数据库管理</span>
        </RouterLink>
        <RouterLink class="nav-item" to="/knowledge" :class="{ 'nav-item--active': isKnowledge }">
          <span class="nav-item-icon">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25"/></svg>
          </span>
          <span class="nav-label">知识库管理</span>
        </RouterLink>
        <RouterLink class="nav-item" to="/settings" :class="{ 'nav-item--active': isSettings }">
          <span class="nav-item-icon">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
          </span>
          <span class="nav-label">系统设置</span>
        </RouterLink>
      </nav>

      <!-- Assistant-only sidebar content -->
      <template v-if="isAssistant">
        <div class="nav-divider"></div>

        <button class="new-btn" @click="chat.newConversation()">
          <el-icon><Plus /></el-icon>
          <span class="nav-label">新建对话</span>
        </button>

        <div class="conv-list">
          <div
            v-for="conv in chat.conversations"
            :key="conv.id"
            class="conv-item"
            :class="{ active: conv.id === chat.activeId }"
            @click="chat.switchConversation(conv.id)"
          >
            <span class="conv-title">{{ conv.title }}</span>
            <button class="del-btn" @click.stop="chat.deleteConversation(conv.id)">
              <el-icon><Delete /></el-icon>
            </button>
          </div>
        </div>
      </template>

      <div class="sidebar-footer">
        <RouterLink class="nav-item" to="/profile" :class="{ 'nav-item--active': isProfile }">
          <span class="nav-item-icon">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z"/></svg>
          </span>
          <span class="nav-label">个人中心</span>
        </RouterLink>
      </div>
    </aside>

    <!-- ── Content (router-view) ── -->
    <div class="content">
      <RouterView v-slot="{ Component }">
        <KeepAlive include="AssistantView">
          <component :is="Component" />
        </KeepAlive>
      </RouterView>
    </div>

    <!-- ── Batch Drawer ── -->
    <Teleport to="body">
      <Transition name="drawer">
        <div v-if="showBatch" class="batch-drawer-wrap">
          <div class="batch-drawer-mask" @click="!batchRunning && (showBatch = false)" />
          <div class="batch-drawer">
            <div class="bm-header">
              <span>⚡ 批量回答（任务二）</span>
              <button class="bm-close" :disabled="batchRunning" @click="showBatch = false">✕</button>
            </div>

            <div class="bm-desc">
              选择问题文件（附件4格式 xlsx），使用智能问数助手逐题回答，结果按附件7表3格式保存为
              <code>result_2.xlsx</code>，图表保存至 <code>result/</code> 目录。
            </div>

            <div class="bm-file-section">
              <div class="bm-file-label">问题文件（.xlsx）</div>
              <div class="bm-file-row">
                <div class="bm-file-name">
                  {{ selectedFile ? selectedFile.name : questionCount ? `附件4（自动检测，${questionCount} 题）` : '未选择文件' }}
                </div>
                <button class="bm-pick-btn" :disabled="batchRunning" @click="fileInputRef?.click()">
                  选择文件
                </button>
                <input
                  ref="fileInputRef"
                  type="file"
                  accept=".xlsx"
                  style="display:none"
                  @change="onFileChange"
                />
              </div>
              <div v-if="selectedFile" class="bm-file-clear">
                <button @click="selectedFile = null; if(fileInputRef) fileInputRef.value = ''">✕ 清除，使用自动检测</button>
              </div>
            </div>

            <div v-if="!batchRunning && !batchDone" class="bm-start">
              <button class="bm-run-btn" @click="runBatch">开始批量处理</button>
            </div>

            <div v-if="batchRunning" class="bm-running">
              <div class="bm-spinner"></div>
              <div class="bm-tip">正在逐题调用智能问数助手，请勿关闭页面…</div>
              <div class="bm-sub">处理时间取决于问题数量和网络情况</div>
            </div>

            <div v-if="batchDone" class="bm-done">
              <div class="bm-check">✓</div>
              <div class="bm-done-msg">共处理 <strong>{{ batchCount }}</strong> 道题，result_2.xlsx 已生成</div>
              <a class="bm-dl-btn" href="/api/task2/result-file" download="result_2.xlsx">下载 result_2.xlsx</a>
              <button class="bm-reset" @click="batchDone = false; selectedFile = null">重新处理</button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ── Task 3 Batch Drawer ── -->
    <Teleport to="body">
      <Transition name="drawer">
        <div v-if="showBatch3" class="batch-drawer-wrap">
          <div class="batch-drawer-mask" @click="!batch3Running && (showBatch3 = false)" />
          <div class="batch-drawer">
            <div class="bm-header">
              <span>🧠 批量回答（任务三·增强版）</span>
              <button class="bm-close" :disabled="batch3Running" @click="showBatch3 = false">✕</button>
            </div>

            <div class="bm-desc">
              选择问题文件（附件6格式 xlsx），使用<strong>增强版智能问数助手</strong>（多意图规划 + 知识库归因）逐题回答，
              结果按附件7表6格式保存为 <code>result_3.xlsx</code>。
            </div>

            <div class="bm-file-section">
              <div class="bm-file-label">问题文件（.xlsx）</div>
              <div class="bm-file-row">
                <div class="bm-file-name">
                  {{ selectedFile3 ? selectedFile3.name : question3Count ? `附件6（自动检测，${question3Count} 题）` : '未选择文件' }}
                </div>
                <button class="bm-pick-btn" :disabled="batch3Running" @click="fileInput3Ref?.click()">选择文件</button>
                <input ref="fileInput3Ref" type="file" accept=".xlsx" style="display:none" @change="onFile3Change" />
              </div>
              <div v-if="selectedFile3" class="bm-file-clear">
                <button @click="selectedFile3 = null; if(fileInput3Ref) fileInput3Ref.value = ''">✕ 清除，使用自动检测</button>
              </div>
            </div>

            <div v-if="!batch3Running && !batch3Done" class="bm-start">
              <button class="bm-run-btn" @click="runBatch3">开始批量处理</button>
            </div>

            <div v-if="batch3Running" class="bm-running">
              <div class="bm-spinner"></div>
              <div class="bm-tip">正在使用增强助手逐题回答（多意图 + 知识库），请勿关闭页面…</div>
              <div class="bm-sub">处理时间较长，取决于问题数量和 LLM 响应速度</div>
            </div>

            <div v-if="batch3Done" class="bm-done">
              <div class="bm-check">✓</div>
              <div class="bm-done-msg">共处理 <strong>{{ batch3Count }}</strong> 道题，result_3.xlsx 已生成</div>
              <a class="bm-dl-btn" href="/api/task3/result-file" download="result_3.xlsx">下载 result_3.xlsx</a>
              <button class="bm-reset" @click="batch3Done = false; selectedFile3 = null">重新处理</button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
/* ═══════════════════════════════════════════════
   AppLayout — Premium Dark Sidebar Shell
   ═══════════════════════════════════════════════ */

/* ── App shell ── */
.app-layout {
  position: fixed;
  inset: 0;
  display: flex;
  font-family: var(--font-body);
  background: var(--bg-primary);
}

.content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-primary);
}

/* ── Sidebar ── */
.sidebar {
  width: 240px;
  flex-shrink: 0;
  background: var(--sidebar-bg);
  display: flex;
  flex-direction: column;
  padding: 16px 12px;
  gap: 4px;
  overflow: hidden;
  border-right: 1px solid var(--sidebar-border);
}

/* ── Brand ── */
.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 8px 8px 18px;
  border-bottom: 1px solid var(--sidebar-border);
  margin-bottom: 8px;
}
.brand-icon {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: linear-gradient(135deg, #2563eb, #3b82f6);
  display: grid;
  place-items: center;
  color: #fff;
  flex-shrink: 0;
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.3);
}
.brand-name {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 700;
  color: #eaecf0;
  letter-spacing: -0.01em;
}
.brand-sub {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--sidebar-text);
  margin-top: 2px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

/* ── Navigation ── */
.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--sidebar-text);
  text-decoration: none;
  transition: all 0.15s ease;
  cursor: pointer;
  position: relative;
}
.nav-item:hover {
  background: var(--sidebar-surface);
  color: var(--sidebar-text-hover);
}
.nav-item--active {
  background: var(--sidebar-accent);
  color: var(--sidebar-text-active);
  font-weight: 600;
}
.nav-item--active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 16px;
  border-radius: 0 3px 3px 0;
  background: #3b82f6;
}
.nav-item-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  opacity: 0.6;
}
.nav-item:hover .nav-item-icon,
.nav-item--active .nav-item-icon {
  opacity: 1;
}

/* ── Divider ── */
.nav-divider {
  height: 1px;
  background: var(--sidebar-border);
  margin: 8px 4px;
}

/* ── New conversation button ── */
.new-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 12px;
  border-radius: 8px;
  border: 1px dashed rgba(255,255,255,0.1);
  background: transparent;
  color: var(--sidebar-text);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.new-btn:hover {
  background: var(--sidebar-surface);
  color: var(--sidebar-text-hover);
  border-color: rgba(255,255,255,0.15);
}

/* ── Conversation list ── */
.conv-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin-top: 6px;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.08) transparent;
}
.conv-item {
  display: flex;
  align-items: center;
  padding: 7px 10px;
  border-radius: 7px;
  cursor: pointer;
  transition: all 0.12s ease;
  gap: 6px;
}
.conv-item:hover {
  background: var(--sidebar-surface);
}
.conv-item.active {
  background: var(--sidebar-accent);
}
.conv-title {
  flex: 1;
  font-size: 12px;
  color: var(--sidebar-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-item:hover .conv-title { color: var(--sidebar-text-hover); }
.conv-item.active .conv-title { color: var(--sidebar-text-active); font-weight: 500; }
.del-btn {
  opacity: 0;
  width: 20px;
  height: 20px;
  border: none;
  background: transparent;
  color: var(--sidebar-text);
  cursor: pointer;
  display: grid;
  place-items: center;
  border-radius: 4px;
  font-size: 11px;
  transition: all 0.12s ease;
}
.conv-item:hover .del-btn { opacity: 1; }
.del-btn:hover { color: #f87171; background: rgba(248, 113, 113, 0.1); }

/* ── Sidebar footer ── */
.sidebar-footer {
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--sidebar-border);
}

/* ── Batch Drawer ── */
.batch-drawer-wrap {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
}
.batch-drawer-mask {
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,0.4);
  backdrop-filter: blur(2px);
}
.batch-drawer {
  position: relative;
  width: 440px;
  height: 100%;
  background: var(--bg-secondary);
  box-shadow: -8px 0 32px rgba(0,0,0,0.12);
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: 28px 24px;
  overflow-y: auto;
}
.bm-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
}
.bm-close {
  width: 30px; height: 30px;
  border-radius: var(--radius-md); border: 1px solid var(--border-primary); background: var(--bg-tertiary);
  color: var(--text-tertiary); cursor: pointer; font-size: 14px;
  display: grid; place-items: center;
  transition: all 0.15s ease;
}
.bm-close:hover { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger); }
.bm-desc { font-size: 13px; color: var(--text-secondary); line-height: 1.7; }
.bm-desc code {
  background: var(--bg-inset); padding: 2px 6px; border-radius: var(--radius-xs);
  font-family: var(--font-mono); font-size: 12px; color: var(--accent);
}
.bm-file-section { display: flex; flex-direction: column; gap: 8px; }
.bm-file-label { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.bm-file-row { display: flex; align-items: center; gap: 10px; }
.bm-file-name {
  flex: 1; font-size: 12px; color: var(--text-tertiary);
  background: var(--bg-tertiary); padding: 8px 12px; border-radius: var(--radius-md); border: 1px solid var(--border-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.bm-pick-btn {
  padding: 8px 16px; border-radius: var(--radius-md); border: 1px solid var(--border-primary);
  background: var(--bg-secondary); font-size: 13px; color: var(--text-secondary); cursor: pointer; white-space: nowrap;
  transition: all 0.15s ease;
}
.bm-pick-btn:hover { background: var(--bg-tertiary); border-color: var(--border-hover); }
.bm-file-clear button {
  font-size: 12px; color: var(--text-tertiary); background: none; border: none; cursor: pointer; padding: 0;
}
.bm-file-clear button:hover { color: var(--text-secondary); }
.bm-start { display: flex; justify-content: center; margin-top: 8px; }
.bm-run-btn {
  padding: 11px 32px; border-radius: var(--radius-md); border: none;
  background: var(--accent); color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
  transition: all 0.15s ease;
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.25);
}
.bm-run-btn:hover { background: var(--accent-hover); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3); }
.bm-running { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 12px 0; }
.bm-spinner {
  width: 36px; height: 36px; border: 3px solid var(--border-primary);
  border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.bm-tip { font-size: 14px; color: var(--text-primary); text-align: center; }
.bm-sub { font-size: 12px; color: var(--text-tertiary); text-align: center; }
.bm-done { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 8px 0; }
.bm-check { font-size: 36px; color: var(--success); }
.bm-done-msg { font-size: 14px; color: var(--text-primary); text-align: center; }
.bm-dl-btn {
  padding: 10px 24px; border-radius: var(--radius-md);
  background: var(--success); color: #fff; font-size: 13px; font-weight: 600; text-decoration: none;
  transition: all 0.15s ease;
}
.bm-dl-btn:hover { opacity: 0.9; transform: translateY(-1px); }
.bm-reset { font-size: 12px; color: var(--text-tertiary); background: none; border: none; cursor: pointer; margin-top: 4px; }
.bm-reset:hover { color: var(--text-secondary); }

/* ── Drawer transition ── */
.drawer-enter-active, .drawer-leave-active { transition: opacity 0.24s ease; }
.drawer-enter-active .batch-drawer, .drawer-leave-active .batch-drawer { transition: transform 0.24s ease; }
.drawer-enter-from, .drawer-leave-to { opacity: 0; }
.drawer-enter-from .batch-drawer, .drawer-leave-to .batch-drawer { transform: translateX(100%); }

/* ── Sidebar toggle button ── */
.sidebar-toggle {
  margin-left: auto;
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: 1px solid var(--sidebar-border);
  background: transparent;
  color: var(--sidebar-text);
  display: grid;
  place-items: center;
  cursor: pointer;
  opacity: 0.5;
  transition: all 0.15s ease;
}
.sidebar-toggle:hover {
  opacity: 1;
  background: var(--sidebar-surface);
  border-color: rgba(255,255,255,0.15);
}

/* ── Sidebar collapse transition ── */
.sidebar {
  transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1), padding 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
.nav-label {
  white-space: nowrap;
  overflow: hidden;
  transition: opacity 0.2s ease, width 0.25s ease;
}

/* ── Collapsed state ── */
.sidebar--collapsed {
  width: 60px;
  padding: 16px 8px;
  align-items: center;
}
.sidebar--collapsed .brand {
  justify-content: center;
  padding: 8px 0 18px;
}
.sidebar--collapsed .brand-text {
  display: none;
}
.sidebar--collapsed .sidebar-toggle {
  display: none;
}
.brand-icon--clickable {
  cursor: pointer;
}
.brand-icon--clickable:hover {
  box-shadow: 0 2px 12px rgba(37, 99, 235, 0.5);
}
.sidebar--collapsed .nav-label {
  opacity: 0;
  width: 0;
  overflow: hidden;
}
.sidebar--collapsed .nav-item {
  justify-content: center;
  padding: 9px;
  gap: 0;
}
.sidebar--collapsed .nav-item--active::before {
  left: -8px;
}
.sidebar--collapsed .nav-divider {
  margin: 8px 0;
  width: 100%;
}
.sidebar--collapsed .new-btn {
  justify-content: center;
  padding: 9px;
  gap: 0;
}
.sidebar--collapsed .conv-list {
  display: none;
}
.sidebar--collapsed .conv-item {
  display: none;
}
</style>
