<script setup lang="ts">
import { computed, onMounted, reactive, ref, nextTick } from 'vue'
import { RouterLink } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchSystemSettings, saveSystemSettings } from '../services/api'
import { useChatStore } from '../store/chat'
import type { SystemSettings, SystemSettingsPayload } from '../types'
import {
  DEFAULT_PROFILE_PREFERENCES, PROFILE_AVATAR_OPTIONS, ROLE_LABEL_OPTIONS,
  THEME_OPTIONS, FONT_SIZE_OPTIONS, ANSWER_DETAIL_OPTIONS,
  NUMBER_FORMAT_OPTIONS, ANALYSIS_FOCUS_OPTIONS,
  loadProfilePreferences, saveProfilePreferences,
  type ThemeMode, type AnswerDetail, type NumberFormat, type AnalysisFocus, type FontSize,
} from '../utils/profilePreferences'

const chat = useChatStore()
const loading = ref(false)
const saving = ref(false)
const loaded = ref<SystemSettings | null>(null)
const favoritePromptDraft = ref('')
const feedbackText = ref('')
const feedbackSent = ref(false)
const backendOnline = ref<boolean | null>(null)

const form = reactive({
  enable_multi_intent: true,
  kb_top_k: 3,
  default_auto_chart: true,
  default_enable_context: true
})

const profile = reactive({
  nickname: DEFAULT_PROFILE_PREFERENCES.nickname,
  avatarEmoji: DEFAULT_PROFILE_PREFERENCES.avatarEmoji,
  roleLabel: DEFAULT_PROFILE_PREFERENCES.roleLabel,
  signature: DEFAULT_PROFILE_PREFERENCES.signature,
  favoritePrompts: [...DEFAULT_PROFILE_PREFERENCES.favoritePrompts],
  theme: DEFAULT_PROFILE_PREFERENCES.theme as ThemeMode,
  fontSize: DEFAULT_PROFILE_PREFERENCES.fontSize as FontSize,
  answerDetail: DEFAULT_PROFILE_PREFERENCES.answerDetail as AnswerDetail,
  numberFormat: DEFAULT_PROFILE_PREFERENCES.numberFormat as NumberFormat,
  analysisFocus: DEFAULT_PROFILE_PREFERENCES.analysisFocus as AnalysisFocus,
})

const applyProfile = () => {
  const prefs = loadProfilePreferences()
  profile.nickname = prefs.nickname
  profile.avatarEmoji = prefs.avatarEmoji
  profile.roleLabel = prefs.roleLabel
  profile.signature = prefs.signature
  profile.favoritePrompts = [...prefs.favoritePrompts]
  profile.theme = prefs.theme
  profile.fontSize = prefs.fontSize
  profile.answerDetail = prefs.answerDetail
  profile.numberFormat = prefs.numberFormat
  profile.analysisFocus = prefs.analysisFocus
}

const applySettings = (settings: SystemSettings) => {
  loaded.value = settings
  form.enable_multi_intent = settings.assistant.enable_multi_intent
  form.kb_top_k = settings.assistant.kb_top_k
  form.default_auto_chart = settings.assistant.default_auto_chart
  form.default_enable_context = settings.assistant.default_enable_context
}

const checkBackend = async () => {
  try {
    const resp = await fetch('/api/system/settings', { signal: AbortSignal.timeout(5000) })
    backendOnline.value = resp.ok
  } catch { backendOnline.value = false }
}

const loadSettings = async () => {
  loading.value = true
  applyProfile()
  checkBackend()
  try {
    const settings = await fetchSystemSettings()
    applySettings(settings)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '个人中心加载失败')
  } finally {
    loading.value = false
  }
}

const buildLocalProfilePayload = () => ({
  nickname: profile.nickname,
  avatarEmoji: profile.avatarEmoji,
  roleLabel: profile.roleLabel,
  signature: profile.signature,
  favoritePrompts: [...profile.favoritePrompts],
  theme: profile.theme,
  fontSize: profile.fontSize,
  answerDetail: profile.answerDetail,
  numberFormat: profile.numberFormat,
  analysisFocus: profile.analysisFocus,
})

const buildPayload = (): SystemSettingsPayload | null => {
  if (!loaded.value) return null
  return {
    database: { path: loaded.value.database.path },
    reports_dir: loaded.value.reports_dir,
    research_reports_dir: loaded.value.research_reports_dir,
    parallel: { max_workers: loaded.value.parallel.max_workers },
    assistant: {
      enable_multi_intent: form.enable_multi_intent,
      kb_top_k: Number(form.kb_top_k),
      default_auto_chart: form.default_auto_chart,
      default_enable_context: form.default_enable_context
    },
    llm: {
      base_url: loaded.value.llm.base_url,
      model: loaded.value.llm.model,
      request_timeout: loaded.value.llm.request_timeout,
      intent_temperature: loaded.value.llm.intent_temperature,
      query_plan_temperature: loaded.value.llm.query_plan_temperature,
      analysis_temperature: loaded.value.llm.analysis_temperature,
      general_reply_temperature: loaded.value.llm.general_reply_temperature,
      planner_decompose_temperature: loaded.value.llm.planner_decompose_temperature,
      synthesis_temperature: loaded.value.llm.synthesis_temperature,
      ref_extract_temperature: loaded.value.llm.ref_extract_temperature
    },
    extract_llm: {
      base_url: loaded.value.extract_llm.base_url,
      model: loaded.value.extract_llm.model,
      request_timeout: loaded.value.extract_llm.request_timeout
    },
    intent_llm: {
      base_url: loaded.value.intent_llm.base_url,
      model: loaded.value.intent_llm.model,
      request_timeout: loaded.value.intent_llm.request_timeout
    },
    embedding: {
      base_url: loaded.value.embedding.base_url,
      model: loaded.value.embedding.model,
      reranker_model: loaded.value.embedding.reranker_model
    }
  }
}

const handleSave = async () => {
  saving.value = true
  let localSaved = false
  try {
    saveProfilePreferences(buildLocalProfilePayload())
    localSaved = true
    applyTheme(profile.theme)
    const payload = buildPayload()
    if (payload) {
      const result = await saveSystemSettings(payload)
      applySettings(result)
      ElMessage.success('个人中心设置已保存')
    } else {
      ElMessage.success('个人资料已保存')
    }
  } catch (err) {
    ElMessage.error(localSaved
      ? `系统偏好保存失败（昵称与常用问题已本地保存）：${err instanceof Error ? err.message : '未知错误'}`
      : err instanceof Error ? err.message : '个人偏好保存失败')
  } finally {
    saving.value = false
  }
}

const addFavoritePrompt = () => {
  const prompt = favoritePromptDraft.value.trim()
  if (!prompt) return
  if (profile.favoritePrompts.includes(prompt)) {
    ElMessage.warning('该常用问题已存在')
    return
  }
  if (profile.favoritePrompts.length >= 8) {
    ElMessage.warning('最多保留 8 条常用问题偏好')
    return
  }
  profile.favoritePrompts.unshift(prompt)
  favoritePromptDraft.value = ''
}

