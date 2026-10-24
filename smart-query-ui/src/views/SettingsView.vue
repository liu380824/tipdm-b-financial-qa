<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchSystemSettings, saveSystemSettings } from '../services/api'
import type { SystemSettings, SystemSettingsPayload, KnowledgeBaseSettings } from '../types'

const loading = ref(false)
const saving = ref(false)
const loaded = ref<SystemSettings | null>(null)
const clearApiKey = ref(false)
const clearExtractApiKey = ref(false)
const clearIntentApiKey = ref(false)
const clearEmbeddingApiKey = ref(false)

const activeTab = ref<'llm' | 'embed' | 'hyper' | 'env'>('llm')
const hyperTab = ref<'rag' | 'llm_call' | 'agent' | 'sql' | 'chart' | 'embed_hyper'>('rag')

const kb = reactive<KnowledgeBaseSettings>({
  index_concurrency: 4,
  embed_batch_size: 64,
  embed_max_chars: 2000,
  child_chunk_min: 100,
  child_chunk_max: 300,
  parent_chunk_max: 900,
  chunk_overlap: 50,
  ocr_dpi: 200,
  ocr_workers: 4,
  bm25_weight: 0.3,
  vector_weight: 0.7,
  rrf_k: 60,
  rerank_candidates: 30,
  top_k: 5,
})

const form = reactive<SystemSettingsPayload>({
  database: { path: '' },
  reports_dir: '',
  research_reports_dir: '',
  parallel: { max_workers: 4 },
  assistant: {
    enable_multi_intent: true,
    kb_top_k: 3,
    default_auto_chart: true,
    default_enable_context: true
  },
  llm: {
    base_url: '',
    model: '',
    api_key: '',
    clear_api_key: false,
    request_timeout: 120,
    intent_temperature: 0.1,
    query_plan_temperature: 0.1,
    analysis_temperature: 0.3,
    general_reply_temperature: 0.4,
    planner_decompose_temperature: 0.1,
    synthesis_temperature: 0.4,
    ref_extract_temperature: 0.1
  },
  extract_llm: {
    base_url: '',
    model: '',
    api_key: '',
    clear_api_key: false,
    request_timeout: 120
  },
  intent_llm: {
    base_url: '',
    model: '',
    api_key: '',
    clear_api_key: false,
    request_timeout: 30
  },
  embedding: {
    base_url: '',
    model: '',
    reranker_model: '',
    api_key: '',
    clear_api_key: false
  }
})

const applySettings = (settings: SystemSettings) => {
  loaded.value = settings
  form.database.path = settings.database.path
  form.reports_dir = settings.reports_dir
  form.research_reports_dir = settings.research_reports_dir
  form.parallel.max_workers = settings.parallel.max_workers
  form.assistant.enable_multi_intent = settings.assistant.enable_multi_intent
  form.assistant.kb_top_k = settings.assistant.kb_top_k
  form.assistant.default_auto_chart = settings.assistant.default_auto_chart
  form.assistant.default_enable_context = settings.assistant.default_enable_context
  form.llm.base_url = settings.llm.base_url
  form.llm.model = settings.llm.model
  form.llm.api_key = ''
  form.llm.clear_api_key = false
  form.llm.request_timeout = settings.llm.request_timeout
  form.llm.intent_temperature = settings.llm.intent_temperature
  form.llm.query_plan_temperature = settings.llm.query_plan_temperature
  form.llm.analysis_temperature = settings.llm.analysis_temperature
  form.llm.general_reply_temperature = settings.llm.general_reply_temperature
  form.llm.planner_decompose_temperature = settings.llm.planner_decompose_temperature
  form.llm.synthesis_temperature = settings.llm.synthesis_temperature
  form.llm.ref_extract_temperature = settings.llm.ref_extract_temperature
  form.extract_llm.base_url = settings.extract_llm.base_url
  form.extract_llm.model = settings.extract_llm.model
  form.extract_llm.api_key = ''
  form.extract_llm.clear_api_key = false
  form.extract_llm.request_timeout = settings.extract_llm.request_timeout
  form.intent_llm.base_url = settings.intent_llm.base_url
  form.intent_llm.model = settings.intent_llm.model
  form.intent_llm.api_key = ''
  form.intent_llm.clear_api_key = false
  form.intent_llm.request_timeout = settings.intent_llm.request_timeout
  form.embedding.base_url = settings.embedding.base_url
  form.embedding.model = settings.embedding.model
  form.embedding.reranker_model = settings.embedding.reranker_model
  form.embedding.api_key = ''
  form.embedding.clear_api_key = false
  clearApiKey.value = false
  clearExtractApiKey.value = false
  clearIntentApiKey.value = false
  clearEmbeddingApiKey.value = false
  if (settings.knowledge_base) {
    Object.assign(kb, settings.knowledge_base)
  }
}