const removeFavoritePrompt = (index: number) => {
  profile.favoritePrompts.splice(index, 1)
}

const copyFavoritePrompt = async (prompt: string) => {
  try {
    await navigator.clipboard.writeText(prompt)
    ElMessage.success('常用问题已复制')
  } catch {
    ElMessage.error('复制失败')
  }
}

const toggleQuickSwitch = (key: 'multi_intent' | 'auto_chart' | 'context' | 'recall') => {
  if (key === 'multi_intent') { form.enable_multi_intent = !form.enable_multi_intent; return }
  if (key === 'auto_chart') { form.default_auto_chart = !form.default_auto_chart; return }
  if (key === 'context') { form.default_enable_context = !form.default_enable_context; return }
  form.kb_top_k = form.kb_top_k >= 5 ? 3 : 6
}

/* ── Theme ── */
const applyTheme = (mode: ThemeMode) => {
  const root = document.documentElement
  if (mode === 'auto') {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    root.setAttribute('data-theme', prefersDark ? 'dark' : 'light')
  } else {
    root.setAttribute('data-theme', mode)
  }
}

/* ── Data Export ── */
const exportAllConversations = () => {
  const data = chat.conversations.map(c => ({
    id: c.id, title: c.title,
    messages: c.messages.map(m => ({ role: m.role, content: m.content }))
  }))
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = `smart-query-conversations-${new Date().toISOString().slice(0, 10)}.json`
  a.click(); URL.revokeObjectURL(url)
  ElMessage.success(`已导出 ${data.length} 个对话`)
}

const exportSettings = () => {
  const data = { profile: buildLocalProfilePayload(), system: form }
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = `smart-query-settings-${new Date().toISOString().slice(0, 10)}.json`
  a.click(); URL.revokeObjectURL(url)
  ElMessage.success('设置已导出')
}