const loadSettings = async () => {
  loading.value = true
  try {
    const settings = await fetchSystemSettings()
    applySettings(settings)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '系统设置加载失败')
  } finally {
    loading.value = false
  }
}

const llmStatusText = computed(() => loaded.value?.llm.available ? '模型服务已就绪' : '模型服务未就绪')
const llmStatusClass = computed(() => loaded.value?.llm.available ? 'ok' : 'warn')
const apiKeyHint = computed(() => {
  if (!loaded.value?.llm.has_api_key) return '当前未配置 API Key'
  if (loaded.value.llm.api_key_masked) return `当前已配置：${loaded.value.llm.api_key_masked}`
  return '当前通过环境变量注入 API Key'
})
const extractApiKeyHint = computed(() => {
  if (!loaded.value?.extract_llm.has_api_key) return '当前未配置 API Key'
  if (loaded.value.extract_llm.api_key_masked) return `当前已配置：${loaded.value.extract_llm.api_key_masked}`
  return '当前通过环境变量注入 API Key'
})
const intentApiKeyHint = computed(() => {
  if (!loaded.value?.intent_llm.has_api_key) return '当前未配置 API Key'
  if (loaded.value.intent_llm.api_key_masked) return `当前已配置：${loaded.value.intent_llm.api_key_masked}`
  return '当前通过环境变量注入 API Key'
})
const embeddingApiKeyHint = computed(() => {
  if (!loaded.value?.embedding.has_api_key) return '当前未配置 API Key'
  if (loaded.value.embedding.api_key_masked) return `当前已配置：${loaded.value.embedding.api_key_masked}`
  return '当前通过环境变量注入 API Key'
})

const handleSave = async () => {
  saving.value = true
  try {
    const payload: SystemSettingsPayload = {
      database: { path: form.database.path.trim() },
      reports_dir: form.reports_dir.trim(),
      research_reports_dir: form.research_reports_dir.trim(),
      parallel: { max_workers: Number(form.parallel.max_workers) },
      knowledge_base: { ...kb },
      assistant: {
        enable_multi_intent: form.assistant.enable_multi_intent,
        kb_top_k: Number(form.assistant.kb_top_k),
        default_auto_chart: form.assistant.default_auto_chart,
        default_enable_context: form.assistant.default_enable_context
      },
      llm: {
        base_url: form.llm.base_url.trim(),
        model: form.llm.model.trim(),
        request_timeout: Number(form.llm.request_timeout),
        intent_temperature: Number(form.llm.intent_temperature),
        query_plan_temperature: Number(form.llm.query_plan_temperature),
        analysis_temperature: Number(form.llm.analysis_temperature),
        general_reply_temperature: Number(form.llm.general_reply_temperature),
        planner_decompose_temperature: Number(form.llm.planner_decompose_temperature),
        synthesis_temperature: Number(form.llm.synthesis_temperature),
        ref_extract_temperature: Number(form.llm.ref_extract_temperature)
      },
      extract_llm: {
        base_url: form.extract_llm.base_url.trim(),
        model: form.extract_llm.model.trim(),
        request_timeout: Number(form.extract_llm.request_timeout)
      },
      intent_llm: {
        base_url: form.intent_llm.base_url.trim(),
        model: form.intent_llm.model.trim(),
        request_timeout: Number(form.intent_llm.request_timeout)
      },
      embedding: {
        base_url: form.embedding.base_url.trim(),
        model: form.embedding.model.trim(),
        reranker_model: form.embedding.reranker_model.trim()
      }
    }
    if (form.llm.api_key?.trim()) {
      payload.llm.api_key = form.llm.api_key.trim()
    }
    if (clearApiKey.value) {
      payload.llm.clear_api_key = true
    }
    if (form.extract_llm.api_key?.trim()) {
      payload.extract_llm.api_key = form.extract_llm.api_key.trim()
    }
    if (clearExtractApiKey.value) {
      payload.extract_llm.clear_api_key = true
    }
    if (form.intent_llm.api_key?.trim()) {
      payload.intent_llm.api_key = form.intent_llm.api_key.trim()
    }
    if (clearIntentApiKey.value) {
      payload.intent_llm.clear_api_key = true
    }
    if (form.embedding.api_key?.trim()) {
      payload.embedding.api_key = form.embedding.api_key.trim()
    }
    if (clearEmbeddingApiKey.value) {
      payload.embedding.clear_api_key = true
    }
    const result = await saveSystemSettings(payload)
    applySettings(result)
    ElMessage.success('系统设置已保存')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '系统设置保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(loadSettings)
</script>

<template>
  <div class="sv" v-loading="loading">

    <!-- ══ Top-Level Tab Bar ══ -->
    <nav class="sv-tabs">
      <button :class="['sv-tab', { active: activeTab === 'llm' }]" @click="activeTab = 'llm'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23.693L5 14.5m14.8.8l1.402 5.684A1.125 1.125 0 0120.112 22H3.888a1.125 1.125 0 01-1.09-1.416L4.2 14.5"/></svg>
        大模型配置
      </button>
      <button :class="['sv-tab', { active: activeTab === 'embed' }]" @click="activeTab = 'embed'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z"/></svg>
        嵌入模型配置
      </button>
      <button :class="['sv-tab', { active: activeTab === 'hyper' }]" @click="activeTab = 'hyper'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75"/></svg>
        超参数配置
      </button>
      <button :class="['sv-tab', { active: activeTab === 'env' }]" @click="activeTab = 'env'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
        界面设置
      </button>
    </nav>

    <!-- ══ Content Card ══ -->
    <div class="sv-content">

      <!-- Action Buttons (top-right) -->
      <div class="sv-actions">
        <button class="sv-btn sv-btn--ghost" :disabled="loading || saving" @click="loadSettings">
          <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182"/></svg>
          刷新
        </button>
        <button class="sv-btn sv-btn--primary" :disabled="loading || saving" @click="handleSave">
          <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 3.75H6.912a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H15M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859"/></svg>
          {{ saving ? '保存中…' : '保存配置' }}
        </button>
      </div>

      <!-- ═══════════════════════════════════════ -->
      <!-- Tab 1: 大模型配置 -->
      <!-- ═══════════════════════════════════════ -->
      <div v-show="activeTab === 'llm'" class="sv-panel">
        <div class="sv-section-head sv-section-head--lg">
          <svg width="17" height="17" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23.693L5 14.5m14.8.8l1.402 5.684A1.125 1.125 0 0120.112 22H3.888a1.125 1.125 0 01-1.09-1.416L4.2 14.5"/></svg>
          <span>智能助手 LLM</span>
        </div>
        <el-form label-position="top" class="sv-form">
          <div class="sv-grid-2">
            <el-form-item label="模型服务地址"><el-input v-model="form.llm.base_url" placeholder="https://open.bigmodel.cn/api/paas/v4" /></el-form-item>
            <el-form-item label="模型名称"><el-input v-model="form.llm.model" placeholder="glm-4-flash" /></el-form-item>
          </div>
          <div class="sv-grid-2">
            <el-form-item label="请求超时（秒）"><el-input-number v-model="form.llm.request_timeout" :min="5" :max="600" /></el-form-item>
            <el-form-item label="API Key">
              <div class="sv-key-row">
                <el-input v-model="form.llm.api_key" type="password" show-password placeholder="留空则保持不变" />
                <button v-if="loaded?.llm.has_api_key" class="sv-clear-key" @click="clearApiKey = !clearApiKey">{{ clearApiKey ? '取消清空' : '清空密钥' }}</button>
              </div>
              <div class="sv-hint">{{ apiKeyHint }}<span v-if="clearApiKey" class="sv-danger"> (保存后将清空)</span></div>
            </el-form-item>
          </div>
        </el-form>
        <div class="sv-model-status" :class="llmStatusClass">
          <span class="sv-dot"></span>
          <span class="sv-ms-text">{{ llmStatusText }}</span>
          <code class="sv-ms-model">{{ loaded?.llm.model || '未配置' }}</code>
        </div>

        <div class="sv-section-head sv-section-head--lg">
          <svg width="17" height="17" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/></svg>
          <span>数据提取 LLM</span>
        </div>
        <el-form label-position="top" class="sv-form">
          <div class="sv-grid-2">
            <el-form-item label="模型服务地址"><el-input v-model="form.extract_llm.base_url" placeholder="https://open.bigmodel.cn/api/paas/v4" /></el-form-item>
            <el-form-item label="模型名称"><el-input v-model="form.extract_llm.model" placeholder="glm-4-flash" /></el-form-item>
          </div>
          <div class="sv-grid-2">
            <el-form-item label="请求超时（秒）"><el-input-number v-model="form.extract_llm.request_timeout" :min="5" :max="600" /></el-form-item>
            <el-form-item label="API Key">
              <div class="sv-key-row">
                <el-input v-model="form.extract_llm.api_key" type="password" show-password placeholder="留空则保持不变" />
                <button v-if="loaded?.extract_llm.has_api_key" class="sv-clear-key" @click="clearExtractApiKey = !clearExtractApiKey">{{ clearExtractApiKey ? '取消清空' : '清空密钥' }}</button>
              </div>
              <div class="sv-hint">{{ extractApiKeyHint }}<span v-if="clearExtractApiKey" class="sv-danger"> (保存后将清空)</span></div>
            </el-form-item>
          </div>
        </el-form>

        <div class="sv-section-head sv-section-head--lg">
          <svg width="17" height="17" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.076-4.076a1.526 1.526 0 011.037-.443 48.282 48.282 0 005.68-.494c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z"/></svg>
          <span>意图识别 LLM</span>
        </div>
        <el-form label-position="top" class="sv-form">
          <div class="sv-grid-2">
            <el-form-item label="模型服务地址"><el-input v-model="form.intent_llm.base_url" placeholder="https://api.siliconflow.cn/v1" /></el-form-item>
            <el-form-item label="模型名称"><el-input v-model="form.intent_llm.model" placeholder="deepseek-ai/DeepSeek-V3" /></el-form-item>
          </div>
          <div class="sv-grid-2">
            <el-form-item label="请求超时（秒）"><el-input-number v-model="form.intent_llm.request_timeout" :min="5" :max="120" /></el-form-item>
            <el-form-item label="API Key">
              <div class="sv-key-row">
                <el-input v-model="form.intent_llm.api_key" type="password" show-password placeholder="留空则保持不变" />
                <button v-if="loaded?.intent_llm.has_api_key" class="sv-clear-key" @click="clearIntentApiKey = !clearIntentApiKey">{{ clearIntentApiKey ? '取消清空' : '清空密钥' }}</button>
              </div>
              <div class="sv-hint">{{ intentApiKeyHint }}<span v-if="clearIntentApiKey" class="sv-danger"> (保存后将清空)</span></div>
            </el-form-item>
          </div>
        </el-form>
      </div>

      <!-- ═══════════════════════════════════════ -->
      <!-- Tab 2: 嵌入模型配置 -->
      <!-- ═══════════════════════════════════════ -->
      <div v-show="activeTab === 'embed'" class="sv-panel">
        <div class="sv-section-head sv-section-head--lg">
          <svg width="17" height="17" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z"/></svg>
          <span>嵌入模型 / Reranker</span>
        </div>
        <el-form label-position="top" class="sv-form">
          <div class="sv-grid-2">
            <el-form-item label="模型服务地址"><el-input v-model="form.embedding.base_url" placeholder="https://api.siliconflow.cn/v1" /></el-form-item>
            <el-form-item label="Embedding 模型"><el-input v-model="form.embedding.model" placeholder="BAAI/bge-m3" /></el-form-item>
          </div>
          <div class="sv-grid-2">
            <el-form-item label="Reranker 模型"><el-input v-model="form.embedding.reranker_model" placeholder="BAAI/bge-reranker-v2-m3" /></el-form-item>
            <el-form-item label="API Key">
              <div class="sv-key-row">
                <el-input v-model="form.embedding.api_key" type="password" show-password placeholder="留空则保持不变" />
                <button v-if="loaded?.embedding.has_api_key" class="sv-clear-key" @click="clearEmbeddingApiKey = !clearEmbeddingApiKey">{{ clearEmbeddingApiKey ? '取消清空' : '清空密钥' }}</button>
              </div>
              <div class="sv-hint">{{ embeddingApiKeyHint }}<span v-if="clearEmbeddingApiKey" class="sv-danger"> (保存后将清空)</span></div>
            </el-form-item>
          </div>
        </el-form>
      </div>

      <!-- ═══════════════════════════════════════ -->
      <!-- Tab 3: 超参数配置 (with sub-tabs) -->
      <!-- ═══════════════════════════════════════ -->
      <div v-show="activeTab === 'hyper'" class="sv-panel">
        <nav class="sv-sub-tabs">
          <button :class="['sv-sub-tab', { active: hyperTab === 'rag' }]" @click="hyperTab = 'rag'">RAG检索配置</button>
          <button :class="['sv-sub-tab', { active: hyperTab === 'llm_call' }]" @click="hyperTab = 'llm_call'">LLM调用配置</button>
          <button :class="['sv-sub-tab', { active: hyperTab === 'agent' }]" @click="hyperTab = 'agent'">Agent配置</button>
          <button :class="['sv-sub-tab', { active: hyperTab === 'sql' }]" @click="hyperTab = 'sql'">SQL生成器配置</button>
          <button :class="['sv-sub-tab', { active: hyperTab === 'chart' }]" @click="hyperTab = 'chart'">图表生成器配置</button>
          <button :class="['sv-sub-tab', { active: hyperTab === 'embed_hyper' }]" @click="hyperTab = 'embed_hyper'">嵌入与索引参数</button>
        </nav>

        <!-- Sub-tab: RAG检索配置 -->
        <div v-show="hyperTab === 'rag'" class="sv-form-area">
          <el-form label-position="top" class="sv-form">
            <div class="sv-grid-2">
              <el-form-item label="文本分块大小（子块最大字符数）">
                <el-input-number v-model="kb.child_chunk_max" :min="100" :max="2000" />
                <div class="sv-hint">子文档块的最大字符长度，默认 300</div>
              </el-form-item>
              <el-form-item label="分块重叠大小">
                <el-input-number v-model="kb.chunk_overlap" :min="0" :max="200" />
                <div class="sv-hint">相邻文本块之间的重叠字符数，默认 50</div>
              </el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="子块最小字符数">
                <el-input-number v-model="kb.child_chunk_min" :min="20" :max="500" />
                <div class="sv-hint">低于该阈值的块会被合并，默认 100</div>
              </el-form-item>
              <el-form-item label="父块最大字符数">
                <el-input-number v-model="kb.parent_chunk_max" :min="300" :max="5000" />
                <div class="sv-hint">父文档块的最大字符长度，默认 900</div>
              </el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="BM25 权重">
                <el-slider v-model="kb.bm25_weight" :min="0" :max="1" :step="0.05" show-stops />
                <div class="sv-hint">关键词检索的混合权重，默认 0.3</div>
              </el-form-item>
              <el-form-item label="向量检索权重">
                <el-slider v-model="kb.vector_weight" :min="0" :max="1" :step="0.05" show-stops />
                <div class="sv-hint">语义向量检索的混合权重，默认 0.7</div>
              </el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="RRF K 参数">
                <el-input-number v-model="kb.rrf_k" :min="1" :max="200" />
                <div class="sv-hint">Reciprocal Rank Fusion 平滑参数，默认 60</div>
              </el-form-item>
              <el-form-item label="Rerank 候选数">
                <el-input-number v-model="kb.rerank_candidates" :min="5" :max="100" />
                <div class="sv-hint">送入 Reranker 的候选文档数，默认 30</div>
              </el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="最终返回 Top K">
                <el-input-number v-model="kb.top_k" :min="1" :max="50" />
                <div class="sv-hint">Rerank 后返回的最终结果数，默认 5</div>
              </el-form-item>
            </div>
          </el-form>
        </div>

        <!-- Sub-tab: LLM调用配置 -->
        <div v-show="hyperTab === 'llm_call'" class="sv-form-area">
          <el-form label-position="top" class="sv-form">
            <div class="sv-grid-2">
              <el-form-item label="意图识别温度"><el-slider v-model="form.llm.intent_temperature" :min="0" :max="1" :step="0.1" show-stops /></el-form-item>
              <el-form-item label="查询规划温度"><el-slider v-model="form.llm.query_plan_temperature" :min="0" :max="1" :step="0.1" show-stops /></el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="分析生成温度"><el-slider v-model="form.llm.analysis_temperature" :min="0" :max="1" :step="0.1" show-stops /></el-form-item>
              <el-form-item label="通用回复温度"><el-slider v-model="form.llm.general_reply_temperature" :min="0" :max="1" :step="0.1" show-stops /></el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="多意图拆解温度"><el-slider v-model="form.llm.planner_decompose_temperature" :min="0" :max="1" :step="0.1" show-stops /></el-form-item>
              <el-form-item label="多意图合成温度"><el-slider v-model="form.llm.synthesis_temperature" :min="0" :max="1" :step="0.1" show-stops /></el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="引用提取温度"><el-slider v-model="form.llm.ref_extract_temperature" :min="0" :max="1" :step="0.1" show-stops /></el-form-item>
              <el-form-item label="请求超时（秒）"><el-input-number v-model="form.llm.request_timeout" :min="5" :max="600" /></el-form-item>
            </div>
          </el-form>
        </div>

        <!-- Sub-tab: Agent配置 -->
        <div v-show="hyperTab === 'agent'" class="sv-form-area">
          <el-form label-position="top" class="sv-form">
            <div class="sv-grid-2">
              <el-form-item label="多意图规划">
                <el-switch v-model="form.assistant.enable_multi_intent" />
                <div class="sv-hint">开启后可自动拆解复合问题为多个子任务并行执行</div>
              </el-form-item>
              <el-form-item label="知识库召回数">
                <el-input-number v-model="form.assistant.kb_top_k" :min="1" :max="20" />
                <div class="sv-hint">每次检索返回的最相关文档片段数量</div>
              </el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="默认启用上下文">
                <el-switch v-model="form.assistant.default_enable_context" />
                <div class="sv-hint">开启后对话会携带历史上下文以提升连续对话质量</div>
              </el-form-item>
              <el-form-item label="默认生成图表">
                <el-switch v-model="form.assistant.default_auto_chart" />
                <div class="sv-hint">开启后查询结果会自动尝试生成可视化图表</div>
              </el-form-item>
            </div>
          </el-form>
        </div>

        <!-- Sub-tab: SQL生成器配置 -->
        <div v-show="hyperTab === 'sql'" class="sv-form-area">
          <el-form label-position="top" class="sv-form">
            <div class="sv-grid-2">
              <el-form-item label="SQL生成温度">
                <el-slider v-model="form.llm.query_plan_temperature" :min="0" :max="1" :step="0.1" show-stops />
                <div class="sv-hint">控制 SQL 生成的创造性，低值更精确，高值更多样</div>
              </el-form-item>
              <el-form-item label="数据库路径">
                <el-input v-model="form.database.path" placeholder="financial_reports.db" />
                <div class="sv-hint">SQL 查询的目标数据库文件</div>
              </el-form-item>
            </div>
          </el-form>
        </div>

        <!-- Sub-tab: 图表生成器配置 -->
        <div v-show="hyperTab === 'chart'" class="sv-form-area">
          <el-form label-position="top" class="sv-form">
            <div class="sv-grid-2">
              <el-form-item label="默认生成图表">
                <el-switch v-model="form.assistant.default_auto_chart" />
                <div class="sv-hint">开启后查询结果自动尝试生成 ECharts 可视化</div>
              </el-form-item>
              <el-form-item label="分析生成温度">
                <el-slider v-model="form.llm.analysis_temperature" :min="0" :max="1" :step="0.1" show-stops />
                <div class="sv-hint">影响图表标题和分析摘要的生成风格</div>
              </el-form-item>
            </div>
          </el-form>
        </div>

        <!-- Sub-tab: 嵌入模型配置 -->
        <div v-show="hyperTab === 'embed_hyper'" class="sv-form-area">
          <el-form label-position="top" class="sv-form">
            <div class="sv-grid-2">
              <el-form-item label="Embedding 批量大小">
                <el-input-number v-model="kb.embed_batch_size" :min="1" :max="256" />
                <div class="sv-hint">每批次发送到 Embedding API 的文本数，默认 64</div>
              </el-form-item>
              <el-form-item label="Embedding 最大字符数">
                <el-input-number v-model="kb.embed_max_chars" :min="200" :max="8000" />
                <div class="sv-hint">超过此长度的文本会被截断，默认 2000</div>
              </el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="索引并发数">
                <el-input-number v-model="kb.index_concurrency" :min="1" :max="16" />
                <div class="sv-hint">构建向量索引时的并发线程数，默认 4</div>
              </el-form-item>
              <el-form-item label="OCR DPI">
                <el-input-number v-model="kb.ocr_dpi" :min="72" :max="600" />
                <div class="sv-hint">OCR 扫描分辨率，值越高越精确但越慢，默认 200</div>
              </el-form-item>
            </div>
            <div class="sv-grid-2">
              <el-form-item label="OCR 并发线程数">
                <el-input-number v-model="kb.ocr_workers" :min="1" :max="16" />
                <div class="sv-hint">OCR 并行处理的线程数，默认 4</div>
              </el-form-item>
            </div>
          </el-form>
        </div>
      </div>

      <!-- ═══════════════════════════════════════ -->
      <!-- Tab 4: 界面设置 -->
      <!-- ═══════════════════════════════════════ -->
      <div v-show="activeTab === 'env'" class="sv-panel">
        <div class="sv-section-head sv-section-head--lg">
          <svg width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z"/></svg>
          <span>数据目录与运行环境</span>
        </div>
        <el-form label-position="top" class="sv-form">
          <div class="sv-grid-2">
            <el-form-item label="数据库文件路径">
              <el-input v-model="form.database.path" placeholder="financial_reports.db" />
              <div class="sv-hint">保存后立即生效，无需重启</div>
            </el-form-item>
            <el-form-item label="财报提取并发线程数">
              <el-input-number v-model="form.parallel.max_workers" :min="1" :max="32" />
              <div class="sv-hint">批量导入财报 PDF 时的并行线程数</div>
            </el-form-item>
          </div>
          <div class="sv-grid-2">
            <el-form-item label="财务报告目录">
              <el-input v-model="form.reports_dir" placeholder="附件2：财务报告 目录路径" />
              <div class="sv-hint">用于批量提取财报 PDF 并导入数据库</div>
            </el-form-item>
            <el-form-item label="研报数据目录">
              <el-input v-model="form.research_reports_dir" placeholder="附件5：研报数据 目录路径" />
              <div class="sv-hint">研报 PDF 及元数据 Excel 所在目录，保存后立即生效</div>
            </el-form-item>
          </div>
        </el-form>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ─── Design Tokens ─── */
.sv {
  --sv-font: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  --sv-mono: 'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, monospace;
  --sv-bg: #f5f5f7;
  --sv-card: #ffffff;
  --sv-border: #e5e5ea;
  --sv-border-light: #f0f0f3;
  --sv-text-1: #1d1d1f;
  --sv-text-2: #52525b;
  --sv-text-3: #a1a1aa;
  --sv-accent: #2563eb;
  --sv-accent-hover: #1d4ed8;
  --sv-accent-bg: #eff6ff;
  --sv-green: #059669;
  --sv-amber: #d97706;
  --sv-red: #dc2626;
  --sv-radius: 14px;
  --sv-radius-sm: 8px;
  --sv-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03);

  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 0;
  display: flex;
  flex-direction: column;
  background: var(--sv-bg);
  font-family: var(--sv-font);
  color: var(--sv-text-2);
}

/* ═══ Top-Level Tabs ═══ */
.sv-tabs {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 0;
  padding: 0 36px;
  background: var(--sv-card);
  border-bottom: 1px solid var(--sv-border);
}
.sv-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 16px 22px;
  border: none;
  background: none;
  font-family: var(--sv-font);
  font-size: 14px;
  font-weight: 600;
  color: var(--sv-text-3);
  cursor: pointer;
  transition: color 0.2s;
  white-space: nowrap;
}
.sv-tab:hover { color: var(--sv-text-2); }
.sv-tab.active { color: var(--sv-accent); }
.sv-tab::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 22px;
  right: 22px;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: transparent;
  transition: background 0.2s;
}
.sv-tab.active::after { background: var(--sv-accent); }
.sv-tab svg {
  flex-shrink: 0;
  opacity: 0.55;
  transition: opacity 0.2s;
}
.sv-tab.active svg { opacity: 1; }