const clearAllConversations = async () => {
  try {
    await ElMessageBox.confirm('确定要删除所有会话记录吗？此操作不可恢复。', '清空会话', { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' })
    const count = chat.conversations.length
    while (chat.conversations.length > 1) chat.deleteConversation(chat.conversations[chat.conversations.length - 1].id)
    chat.deleteConversation(chat.conversations[0].id)
    ElMessage.success(`已清空 ${count} 个会话`)
  } catch { /* cancelled */ }
}

const clearLocalCache = async () => {
  try {
    await ElMessageBox.confirm('将清除本地缓存的偏好设置，页面会刷新。', '清除缓存', { type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消' })
    localStorage.removeItem('smart-query-ui-profile-preferences')
    location.reload()
  } catch { /* cancelled */ }
}

/* ── Feedback ── */
const submitFeedback = () => {
  if (!feedbackText.value.trim()) return
  feedbackSent.value = true
  feedbackText.value = ''
  setTimeout(() => { feedbackSent.value = false }, 3000)
}

/* ── Stats ── */
const summaryCards = computed(() => [
  { label: '当前昵称', value: profile.nickname, detail: profile.roleLabel },
  { label: '常用问题偏好', value: `${profile.favoritePrompts.length} 条`, detail: '持续积累常问问题，便于快速复用' },
  { label: '最近使用会话', value: `${recentUsage.value.length} 个`, detail: '根据当前会话记录生成使用概览' },
  { label: '当前对话模式', value: form.enable_multi_intent ? '多意图' : '单任务', detail: form.default_enable_context ? '默认继承上下文' : '默认独立问答' }
])

const quickSwitchCards = computed(() => [
  { key: 'multi_intent' as const, title: '多意图规划', detail: '复合问题自动拆解并并行执行', active: form.enable_multi_intent, value: form.enable_multi_intent ? '已启用' : '已关闭', tone: 'violet' },
  { key: 'auto_chart' as const, title: '自动图表', detail: '查询完成后默认尝试生成图表', active: form.default_auto_chart, value: form.default_auto_chart ? '自动生成' : '手动触发', tone: 'sky' },
  { key: 'context' as const, title: '上下文继承', detail: '连续追问时默认保留历史语境', active: form.default_enable_context, value: form.default_enable_context ? '已开启' : '已关闭', tone: 'emerald' },
  { key: 'recall' as const, title: '高召回模式', detail: '在知识库问答中带入更多参考片段', active: form.kb_top_k >= 5, value: `Top ${form.kb_top_k}`, tone: 'amber' }
])

const recentUsage = computed(() =>
  chat.conversations
    .map((conv, idx) => {
      const userMessages = conv.messages.filter(msg => msg.role === 'user')
      if (!userMessages.length) return null
      return {
        id: conv.id, title: conv.title,
        lastQuestion: userMessages[userMessages.length - 1]?.content || '',
        userTurns: userMessages.length,
        rankLabel: idx === 0 ? '最近使用' : `历史会话 ${idx + 1}`
      }
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item))
    .slice(0, 4)
)

/* ── Mini chart data (7-day histogram) ── */
const weeklyActivity = computed(() => {
  const now = Date.now()
  const days = Array.from({ length: 7 }, (_, i) => {
    const d = new Date(now - (6 - i) * 86400000)
    return { label: `${d.getMonth() + 1}/${d.getDate()}`, count: 0 }
  })
  chat.conversations.forEach(conv => {
    conv.messages.forEach(msg => {
      if (msg.role !== 'user') return
      const ts = parseInt(msg.id?.split('-')[0] || '0')
      if (!ts) return
      const dayIdx = Math.floor((ts - (now - 6 * 86400000)) / 86400000)
      if (dayIdx >= 0 && dayIdx < 7) days[dayIdx].count++
    })
  })
  const max = Math.max(...days.map(d => d.count), 1)
  return days.map(d => ({ ...d, pct: Math.round((d.count / max) * 100) }))
})

const topCompanies = computed(() => {
  const freq: Record<string, number> = {}
  chat.conversations.forEach(conv => {
    conv.messages.forEach(msg => {
      if (msg.role !== 'user') return
      const matches = msg.content.match(/[\u4e00-\u9fa5]{2,6}(?:股份|集团|药业|制药|医药|科技|控股|实业)/g)
      matches?.forEach(m => { freq[m] = (freq[m] || 0) + 1 })
    })
  })
  return Object.entries(freq).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([name, count]) => ({ name, count }))
})

/* ── Keyboard shortcuts data ── */
const shortcuts = [
  { keys: 'Ctrl + L', desc: '清空当前对话' },
  { keys: 'Ctrl + Z', desc: '撤回上一条消息' },
  { keys: 'Ctrl + C', desc: '复制最新回答' },
  { keys: 'Ctrl + E', desc: '导出当前对话' },
  { keys: 'Ctrl + H', desc: '打开历史面板' },
  { keys: 'Ctrl + ,', desc: '打开设置面板' },
  { keys: 'Enter', desc: '发送消息' },
  { keys: 'Shift + Enter', desc: '消息内换行' },
]

/* ── System info ── */
const systemInfo = computed(() => ({
  version: '1.0.0',
  backend: backendOnline.value === null ? '检测中…' : backendOnline.value ? '在线' : '离线',
  backendOk: backendOnline.value === true,
  model: loaded.value?.llm?.model || '—',
  dbPath: loaded.value?.database?.path || '—',
}))

onMounted(loadSettings)
</script>

<template>
  <div class="pv" v-loading="loading">

    <!-- ══ Header bar ══ -->
    <div class="pv-header">
      <div class="pv-header-title">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z"/></svg>
        <span>个人中心</span>
      </div>
      <div class="pv-header-actions">
        <button class="pv-btn pv-btn--ghost" :disabled="loading || saving" @click="loadSettings">
          <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182"/></svg>
          刷新
        </button>
        <button class="pv-btn pv-btn--primary" :disabled="loading || saving" @click="handleSave">
          <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 3.75H6.912a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H15M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859"/></svg>
          {{ saving ? '保存中…' : '保存配置' }}
        </button>
      </div>
    </div>

    <!-- ══ Content area ══ -->
    <div class="pv-body">

      <!-- ══ Card 1: 个人资料 (enhanced with gradient banner) ══ -->
      <section class="pv-card pv-card--profile">
        <div class="pv-profile-banner"></div>
        <div class="pv-card-head">
          <div class="pv-card-icon">
            <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z"/></svg>
          </div>
          <div>
            <h2>个人资料</h2>
            <p>设置昵称、头像和签名，数据保存在浏览器本地</p>
          </div>
        </div>
        <div class="pv-identity">
          <div class="pv-preview">
            <div class="pv-preview-avatar-wrap">
              <div class="pv-preview-avatar">{{ profile.avatarEmoji }}</div>
              <div class="pv-preview-avatar-edit">更换</div>
            </div>
            <div class="pv-preview-name">{{ profile.nickname }}</div>
            <div class="pv-preview-role">{{ profile.roleLabel }}</div>
            <div class="pv-preview-sig">{{ profile.signature }}</div>
          </div>
          <div class="pv-editor">
            <el-form label-position="top" class="pv-form-2col">
              <el-form-item label="用户昵称">
                <el-input v-model="profile.nickname" maxlength="20" placeholder="请输入你的昵称" />
              </el-form-item>
              <el-form-item label="角色标签">
                <el-select v-model="profile.roleLabel" placeholder="选择角色" filterable allow-create>
                  <el-option v-for="r in ROLE_LABEL_OPTIONS" :key="r" :label="r" :value="r" />
                </el-select>
              </el-form-item>
              <el-form-item label="个人签名" class="pv-span-full">
                <el-input v-model="profile.signature" type="textarea" :rows="3" maxlength="120" show-word-limit placeholder="写一句你偏好的分析风格或常见目标" />
              </el-form-item>
            </el-form>
            <div class="pv-avatar-picker">
              <span class="pv-picker-label">选择头像</span>
              <div class="pv-avatars">
                <button
                  v-for="emoji in PROFILE_AVATAR_OPTIONS"
                  :key="emoji"
                  :class="['pv-ava-btn', { active: profile.avatarEmoji === emoji }]"
                  @click="profile.avatarEmoji = emoji"
                >{{ emoji }}</button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ══ Two-Column Grid: Row 1 ══ -->
      <div class="pv-columns">

        <!-- ── Card 2: 外观与主题 ── -->
        <section class="pv-card">
          <div class="pv-card-head">
            <div class="pv-card-icon pv-card-icon--violet">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9.53 16.122a3 3 0 00-5.78 1.128 2.25 2.25 0 01-2.4 2.245 4.5 4.5 0 008.4-2.245c0-.399-.078-.78-.22-1.128zm0 0a15.998 15.998 0 003.388-1.62m-5.043-.025a15.994 15.994 0 011.622-3.395m3.42 3.42a15.995 15.995 0 004.764-4.648l3.876-5.814a1.151 1.151 0 00-1.597-1.597L14.146 6.32a15.996 15.996 0 00-4.649 4.763m3.42 3.42a6.776 6.776 0 00-3.42-3.42"/></svg>
            </div>
            <div>
              <h2>外观与主题</h2>
              <p>切换浅色/深色主题和字号大小</p>
            </div>
          </div>
          <div class="pv-theme-grid">
            <div v-for="t in THEME_OPTIONS" :key="t.value"
              :class="['pv-theme-card', { active: profile.theme === t.value }]"
              @click="profile.theme = t.value">
              <div :class="['pv-theme-preview', `pv-theme-preview--${t.value}`]">
                <div class="pv-theme-dot"></div>
                <div class="pv-theme-line"></div>
                <div class="pv-theme-line short"></div>
              </div>
              <div class="pv-theme-name">{{ t.label }}</div>
            </div>
          </div>
          <div class="pv-section-divider"></div>
          <div class="pv-section-title">字号大小</div>
          <div class="pv-radio-row">
            <button v-for="f in FONT_SIZE_OPTIONS" :key="f.value"
              :class="['pv-radio-btn', { active: profile.fontSize === f.value }]"
              @click="profile.fontSize = f.value">{{ f.label }}</button>
          </div>
        </section>

        <!-- ── Card 3: 回答风格偏好 ── -->
        <section class="pv-card">
          <div class="pv-card-head">
            <div class="pv-card-icon pv-card-icon--teal">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75"/></svg>
            </div>
            <div>
              <h2>回答风格偏好</h2>
              <p>定制AI分析师的回答方式与关注重点</p>
            </div>
          </div>
          <el-form label-position="top">
            <el-form-item label="回答详细程度">
              <div class="pv-radio-row">
                <button v-for="a in ANSWER_DETAIL_OPTIONS" :key="a.value"
                  :class="['pv-radio-btn', { active: profile.answerDetail === a.value }]"
                  @click="profile.answerDetail = a.value">{{ a.label }}</button>
              </div>
            </el-form-item>
            <el-form-item label="数字格式偏好">
              <div class="pv-radio-row">
                <button v-for="n in NUMBER_FORMAT_OPTIONS" :key="n.value"
                  :class="['pv-radio-btn', { active: profile.numberFormat === n.value }]"
                  @click="profile.numberFormat = n.value">{{ n.label }}</button>
              </div>
            </el-form-item>
            <el-form-item label="默认分析维度">
              <div class="pv-radio-row">
                <button v-for="af in ANALYSIS_FOCUS_OPTIONS" :key="af.value"
                  :class="['pv-radio-btn', { active: profile.analysisFocus === af.value }]"
                  @click="profile.analysisFocus = af.value">{{ af.label }}</button>
              </div>
            </el-form-item>
          </el-form>
        </section>
      </div>

      <!-- ══ Two-Column Grid: Row 2 ══ -->
      <div class="pv-columns">

        <!-- ── Card 5: 使用数据概览 (enhanced with mini charts) ── -->
        <section class="pv-card">
          <div class="pv-card-head">
            <div class="pv-card-icon pv-card-icon--blue">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"/></svg>
            </div>
            <div>
              <h2>使用数据概览</h2>
              <p>实时同步你的使用状态</p>
            </div>
          </div>
          <div class="pv-stat-grid">
            <div v-for="card in summaryCards" :key="card.label" class="pv-stat-cell">
              <div class="pv-stat-num">{{ card.value }}</div>
              <div class="pv-stat-lbl">{{ card.label }}</div>
              <div class="pv-stat-hint">{{ card.detail }}</div>
            </div>
          </div>

          <!-- Mini 7-day activity chart -->
          <div class="pv-section-divider"></div>
          <div class="pv-section-title">近 7 日提问趋势</div>
          <div class="pv-mini-chart">
            <div v-for="d in weeklyActivity" :key="d.label" class="pv-bar-col">
              <div class="pv-bar-track"><div class="pv-bar-fill" :style="{ height: `${Math.max(d.pct, 4)}%` }"></div></div>
              <div class="pv-bar-label">{{ d.label }}</div>
              <div class="pv-bar-count">{{ d.count }}</div>
            </div>
          </div>

          <!-- Top companies tag cloud -->
          <div v-if="topCompanies.length" class="pv-top-companies">
            <div class="pv-section-title">高频查询公司</div>
            <div class="pv-tag-cloud">
              <span v-for="c in topCompanies" :key="c.name" class="pv-company-tag">{{ c.name }} <em>{{ c.count }}</em></span>
            </div>
          </div>

          <div class="pv-section-divider"></div>
          <div class="pv-section-title">最近使用记录</div>
          <div v-if="recentUsage.length" class="pv-usage-list">
            <div v-for="item in recentUsage" :key="item.id" class="pv-usage-item">
              <div class="pv-usage-top">
                <span class="pv-usage-tag">{{ item.rankLabel }}</span>
                <span class="pv-usage-turns">{{ item.userTurns }} 轮</span>
              </div>
              <div class="pv-usage-title">{{ item.title }}</div>
              <div class="pv-usage-q">{{ item.lastQuestion }}</div>
            </div>
          </div>
          <div v-else class="pv-empty">还没有使用记录，去智能对话里提问后这里会自动更新</div>
        </section>

        <!-- ── Card: 快速导航 & 系统能力 ── -->
        <section class="pv-card">
          <div class="pv-card-head">
            <div class="pv-card-icon pv-card-icon--amber">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z"/></svg>
            </div>
            <div>
              <h2>快速导航 & 系统能力</h2>
              <p>跳转常用模块，了解系统功能概览</p>
            </div>
          </div>
          <div class="pv-icon-grid pv-icon-grid--2col">
            <RouterLink to="/assistant" class="pv-icon-item">
              <div class="pv-icon-circle pv-tone--blue"><svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z"/></svg></div>
              <div class="pv-icon-label">智能对话</div>
            </RouterLink>
            <RouterLink to="/database" class="pv-icon-item">
              <div class="pv-icon-circle pv-tone--amber"><svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125"/></svg></div>
              <div class="pv-icon-label">数据库</div>
            </RouterLink>
            <RouterLink to="/knowledge" class="pv-icon-item">
              <div class="pv-icon-circle pv-tone--teal"><svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25"/></svg></div>
              <div class="pv-icon-label">知识库</div>
            </RouterLink>
            <RouterLink to="/settings" class="pv-icon-item">
              <div class="pv-icon-circle pv-tone--gray"><svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg></div>
              <div class="pv-icon-label">系统设置</div>
            </RouterLink>
          </div>

          <div class="pv-section-divider"></div>
          <div class="pv-section-title">当前系统能力</div>
          <div class="pv-capability-list">
            <div class="pv-cap-item">
              <div class="pv-cap-dot pv-cap-dot--blue"></div>
              <div class="pv-cap-info">
                <div class="pv-cap-name">多意图拆解</div>
                <div class="pv-cap-desc">{{ form.enable_multi_intent ? '已启用 — 复合问题自动并行处理' : '已关闭 — 仅处理单个意图' }}</div>
              </div>
            </div>
            <div class="pv-cap-item">
              <div class="pv-cap-dot pv-cap-dot--emerald"></div>
              <div class="pv-cap-info">
                <div class="pv-cap-name">自动生成图表</div>
                <div class="pv-cap-desc">{{ form.default_auto_chart ? '已启用 — 查询结果默认可视化' : '已关闭 — 需手动触发图表' }}</div>
              </div>
            </div>
            <div class="pv-cap-item">
              <div class="pv-cap-dot pv-cap-dot--violet"></div>
              <div class="pv-cap-info">
                <div class="pv-cap-name">上下文继承</div>
                <div class="pv-cap-desc">{{ form.default_enable_context ? '已启用 — 追问时保留历史语境' : '已关闭 — 每轮独立问答' }}</div>
              </div>
            </div>
            <div class="pv-cap-item">
              <div class="pv-cap-dot pv-cap-dot--amber"></div>
              <div class="pv-cap-info">
                <div class="pv-cap-name">知识库召回</div>
                <div class="pv-cap-desc">每次检索返回 Top {{ form.kb_top_k }} 条相关片段</div>
              </div>
            </div>
          </div>

          <div class="pv-section-divider"></div>
          <div class="pv-section-title">运行环境</div>
          <div class="pv-env-grid">
            <div class="pv-env-row">
              <span class="pv-env-label">LLM 模型</span>
              <span class="pv-env-value">{{ loaded?.llm?.model || '—' }}</span>
            </div>
            <div class="pv-env-row">
              <span class="pv-env-label">Embedding</span>
              <span class="pv-env-value">{{ loaded?.embedding?.model || '—' }}</span>
            </div>
            <div class="pv-env-row">
              <span class="pv-env-label">数据库路径</span>
              <span class="pv-env-value">{{ loaded?.database?.path || '—' }}</span>
            </div>
            <div class="pv-env-row">
              <span class="pv-env-label">后端状态</span>
              <span :class="['pv-env-value', { 'pv-env-online': systemInfo.backendOk }]">{{ systemInfo.backend }}</span>
            </div>
          </div>
        </section>
      </div>

      <!-- ══ Two-Column Grid: Row 3 ══ -->
      <div class="pv-columns">

        <!-- ── Card 6: 常用问题 & 数据管理 ── -->
        <section class="pv-card">
          <div class="pv-card-head">
            <div class="pv-card-icon pv-card-icon--emerald">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z"/></svg>
            </div>
            <div>
              <h2>常用问题 & 数据管理</h2>
              <p>收藏问题和导出/清理数据</p>
            </div>
          </div>
          <div class="pv-section-title">常用问题</div>
          <div class="pv-fav-editor">
            <el-input v-model="favoritePromptDraft" placeholder="例如：最近一个完整年度毛利率前十的企业有哪些？" @keydown.enter.prevent="addFavoritePrompt" />
            <button class="pv-add-btn" @click="addFavoritePrompt">
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/></svg>
              添加
            </button>
          </div>
          <div v-if="profile.favoritePrompts.length" class="pv-fav-list">
            <div v-for="(prompt, idx) in profile.favoritePrompts" :key="`${prompt}-${idx}`" class="pv-fav-item">
              <div class="pv-fav-text">{{ prompt }}</div>
              <div class="pv-fav-actions">
                <button class="pv-mini" @click="copyFavoritePrompt(prompt)">复制</button>
                <button class="pv-mini pv-mini--danger" @click="removeFavoritePrompt(idx)">删除</button>
              </div>
            </div>
          </div>
          <div v-else class="pv-empty">还没有添加常用问题，先积累 1~2 条高频问题会更高效</div>

          <div class="pv-section-divider"></div>
          <div class="pv-section-title">数据管理</div>
          <div class="pv-data-actions">
            <button class="pv-btn pv-btn--ghost" @click="exportAllConversations">
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
              导出所有对话
            </button>
            <button class="pv-btn pv-btn--ghost" @click="exportSettings">
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 8.25H7.5a2.25 2.25 0 00-2.25 2.25v9a2.25 2.25 0 002.25 2.25h9a2.25 2.25 0 002.25-2.25v-9a2.25 2.25 0 00-2.25-2.25H15M9 12l3 3m0 0l3-3m-3 3V2.25"/></svg>
              导出设置
            </button>
            <button class="pv-btn pv-btn--danger-ghost" @click="clearAllConversations">
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"/></svg>
              清空会话
            </button>
            <button class="pv-btn pv-btn--danger-ghost" @click="clearLocalCache">
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182"/></svg>
              清除本地缓存
            </button>
          </div>
        </section>

        <!-- ── Card 7: 快捷键速查 & 反馈 ── -->
        <section class="pv-card">
          <div class="pv-card-head">
            <div class="pv-card-icon pv-card-icon--sky">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M6.75 7.5l3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0021 18V6a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 6v12a2.25 2.25 0 002.25 2.25z"/></svg>
            </div>
            <div>
              <h2>快捷键速查</h2>
              <p>提升操作效率的键盘快捷方式</p>
            </div>
          </div>
          <div class="pv-shortcut-grid">
            <div v-for="s in shortcuts" :key="s.keys" class="pv-shortcut-row">
              <kbd class="pv-kbd">{{ s.keys }}</kbd>
              <span class="pv-shortcut-desc">{{ s.desc }}</span>
            </div>
          </div>

          <div class="pv-section-divider"></div>
          <div class="pv-card-head" style="margin-bottom:12px;padding-bottom:0;border:none;">
            <div class="pv-card-icon pv-card-icon--amber">
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.076-4.076a1.526 1.526 0 011.037-.443 48.282 48.282 0 005.68-.494c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z"/></svg>
            </div>
            <div>
              <h2>意见反馈</h2>
              <p>告诉我们你的想法或遇到的问题</p>
            </div>
          </div>
          <div v-if="feedbackSent" class="pv-feedback-ok">感谢你的反馈！我们会认真查看。</div>
          <div v-else class="pv-feedback-form">
            <el-input v-model="feedbackText" type="textarea" :rows="3" placeholder="描述你遇到的问题或改进建议…" maxlength="500" show-word-limit />
            <button class="pv-btn pv-btn--primary" style="margin-top:8px;align-self:flex-end" @click="submitFeedback" :disabled="!feedbackText.trim()">提交反馈</button>
          </div>
        </section>
      </div>

      <!-- ══ Footer: System Info ══ -->
      <section class="pv-footer-info">
        <div class="pv-info-item">
          <span class="pv-info-label">系统版本</span>
          <span class="pv-info-value">v{{ systemInfo.version }}</span>
        </div>
        <div class="pv-info-sep"></div>
        <div class="pv-info-item">
          <span class="pv-info-label">后端状态</span>
          <span :class="['pv-info-value', 'pv-status-dot', { online: systemInfo.backendOk }]">{{ systemInfo.backend }}</span>
        </div>
        <div class="pv-info-sep"></div>
        <div class="pv-info-item">
          <span class="pv-info-label">LLM 模型</span>
          <span class="pv-info-value pv-info-mono">{{ systemInfo.model }}</span>
        </div>
        <div class="pv-info-sep"></div>
        <div class="pv-info-item">
          <span class="pv-info-label">数据库</span>
          <span class="pv-info-value pv-info-mono">{{ systemInfo.dbPath }}</span>
        </div>
      </section>

    </div><!-- /pv-body -->
  </div>
</template>

<style scoped>
/* ─── Design Tokens ─── */
.pv {
  --pv-font: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  --pv-mono: 'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, monospace;
  --pv-bg: #f5f5f7;
  --pv-card: #ffffff;
  --pv-border: #e5e5ea;
  --pv-border-light: #f0f0f3;
  --pv-text-1: #1d1d1f;
  --pv-text-2: #52525b;
  --pv-text-3: #a1a1aa;
  --pv-accent: #2563eb;
  --pv-accent-hover: #1d4ed8;
  --pv-accent-bg: #eff6ff;
  --pv-green: #059669;
  --pv-green-bg: #ecfdf5;
  --pv-amber: #d97706;
  --pv-amber-bg: #fffbeb;
  --pv-red: #dc2626;
  --pv-violet: #7c3aed;
  --pv-violet-bg: #f5f3ff;
  --pv-teal: #0d9488;
  --pv-teal-bg: #f0fdfa;
  --pv-sky: #0ea5e9;
  --pv-sky-bg: #f0f9ff;
  --pv-emerald: #059669;
  --pv-emerald-bg: #ecfdf5;
  --pv-radius: 14px;
  --pv-radius-sm: 8px;
  --pv-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03);
  flex: 1; min-width: 0; overflow-y: auto; padding: 0;
  display: flex; flex-direction: column;
  background: var(--pv-bg); font-family: var(--pv-font); color: var(--pv-text-2);
}

/* ─── Header ─── */
.pv-header { flex-shrink:0; display:flex; align-items:center; justify-content:space-between; padding:18px 36px; background:var(--pv-card); border-bottom:1px solid var(--pv-border); }
.pv-header-title { display:flex; align-items:center; gap:10px; font-size:15px; font-weight:800; color:var(--pv-text-1); letter-spacing:-0.01em; }
.pv-header-title svg { color:var(--pv-accent); }
.pv-header-actions { display:flex; gap:8px; }

/* ─── Body ─── */
.pv-body { flex:1; overflow-y:auto; padding:28px 36px 24px; display:flex; flex-direction:column; gap:16px; }
.pv-columns { display:grid; grid-template-columns:1fr 1fr; gap:16px; }

/* ─── Card ─── */
.pv-card { background:var(--pv-card); border:1px solid var(--pv-border); border-radius:var(--pv-radius); padding:24px; box-shadow:var(--pv-shadow); animation:pv-fadeUp 0.45s ease both; }
.pv-card-head { display:flex; align-items:flex-start; gap:14px; margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid var(--pv-border-light); }
.pv-card-icon { width:36px; height:36px; border-radius:10px; display:grid; place-items:center; flex-shrink:0; background:var(--pv-accent-bg); color:var(--pv-accent); }
.pv-card-icon--blue { background:var(--pv-accent-bg); color:var(--pv-accent); }
.pv-card-icon--violet { background:var(--pv-violet-bg); color:var(--pv-violet); }
.pv-card-icon--teal { background:var(--pv-teal-bg); color:var(--pv-teal); }
.pv-card-icon--amber { background:var(--pv-amber-bg); color:var(--pv-amber); }
.pv-card-icon--sky { background:var(--pv-sky-bg); color:var(--pv-sky); }
.pv-card-icon--emerald { background:var(--pv-emerald-bg); color:var(--pv-emerald); }
.pv-card-head h2 { margin:0; font-size:15px; font-weight:800; color:var(--pv-text-1); letter-spacing:-0.01em; }
.pv-card-head p { margin:3px 0 0; font-size:13px; color:var(--pv-text-3); line-height:1.4; }

/* ─── Profile Banner & Avatar Enhancement ─── */
.pv-card--profile { position:relative; }
.pv-profile-banner { display:none; }
.pv-card--profile > .pv-card-head { background:linear-gradient(135deg, #1e40af 0%, #3b82f6 40%, #7c3aed 100%); margin:-24px -24px 20px -24px; padding:20px 24px; border-radius:var(--pv-radius) var(--pv-radius) 0 0; border-bottom:none; }
.pv-card--profile .pv-card-head h2 { color:#fff; }
.pv-card--profile .pv-card-head p { color:rgba(255,255,255,0.7); }
.pv-card--profile .pv-card-icon { background:rgba(255,255,255,0.2); color:#fff; }

.pv-preview-avatar-wrap { position:relative; cursor:pointer; }
.pv-preview-avatar-edit { position:absolute; inset:0; border-radius:20px; display:grid; place-items:center; background:rgba(0,0,0,0.45); color:#fff; font-size:12px; font-weight:700; opacity:0; transition:opacity 0.2s; }
.pv-preview-avatar-wrap:hover .pv-preview-avatar-edit { opacity:1; }

/* ─── Identity (Profile Card) ─── */
.pv-identity { display:grid; grid-template-columns:220px 1fr; gap:24px; }
.pv-preview { padding:24px 20px; border-radius:var(--pv-radius); background:#fafafa; border:1px solid var(--pv-border-light); display:flex; flex-direction:column; align-items:center; text-align:center; }
.pv-preview-avatar { width:64px; height:64px; border-radius:20px; display:grid; place-items:center; background:linear-gradient(135deg,#1e40af,#3b82f6); color:#fff; font-size:30px; box-shadow:0 8px 20px rgba(37,99,235,0.2); }
.pv-preview-name { margin-top:14px; font-size:18px; font-weight:800; color:var(--pv-text-1); }
.pv-preview-role { margin-top:6px; display:inline-flex; padding:3px 10px; border-radius:6px; background:var(--pv-accent-bg); color:var(--pv-accent); font-size:11px; font-weight:700; }
.pv-preview-sig { margin-top:12px; font-size:12px; line-height:1.7; color:var(--pv-text-3); }
.pv-editor { display:flex; flex-direction:column; gap:16px; }
.pv-form-2col { display:grid; grid-template-columns:1fr 1fr; gap:4px 20px; }
.pv-span-full { grid-column:1/-1; }

/* ─── Avatar Picker ─── */
.pv-avatar-picker { padding:14px 16px; border-radius:var(--pv-radius-sm); background:#fafafa; border:1px dashed var(--pv-border); }
.pv-picker-label { font-size:12px; font-weight:700; color:var(--pv-text-3); text-transform:uppercase; letter-spacing:0.04em; }
.pv-avatars { margin-top:10px; display:flex; flex-wrap:wrap; gap:8px; }
.pv-ava-btn { width:42px; height:42px; border-radius:12px; border:2px solid transparent; background:var(--pv-card); font-size:20px; cursor:pointer; transition:all 0.2s; box-shadow:0 1px 3px rgba(0,0,0,0.06); }
.pv-ava-btn:hover { border-color:#c4c4cc; transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,0.08); }
.pv-ava-btn.active { border-color:var(--pv-accent); box-shadow:0 0 0 3px rgba(37,99,235,0.12),0 4px 12px rgba(0,0,0,0.06); transform:translateY(-2px); }

/* ─── Theme Picker ─── */
.pv-theme-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.pv-theme-card { cursor:pointer; border:2px solid var(--pv-border-light); border-radius:var(--pv-radius-sm); padding:12px; text-align:center; transition:all 0.2s; }
.pv-theme-card:hover { border-color:#c4c4cc; }
.pv-theme-card.active { border-color:var(--pv-accent); box-shadow:0 0 0 3px rgba(37,99,235,0.1); }
.pv-theme-preview { height:48px; border-radius:6px; display:flex; align-items:center; gap:8px; padding:0 12px; margin-bottom:8px; }
.pv-theme-preview--light { background:#f5f5f7; }
.pv-theme-preview--dark { background:#1d1d1f; }
.pv-theme-preview--auto { background:linear-gradient(135deg,#f5f5f7 50%,#1d1d1f 50%); }
.pv-theme-dot { width:12px; height:12px; border-radius:50%; flex-shrink:0; }
.pv-theme-preview--light .pv-theme-dot { background:#2563eb; }
.pv-theme-preview--dark .pv-theme-dot { background:#60a5fa; }
.pv-theme-preview--auto .pv-theme-dot { background:#7c3aed; }
.pv-theme-line { flex:1; height:6px; border-radius:3px; }
.pv-theme-preview--light .pv-theme-line { background:#e5e5ea; }
.pv-theme-preview--dark .pv-theme-line { background:#3f3f46; }
.pv-theme-preview--auto .pv-theme-line { background:rgba(124,58,237,0.2); }
.pv-theme-line.short { flex:0.5; }
.pv-theme-name { font-size:12px; font-weight:700; color:var(--pv-text-1); }

/* ─── Radio Button Row ─── */
.pv-radio-row { display:flex; gap:8px; flex-wrap:wrap; }
.pv-radio-btn { padding:6px 14px; border-radius:999px; border:1px solid var(--pv-border); background:var(--pv-card); color:var(--pv-text-2); font-family:var(--pv-font); font-size:12px; font-weight:600; cursor:pointer; transition:all 0.2s; }
.pv-radio-btn:hover { border-color:#c4c4cc; background:#fafafa; }
.pv-radio-btn.active { background:var(--pv-accent); color:#fff; border-color:var(--pv-accent); box-shadow:0 1px 3px rgba(37,99,235,0.25); }

/* ─── Icon Grid ─── */
.pv-icon-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
.pv-icon-item { display:flex; flex-direction:column; align-items:center; gap:6px; padding:14px 6px 10px; border-radius:var(--pv-radius); border:1px solid transparent; background:none; cursor:pointer; transition:all 0.2s; text-decoration:none; text-align:center; }
.pv-icon-item:hover { background:#fafafa; border-color:var(--pv-border-light); }
.pv-icon-item.active { background:var(--pv-accent-bg); border-color:rgba(37,99,235,0.15); }
.pv-icon-circle { width:46px; height:46px; border-radius:50%; display:grid; place-items:center; flex-shrink:0; transition:all 0.2s; }
.pv-tone--violet { background:var(--pv-violet-bg); color:var(--pv-violet); }
.pv-tone--sky { background:var(--pv-sky-bg); color:var(--pv-sky); }
.pv-tone--emerald { background:var(--pv-emerald-bg); color:var(--pv-emerald); }
.pv-tone--amber { background:var(--pv-amber-bg); color:var(--pv-amber); }
.pv-tone--blue { background:var(--pv-accent-bg); color:var(--pv-accent); }
.pv-tone--teal { background:var(--pv-teal-bg); color:var(--pv-teal); }
.pv-tone--gray { background:#f4f4f5; color:#52525b; }
.pv-icon-item.active .pv-icon-circle { box-shadow:0 0 0 3px rgba(37,99,235,0.1); }
.pv-icon-label { font-size:12px; font-weight:600; color:var(--pv-text-1); line-height:1.2; }
.pv-icon-badge { font-size:10px; font-weight:600; padding:1px 7px; border-radius:999px; background:#f4f4f5; color:var(--pv-text-3); transition:all 0.2s; }
.pv-icon-badge.on { background:var(--pv-accent-bg); color:var(--pv-accent); }
.pv-pref-form { margin-top:16px; padding-top:16px; border-top:1px solid var(--pv-border-light); }

/* ─── Stat Grid ─── */
.pv-stat-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.pv-stat-cell { padding:14px; border-radius:var(--pv-radius-sm); border:1px solid var(--pv-border-light); background:#fafafa; }
.pv-stat-num { font-size:24px; font-weight:800; color:var(--pv-text-1); letter-spacing:-0.02em; }
.pv-stat-lbl { margin-top:2px; font-size:11px; font-weight:600; color:var(--pv-text-3); }
.pv-stat-hint { margin-top:4px; font-size:11px; color:var(--pv-text-3); line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }

/* ─── Mini Chart (7-day bar) ─── */
.pv-mini-chart { display:flex; gap:6px; align-items:flex-end; padding:8px 0; }
.pv-bar-col { flex:1; display:flex; flex-direction:column; align-items:center; gap:4px; }
.pv-bar-track { width:100%; height:64px; background:var(--pv-border-light); border-radius:4px; position:relative; overflow:hidden; }
.pv-bar-fill { position:absolute; bottom:0; left:0; right:0; background:linear-gradient(180deg,#3b82f6,#2563eb); border-radius:4px; transition:height 0.4s ease; }
.pv-bar-label { font-size:10px; color:var(--pv-text-3); }
.pv-bar-count { font-size:10px; font-weight:700; color:var(--pv-text-1); }

/* ─── Top Companies Tag Cloud ─── */
.pv-top-companies { margin-top:12px; }
.pv-tag-cloud { display:flex; flex-wrap:wrap; gap:6px; }
.pv-company-tag { display:inline-flex; align-items:center; gap:4px; padding:3px 10px; border-radius:999px; background:var(--pv-accent-bg); color:var(--pv-accent); font-size:12px; font-weight:600; }
.pv-company-tag em { font-style:normal; font-size:10px; opacity:0.7; }

/* ─── 2-col icon grid variant ─── */
.pv-icon-grid--2col { grid-template-columns:repeat(2,1fr); }

/* ─── Capability List ─── */
.pv-capability-list { display:flex; flex-direction:column; gap:10px; }
.pv-cap-item { display:flex; align-items:flex-start; gap:10px; padding:10px 12px; border-radius:var(--pv-radius-sm); border:1px solid var(--pv-border-light); background:#fafafa; }
.pv-cap-dot { width:8px; height:8px; border-radius:50%; margin-top:5px; flex-shrink:0; }
.pv-cap-dot--blue { background:var(--pv-accent); }
.pv-cap-dot--emerald { background:var(--pv-emerald); }
.pv-cap-dot--violet { background:var(--pv-violet); }
.pv-cap-dot--amber { background:var(--pv-amber); }
.pv-cap-info { flex:1; min-width:0; }
.pv-cap-name { font-size:13px; font-weight:700; color:var(--pv-text-1); }
.pv-cap-desc { font-size:12px; color:var(--pv-text-3); margin-top:2px; line-height:1.4; }

/* ─── Environment Info Grid ─── */
.pv-env-grid { display:flex; flex-direction:column; gap:6px; }
.pv-env-row { display:flex; align-items:center; justify-content:space-between; padding:8px 12px; border-radius:var(--pv-radius-sm); background:#fafafa; border:1px solid var(--pv-border-light); }
.pv-env-label { font-size:12px; font-weight:600; color:var(--pv-text-3); }
.pv-env-value { font-size:12px; font-weight:700; color:var(--pv-text-1); font-family:var(--pv-mono); max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pv-env-online { color:var(--pv-green); }

/* ─── Section Divider & Title ─── */
.pv-section-divider { height:1px; background:var(--pv-border-light); margin:16px 0; }
.pv-section-title { font-size:14px; font-weight:700; color:var(--pv-text-1); margin-bottom:12px; }

/* ─── Usage List ─── */
.pv-usage-list { display:flex; flex-direction:column; gap:8px; }
.pv-usage-item { padding:12px; border-radius:var(--pv-radius-sm); border:1px solid var(--pv-border); background:#fafafa; transition:border-color 0.15s; }
.pv-usage-item:hover { border-color:#c4c4cc; }
.pv-usage-top { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.pv-usage-tag { font-size:11px; font-weight:700; color:var(--pv-accent); background:var(--pv-accent-bg); padding:2px 8px; border-radius:6px; }
.pv-usage-turns { font-size:11px; color:var(--pv-text-3); }
.pv-usage-title { margin-top:6px; font-size:13px; font-weight:700; color:var(--pv-text-1); line-height:1.4; }
.pv-usage-q { margin-top:3px; font-size:12px; line-height:1.5; color:var(--pv-text-3); display:-webkit-box; -webkit-line-clamp:1; line-clamp:1; -webkit-box-orient:vertical; overflow:hidden; }

/* ─── Favorite Prompts ─── */
.pv-fav-editor { display:flex; gap:10px; align-items:center; }
.pv-add-btn { flex-shrink:0; display:inline-flex; align-items:center; gap:4px; height:36px; padding:0 14px; border-radius:var(--pv-radius-sm); border:none; background:var(--pv-accent); color:#fff; font-family:var(--pv-font); font-size:13px; font-weight:700; cursor:pointer; transition:all 0.2s; }
.pv-add-btn:hover { background:var(--pv-accent-hover); transform:translateY(-1px); }
.pv-add-btn svg { flex-shrink:0; }
.pv-fav-list { margin-top:10px; display:flex; flex-direction:column; gap:6px; }
.pv-fav-item { display:flex; justify-content:space-between; align-items:center; gap:10px; padding:10px 12px; border-radius:var(--pv-radius-sm); border:1px solid var(--pv-border); background:#fafafa; transition:border-color 0.15s; }
.pv-fav-item:hover { border-color:#c4c4cc; }
.pv-fav-text { flex:1; min-width:0; font-size:13px; line-height:1.5; color:var(--pv-text-1); }
.pv-fav-actions { display:flex; gap:5px; flex-shrink:0; }
.pv-mini { padding:4px 9px; border-radius:6px; border:1px solid var(--pv-border); background:var(--pv-card); color:var(--pv-text-2); font-family:var(--pv-font); font-size:12px; font-weight:600; cursor:pointer; transition:all 0.15s; }
.pv-mini:hover { border-color:#c4c4cc; background:#fafafa; }
.pv-mini--danger { color:var(--pv-red); border-color:rgba(220,38,38,0.15); }
.pv-mini--danger:hover { background:rgba(220,38,38,0.04); border-color:rgba(220,38,38,0.25); }
.pv-empty { padding:16px; border-radius:var(--pv-radius-sm); background:#fafafa; border:1px dashed var(--pv-border); color:var(--pv-text-3); font-size:13px; line-height:1.7; text-align:center; }

/* ─── Data Management Actions ─── */
.pv-data-actions { display:flex; flex-wrap:wrap; gap:8px; }

/* ─── Keyboard Shortcuts ─── */
.pv-shortcut-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
.pv-shortcut-row { display:flex; align-items:center; gap:10px; padding:6px 0; }
.pv-kbd { display:inline-block; padding:3px 8px; border-radius:5px; background:#f4f4f5; border:1px solid var(--pv-border); font-family:var(--pv-mono); font-size:11px; font-weight:600; color:var(--pv-text-1); white-space:nowrap; min-width:80px; text-align:center; }
.pv-shortcut-desc { font-size:12px; color:var(--pv-text-2); }

/* ─── Feedback ─── */
.pv-feedback-form { display:flex; flex-direction:column; }
.pv-feedback-ok { padding:16px; border-radius:var(--pv-radius-sm); background:var(--pv-green-bg); color:var(--pv-green); font-size:13px; font-weight:600; text-align:center; }

/* ─── Footer System Info ─── */
.pv-footer-info { display:flex; align-items:center; justify-content:center; flex-wrap:wrap; gap:12px; padding:16px 24px; border-radius:var(--pv-radius); background:var(--pv-card); border:1px solid var(--pv-border); animation:pv-fadeUp 0.45s ease both; }
.pv-info-item { display:flex; align-items:center; gap:6px; }
.pv-info-label { font-size:11px; font-weight:600; color:var(--pv-text-3); }
.pv-info-value { font-size:12px; font-weight:700; color:var(--pv-text-1); }
.pv-info-mono { font-family:var(--pv-mono); font-size:11px; max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pv-info-sep { width:1px; height:16px; background:var(--pv-border); }
.pv-status-dot::before { content:''; display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:4px; background:var(--pv-red); }
.pv-status-dot.online::before { background:var(--pv-green); }

/* ─── Form Overrides ─── */
.pv :deep(.el-form-item__label) { font-family:var(--pv-font); font-size:13px; font-weight:600; color:var(--pv-text-2); padding-bottom:4px; }
.pv :deep(.el-input__wrapper) { border-radius:var(--pv-radius-sm); box-shadow:0 0 0 1px var(--pv-border) !important; padding:2px 12px; transition:box-shadow 0.2s; }
.pv :deep(.el-input__wrapper:hover) { box-shadow:0 0 0 1px #c4c4cc !important; }
.pv :deep(.el-input__wrapper.is-focus) { box-shadow:0 0 0 2px var(--pv-accent) !important; }
.pv :deep(.el-input__inner) { font-family:var(--pv-font); font-size:14px; color:var(--pv-text-1); }
.pv :deep(.el-textarea__inner) { font-family:var(--pv-font); font-size:14px; color:var(--pv-text-1); border-radius:var(--pv-radius-sm); }
.pv :deep(.el-input-number) { width:100%; }
.pv :deep(.el-switch.is-checked .el-switch__core) { background-color:var(--pv-accent); border-color:var(--pv-accent); }
.pv :deep(.el-select) { width:100%; }
.pv-hint { margin-top:4px; font-size:12px; color:var(--pv-text-3); line-height:1.5; }

/* ─── Buttons ─── */
.pv-btn { display:inline-flex; align-items:center; gap:5px; padding:7px 16px; border-radius:var(--pv-radius-sm); font-family:var(--pv-font); font-size:12px; font-weight:700; cursor:pointer; border:1px solid transparent; transition:all 0.2s; }
.pv-btn:disabled { opacity:0.55; cursor:default; pointer-events:none; }
.pv-btn svg { flex-shrink:0; }
.pv-btn--ghost { background:var(--pv-card); color:var(--pv-text-2); border-color:var(--pv-border); box-shadow:var(--pv-shadow); }
.pv-btn--ghost:hover:not(:disabled) { border-color:#c4c4cc; background:#fafafa; }
.pv-btn--primary { background:var(--pv-accent); color:#fff; box-shadow:0 1px 3px rgba(37,99,235,0.25); }
.pv-btn--primary:hover:not(:disabled) { background:var(--pv-accent-hover); box-shadow:0 2px 6px rgba(37,99,235,0.35); transform:translateY(-1px); }
.pv-btn--danger-ghost { background:var(--pv-card); color:var(--pv-red); border-color:rgba(220,38,38,0.2); }
.pv-btn--danger-ghost:hover:not(:disabled) { background:rgba(220,38,38,0.04); border-color:rgba(220,38,38,0.35); }

/* ─── Animations ─── */
@keyframes pv-fadeUp { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }

/* ─── Responsive ─── */
@media (max-width:1100px) {
  .pv-body { padding:24px; }
  .pv-columns { grid-template-columns:1fr; }
  .pv-identity { grid-template-columns:1fr; }
  .pv-icon-grid { grid-template-columns:repeat(4,1fr); }
  .pv-stat-grid { grid-template-columns:repeat(2,1fr); }
  .pv-shortcut-grid { grid-template-columns:1fr; }
}
@media (max-width:700px) {
  .pv-body { padding:16px; }
  .pv-header { padding:14px 16px; }
  .pv-icon-grid { grid-template-columns:repeat(2,1fr); }
  .pv-stat-grid { grid-template-columns:1fr; }
  .pv-form-2col { grid-template-columns:1fr; }
  .pv-span-full { grid-column:1; }
  .pv-fav-editor { flex-direction:column; align-items:stretch; }
  .pv-fav-item { flex-direction:column; align-items:stretch; }
  .pv-theme-grid { grid-template-columns:1fr; }
  .pv-data-actions { flex-direction:column; }
  .pv-footer-info { flex-direction:column; gap:8px; }
  .pv-info-sep { width:100%; height:1px; }
}
</style>