/* ═══ Content Card ═══ */
.sv-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 28px 36px 36px;
  position: relative;
}

/* ═══ Action Buttons ═══ */
.sv-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-bottom: 20px;
}

/* ═══ Panel ═══ */
.sv-panel {
  animation: sv-fadeIn 0.25s ease both;
}

/* ═══ Sub-Tabs ═══ */
.sv-sub-tabs {
  display: flex;
  align-items: center;
  gap: 0;
  margin-bottom: 28px;
  border-bottom: 1px solid var(--sv-border-light);
}
.sv-sub-tab {
  position: relative;
  padding: 10px 20px 12px;
  border: none;
  background: none;
  font-family: var(--sv-font);
  font-size: 14px;
  font-weight: 600;
  color: var(--sv-text-3);
  cursor: pointer;
  transition: color 0.2s;
  white-space: nowrap;
}
.sv-sub-tab:hover { color: var(--sv-text-2); }
.sv-sub-tab.active { color: var(--sv-accent); }
.sv-sub-tab::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 20px;
  right: 20px;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: transparent;
  transition: background 0.2s;
}
.sv-sub-tab.active::after { background: var(--sv-accent); }

/* ═══ Form Area ═══ */
.sv-form-area {
  animation: sv-fadeIn 0.2s ease both;
}

/* ═══ Section Headings (flat mode) ═══ */
.sv-section-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 28px 0 14px;
  padding-bottom: 10px;
  font-size: 13px;
  font-weight: 700;
  color: var(--sv-text-1);
  letter-spacing: 0.01em;
}
.sv-section-head:first-child { margin-top: 0; }
.sv-section-head--lg {
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 0.02em;
  margin: 32px 0 16px;
  padding-bottom: 12px;
}
.sv-section-head--lg:first-child { margin-top: 0; }
.sv-section-head--lg svg { opacity: 1; }
.sv-section-head svg {
  flex-shrink: 0;
  color: var(--sv-accent);
  opacity: 0.7;
}

/* ═══ Model Status ═══ */
.sv-model-status {
  margin-top: 20px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-radius: var(--sv-radius-sm);
  background: #fafafa;
  border: 1px solid var(--sv-border-light);
}
.sv-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--sv-text-3);
  transition: background 0.3s, box-shadow 0.3s;
}
.sv-model-status.ok .sv-dot {
  background: var(--sv-green);
  box-shadow: 0 0 0 3px rgba(5, 150, 105, 0.15);
  animation: sv-pulse 2s ease-in-out infinite;
}
.sv-model-status.warn .sv-dot {
  background: var(--sv-amber);
  box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.15);
}
.sv-ms-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--sv-text-1);
}
.sv-ms-model {
  font-family: var(--sv-mono);
  font-size: 12px;
  color: var(--sv-text-3);
  margin-left: auto;
}
@keyframes sv-pulse {
  0%, 100% { box-shadow: 0 0 0 3px rgba(5, 150, 105, 0.15); }
  50% { box-shadow: 0 0 0 6px rgba(5, 150, 105, 0.08); }
}

/* ═══ Form Overrides ═══ */
.sv-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sv-grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 28px;
}

.sv :deep(.el-form-item__label) {
  font-family: var(--sv-font);
  font-size: 13px;
  font-weight: 600;
  color: var(--sv-text-2);
  padding-bottom: 6px;
}
.sv :deep(.el-input__wrapper) {
  border-radius: var(--sv-radius-sm);
  box-shadow: 0 0 0 1px var(--sv-border) !important;
  padding: 4px 14px;
  transition: box-shadow 0.2s;
}
.sv :deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px #c4c4cc !important;
}
.sv :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 2px var(--sv-accent) !important;
}
.sv :deep(.el-input__inner) {
  font-family: var(--sv-font);
  font-size: 14px;
  color: var(--sv-text-1);
  height: 32px;
}
.sv :deep(.el-input__inner::placeholder) {
  color: #c4c4cc;
}
.sv :deep(.el-input-number) {
  width: 100%;
}
.sv :deep(.el-switch.is-checked .el-switch__core) {
  background-color: var(--sv-accent);
  border-color: var(--sv-accent);
}
.sv :deep(.el-slider__runway) {
  height: 4px;
  background: var(--sv-border);
  border-radius: 2px;
}
.sv :deep(.el-slider__bar) {
  height: 4px;
  background: var(--sv-accent);
  border-radius: 2px;
}
.sv :deep(.el-slider__button) {
  width: 16px;
  height: 16px;
  border: 2px solid var(--sv-accent);
  background: var(--sv-card);
}
.sv :deep(.el-slider__stop) {
  width: 4px;
  height: 4px;
  background: var(--sv-border);
}

.sv-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--sv-text-3);
  line-height: 1.5;
}
.sv-danger {
  color: var(--sv-red);
  font-weight: 600;
}

/* ═══ Key Row ═══ */
.sv-key-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.sv-key-row .el-input { flex: 1; }
.sv-clear-key {
  flex-shrink: 0;
  padding: 6px 14px;
  border-radius: 6px;
  border: 1px solid rgba(220, 38, 38, 0.2);
  background: rgba(220, 38, 38, 0.04);
  color: var(--sv-red);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  font-family: var(--sv-font);
}
.sv-clear-key:hover {
  background: rgba(220, 38, 38, 0.08);
  border-color: rgba(220, 38, 38, 0.3);
}

/* ═══ Buttons ═══ */
.sv-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 18px;
  border-radius: var(--sv-radius-sm);
  font-family: var(--sv-font);
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.2s;
}
.sv-btn:disabled {
  opacity: 0.55;
  cursor: default;
  pointer-events: none;
}
.sv-btn svg { flex-shrink: 0; }
.sv-btn--ghost {
  background: var(--sv-card);
  color: var(--sv-text-2);
  border-color: var(--sv-border);
  box-shadow: var(--sv-shadow);
}
.sv-btn--ghost:hover:not(:disabled) {
  border-color: #c4c4cc;
  background: #fafafa;
}
.sv-btn--primary {
  background: var(--sv-accent);
  color: #fff;
  box-shadow: 0 1px 3px rgba(37, 99, 235, 0.25);
}
.sv-btn--primary:hover:not(:disabled) {
  background: var(--sv-accent-hover);
  box-shadow: 0 2px 6px rgba(37, 99, 235, 0.35);
  transform: translateY(-1px);
}

/* ═══ Animations ═══ */
@keyframes sv-fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* ═══ Responsive ═══ */
@media (max-width: 900px) {
  .sv-tabs { padding: 0 20px; overflow-x: auto; }
  .sv-tab { padding: 14px 16px; font-size: 13px; }
  .sv-content { padding: 20px; }
  .sv-grid-2 { grid-template-columns: 1fr; gap: 4px 0; }
}
@media (max-width: 600px) {
  .sv-tabs { padding: 0 12px; }
  .sv-tab { padding: 12px 10px; font-size: 12px; gap: 4px; }
  .sv-content { padding: 16px; }
  .sv-sub-tab { padding: 8px 14px; font-size: 13px; }
  .sv-actions { flex-direction: column; }
  .sv-btn { justify-content: center; }
}
</style>
