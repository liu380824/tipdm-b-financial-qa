<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, Delete, Search, Refresh, DocumentChecked, Warning } from '@element-plus/icons-vue'
import {
  fetchKbDocuments, ingestAttachment5, reindexKb, uploadKbPdf,
  deleteKbDocument, searchKb, fetchKbDocumentChunks, getKbDocumentFileUrl,
  type KbDocument, type KbChunk, type KbChunkDetail
} from '../services/api'

const docs = ref<KbDocument[]>([])
const loading = ref(false)
const ingesting = ref(false)
const reindexing = ref(false)
const uploadLoading = ref(false)
const fileInputRef = ref<HTMLInputElement>()

const searchQuery = ref('')
const searchResults = ref<KbChunk[]>([])
const searching = ref(false)
const hasSearched = ref(false)
let docsPollTimer: number | null = null

const showChunks = ref(false)
const chunksLoading = ref(false)
const chunksDocName = ref('')
const chunksList = ref<KbChunkDetail[]>([])

const totalDocs = computed(() => docs.value.length)
const doneDocs = computed(() => docs.value.filter(d => d.status === 'done').length)
const totalChunks = computed(() => docs.value.reduce((s, d) => s + (d.total_chunks ?? 0), 0))
const indexingDocs = computed(() => docs.value.filter(d => d.status === 'indexing').length)

const statusLabel: Record<string, string> = {
  pending: '待索引',
  indexing: '索引中…',
  done: '已完成',
  error: '失败',
}
const statusType: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
  pending: 'info',
  indexing: 'warning',
  done: 'success',
  error: 'danger',
}

function clearDocsPollTimer() {
  if (docsPollTimer != null) {
    window.clearTimeout(docsPollTimer)
    docsPollTimer = null
  }
}

function scheduleDocsPoll() {
  clearDocsPollTimer()
  if (docs.value.some(doc => doc.status === 'indexing' || doc.status === 'pending')) {
    docsPollTimer = window.setTimeout(() => {
      void loadDocs()
    }, 3000)
  }
}

async function viewChunks(doc: KbDocument) {
  chunksDocName.value = doc.doc_name
  chunksList.value = []
  showChunks.value = true
  chunksLoading.value = true
  try {
    chunksList.value = await fetchKbDocumentChunks(doc.id)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载知识块失败')
  } finally {
    chunksLoading.value = false
  }
}

function openFile(doc: KbDocument) {
  window.open(getKbDocumentFileUrl(doc.id), '_blank')
}

async function loadDocs() {
  loading.value = true
  try {
    docs.value = await fetchKbDocuments()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载失败')
  } finally {
    loading.value = false
    scheduleDocsPoll()
  }
}

async function handleIngest() {
  ingesting.value = true
  try {
    const res = await ingestAttachment5()
    ElMessage.success(`已触发索引 ${res.triggered} 篇研报，后台处理中…`)
    setTimeout(loadDocs, 1500)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '索引触发失败')
  } finally {
    ingesting.value = false
  }
}

async function handleReindex() {
  try {
    await ElMessageBox.confirm('将强制重新索引所有文档，耗时较长。确定？', '重新索引', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
  } catch { return }
  reindexing.value = true
  try {
    const res = await reindexKb()
    ElMessage.success(`已触发重新索引 ${res.triggered} 篇文档`)
    setTimeout(loadDocs, 1500)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '重新索引失败')
  } finally {
    reindexing.value = false
  }
}

async function handleUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''
  uploadLoading.value = true
  try {
    await uploadKbPdf(file, '用户上传')
    ElMessage.success(`${file.name} 上传成功，后台索引中…`)
    setTimeout(loadDocs, 1500)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '上传失败')
  } finally {
    uploadLoading.value = false
  }
}

async function handleDelete(doc: KbDocument) {
  try {
    await ElMessageBox.confirm(
      `确认删除「${doc.doc_name}」及其所有索引块？`,
      '删除确认',
      { confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning' }
    )
    await deleteKbDocument(doc.id)
    ElMessage.success('已删除')
    await loadDocs()
  } catch (e) {
    if ((e as string) !== 'cancel') {
      ElMessage.error(e instanceof Error ? e.message : '删除失败')
    }
  }
}

async function handleSearch() {
  if (!searchQuery.value.trim()) return
  searching.value = true
  hasSearched.value = true
  searchResults.value = []
  try {
    searchResults.value = await searchKb(searchQuery.value, 6)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '搜索失败')
  } finally {
    searching.value = false
  }
}

function shortPath(p: string) {
  const parts = p.replace(/\\/g, '/').split('/')
  return parts.slice(-2).join('/')
}

onMounted(loadDocs)
onUnmounted(() => clearDocsPollTimer())
</script>

<template>
  <div class="kb-view">
    <header class="topbar">
      <span class="topbar-title">知识库管理</span>
      <div class="topbar-actions">
        <button class="action-btn" :disabled="ingesting" @click="handleIngest">
          <el-icon><DocumentChecked /></el-icon>
          {{ ingesting ? '索引中…' : '导入附件5研报' }}
        </button>
        <button class="action-btn action-btn--reindex" :disabled="reindexing" @click="handleReindex">
          <el-icon><Refresh /></el-icon>
          {{ reindexing ? '重建中…' : '重新索引' }}
        </button>
        <button class="action-btn action-btn--upload" :disabled="uploadLoading" @click="fileInputRef?.click()">
          <el-icon><Upload /></el-icon>
          {{ uploadLoading ? '上传中…' : '上传 PDF' }}
        </button>
        <input ref="fileInputRef" type="file" accept=".pdf" style="display:none" @change="handleUpload" />
        <button class="icon-btn" :disabled="loading" @click="loadDocs">
          <el-icon><Refresh /></el-icon>
        </button>
      </div>
    </header>

    <!-- Stats bar -->
    <div class="stats-bar">
      <div class="stat-card">
        <div class="stat-val">{{ totalDocs }}</div>
        <div class="stat-label">文档总数</div>
      </div>
      <div class="stat-card stat-card--green">
        <div class="stat-val">{{ doneDocs }}</div>
        <div class="stat-label">已索引</div>
      </div>
      <div class="stat-card stat-card--blue">
        <div class="stat-val">{{ totalChunks }}</div>
        <div class="stat-label">知识块总数</div>
      </div>
      <div class="stat-card" :class="{ 'stat-card--orange': indexingDocs > 0 }">
        <div class="stat-val">{{ indexingDocs }}</div>
        <div class="stat-label">索引中</div>
      </div>
    </div>

    <!-- Search panel -->
    <div class="search-panel">
      <div class="search-box">
        <input
          v-model="searchQuery"
          class="search-input"
          placeholder="在知识库中检索研报内容…"
          @keydown.enter="handleSearch"
        />
        <button class="search-btn" :disabled="searching || !searchQuery.trim()" @click="handleSearch">
          <el-icon><Search /></el-icon>
          {{ searching ? '检索中…' : '检索' }}
        </button>
      </div>

      <div v-if="hasSearched" class="search-results">
        <div v-if="searchResults.length === 0" class="empty-tip">
          <el-icon><Warning /></el-icon>
          未找到相关内容，请尝试其他关键词或先完成索引
        </div>
        <div v-for="(r, i) in searchResults" :key="r.chunk_id" class="result-card">
          <div class="result-header">
            <span class="result-rank">#{{ i + 1 }}</span>
            <span class="result-doc">{{ r.source_title }}</span>
            <el-tag size="small" effect="plain">{{ r.doc_type }}</el-tag>
            <span class="result-chunk">第 {{ r.chunk_idx }} 块</span>
            <span class="result-score">相关度 {{ (r.score).toFixed(3) }}</span>
          </div>
          <div class="result-text">{{ r.text }}</div>
          <div class="result-path">{{ shortPath(r.doc_path) }}</div>
        </div>
      </div>
    </div>

    <!-- Document table -->
    <div class="doc-table-wrap" v-loading="loading">
      <div v-if="docs.length === 0 && !loading" class="empty-tip">
        <el-icon><DocumentChecked /></el-icon>
        暂无文档，点击「导入附件5研报」自动扫描并索引研报
      </div>
      <table v-else class="doc-table">
        <thead>
          <tr>
            <th>序号</th>
            <th>文档名称</th>
            <th>类型</th>
            <th>知识块数</th>
            <th>索引时间</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(doc, index) in docs" :key="doc.id">
            <td class="td-id">{{ index + 1 }}</td>
            <td class="td-name" :title="doc.doc_path">
              <span class="doc-name-link" @click="openFile(doc)">{{ doc.doc_name }}</span>
            </td>
            <td><el-tag size="small" effect="plain">{{ doc.doc_type }}</el-tag></td>
            <td class="td-num">
              <span v-if="doc.total_chunks" class="chunk-link" @click="viewChunks(doc)">{{ doc.total_chunks }}</span>
              <span v-else>0</span>
            </td>
            <td class="td-date">{{ doc.indexed_at?.slice(0, 16) ?? '—' }}</td>
            <td>
              <el-tag :type="statusType[doc.status]" size="small">
                {{ statusLabel[doc.status] ?? doc.status }}
              </el-tag>
            </td>
            <td class="td-actions">
              <button class="view-btn" @click="openFile(doc)" title="查看文件">📄</button>
              <button class="view-btn" @click="viewChunks(doc)" title="查看知识块" :disabled="!doc.total_chunks">📋</button>
              <button class="del-btn" @click="handleDelete(doc)" title="删除">
                <el-icon><Delete /></el-icon>
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Chunks Dialog -->
    <el-dialog v-model="showChunks" :title="`知识块 — ${chunksDocName}`" width="800px" align-center>
      <div v-loading="chunksLoading" class="chunks-container">
        <div v-if="!chunksLoading && chunksList.length === 0" class="empty-tip">暂无知识块数据</div>
        <div v-for="chunk in chunksList" :key="chunk.id" class="chunk-card">
          <div class="chunk-header">
            <span class="chunk-idx">#{{ chunk.chunk_idx }}</span>
            <el-tag v-if="chunk.chunk_type" size="small" effect="plain">{{ chunk.chunk_type }}</el-tag>
            <span v-if="chunk.page_no" class="chunk-page">P{{ chunk.page_no }}</span>
            <span v-if="chunk.section_title" class="chunk-section">{{ chunk.section_title }}</span>
          </div>
          <div class="chunk-text">{{ chunk.text }}</div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ═══════════════════════════════════════════════
   KnowledgeView — Document Intelligence Hub
   ═══════════════════════════════════════════════ */

.kb-view {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-primary);
  overflow: hidden;
  font-family: var(--font-body);
}
.topbar {
  height: 52px;
  padding: 0 24px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-primary);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.topbar-title {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}
.topbar-actions { display: flex; gap: 8px; align-items: center; }
.action-btn {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 14px; border-radius: var(--radius-md); border: none;
  background: var(--accent); color: #fff; font-size: 13px; font-weight: 600;
  cursor: pointer; transition: all 0.15s ease;
}
.action-btn:hover:not(:disabled) { background: var(--accent-hover); }
.action-btn:disabled { opacity: 0.5; cursor: default; }
.action-btn--upload { background: var(--success); }
.action-btn--upload:hover:not(:disabled) { background: #059669; }
.action-btn--reindex { background: transparent; border: 1px solid var(--border-primary); color: var(--text-secondary); }
.action-btn--reindex:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); background: var(--accent-bg); }
.icon-btn {
  width: 32px; height: 32px; border-radius: var(--radius-md);
  border: 1px solid var(--border-primary); background: var(--bg-secondary); color: var(--text-tertiary);
  cursor: pointer; display: grid; place-items: center; transition: all 0.15s ease;
}
.icon-btn:hover { background: var(--bg-tertiary); color: var(--text-secondary); }
.icon-btn:disabled { opacity: 0.4; cursor: default; }

/* ── Stats ── */
.stats-bar {
  display: flex; gap: 12px; padding: 14px 24px;
  background: var(--bg-secondary); border-bottom: 1px solid var(--border-primary); flex-shrink: 0;
}
.stat-card {
  flex: 1; padding: 12px 16px; border-radius: var(--radius-lg);
  background: var(--bg-tertiary); border: 1px solid var(--border-secondary); text-align: center;
  transition: all 0.15s ease;
}
.stat-card--green { border-color: var(--success-border); background: var(--success-bg); }
.stat-card--blue  { border-color: var(--accent-border); background: var(--accent-bg); }
.stat-card--orange { border-color: var(--warning-border); background: var(--warning-bg); }
.stat-val {
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
}
.stat-label {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 2px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 500;
}

/* ── Search ── */
.search-panel {
  padding: 14px 24px; background: var(--bg-secondary); border-bottom: 1px solid var(--border-primary); flex-shrink: 0;
}
.search-box { display: flex; gap: 10px; }
.search-input {
  flex: 1; padding: 9px 14px; border: 1px solid var(--border-primary); border-radius: var(--radius-md);
  font-size: 13px; outline: none; transition: border-color 0.15s ease; background: var(--bg-tertiary);
  color: var(--text-primary);
}
.search-input:focus { border-color: var(--accent); background: var(--bg-secondary); }
.search-input::placeholder { color: var(--text-tertiary); }
.search-btn {
  display: flex; align-items: center; gap: 6px;
  padding: 9px 18px; border-radius: var(--radius-md); border: none;
  background: var(--accent); color: #fff; font-size: 13px; font-weight: 600;
  cursor: pointer; transition: all 0.15s ease; white-space: nowrap;
}
.search-btn:hover:not(:disabled) { background: var(--accent-hover); }
.search-btn:disabled { background: var(--bg-inset); color: var(--text-tertiary); cursor: default; }
.search-results { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }
.result-card {
  padding: 12px 14px; border-radius: var(--radius-lg);
  border: 1px solid var(--border-primary); background: var(--bg-tertiary);
  transition: all 0.15s ease;
}
.result-card:hover {
  border-color: var(--border-hover);
}
.result-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap;
}
.result-rank {
  font-family: var(--font-mono);
  font-size: 11px; font-weight: 700; color: var(--accent);
  background: var(--accent-bg); padding: 2px 8px; border-radius: var(--radius-xs);
}
.result-doc { font-size: 13px; font-weight: 600; color: var(--text-primary); flex: 1; }
.result-chunk {
  font-family: var(--font-mono);
  font-size: 10px; color: var(--text-tertiary); background: var(--bg-inset); padding: 2px 6px; border-radius: var(--radius-xs);
}
.result-score {
  font-family: var(--font-mono);
  font-size: 11px; color: var(--text-tertiary); margin-left: auto;
}
.result-text {
  font-size: 13px; line-height: 1.7; color: var(--text-secondary);
  display: -webkit-box; -webkit-line-clamp: 4; line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
}
.result-path {
  font-family: var(--font-mono);
  font-size: 10px; color: var(--text-tertiary); margin-top: 6px;
}

/* ── Document Table ── */
.doc-table-wrap {
  flex: 1; overflow-y: auto; padding: 16px 24px;
}
.empty-tip {
  display: flex; align-items: center; gap: 8px; justify-content: center;
  padding: 60px 24px; font-size: 13px; color: var(--text-tertiary);
}
.doc-table {
  width: 100%; border-collapse: collapse; background: var(--bg-secondary);
  border-radius: var(--radius-lg); overflow: hidden;
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--border-secondary);
}
.doc-table th {
  background: var(--bg-tertiary);
  font-size: 11px; font-weight: 600; color: var(--text-tertiary);
  text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border-primary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.doc-table td {
  font-size: 13px; color: var(--text-secondary); padding: 10px 14px;
  border-bottom: 1px solid var(--border-secondary); vertical-align: middle;
}
.doc-table tr:last-child td { border-bottom: none; }
.doc-table tr:hover td { background: var(--bg-tertiary); }
.td-id { width: 48px; color: var(--text-tertiary); font-family: var(--font-mono); }
.td-name { max-width: 280px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text-primary); font-weight: 500; }
.td-num { text-align: center; font-family: var(--font-mono); }
.td-duration { font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); }
.td-date { color: var(--text-tertiary); font-family: var(--font-mono); font-size: 11px; white-space: nowrap; }
.del-btn {
  width: 28px; height: 28px; border-radius: var(--radius-sm);
  border: 1px solid var(--danger-border); background: var(--danger-bg); color: var(--danger);
  cursor: pointer; display: grid; place-items: center; font-size: 12px;
  transition: all 0.12s ease;
}
.del-btn:hover { background: #fecaca; }

/* ── Clickable Links ── */
.doc-name-link {
  cursor: pointer;
  color: var(--accent, #2563eb);
  font-weight: 500;
  transition: color 0.15s;
}
.doc-name-link:hover { color: var(--accent-hover, #1d4ed8); text-decoration: underline; }
.chunk-link {
  cursor: pointer;
  color: var(--accent, #2563eb);
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 2px;
}
.chunk-link:hover { color: var(--accent-hover, #1d4ed8); }
.td-actions { display: flex; align-items: center; gap: 4px; }
.view-btn {
  width: 28px; height: 28px; border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-primary, #e5e7eb); background: var(--bg-tertiary, #f9fafb);
  cursor: pointer; display: grid; place-items: center; font-size: 14px;
  transition: all 0.12s ease;
}
.view-btn:hover:not(:disabled) { background: var(--bg-secondary, #f3f4f6); border-color: var(--accent-border, #93c5fd); }
.view-btn:disabled { opacity: 0.35; cursor: default; }

/* ── Chunks Dialog ── */
.chunks-container { max-height: 65vh; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; min-height: 100px; }
.chunk-card {
  padding: 12px 14px; border-radius: 8px;
  border: 1px solid var(--border-secondary, #e5e7eb); background: var(--bg-tertiary, #f9fafb);
}
.chunk-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap;
}
.chunk-idx {
  font-family: var(--font-mono, monospace);
  font-size: 11px; font-weight: 700; color: var(--accent, #2563eb);
  background: var(--accent-bg, #eff6ff); padding: 2px 8px; border-radius: 4px;
}
.chunk-page {
  font-family: var(--font-mono, monospace);
  font-size: 10px; color: var(--text-tertiary, #9ca3af);
  background: var(--bg-inset, #f3f4f6); padding: 2px 6px; border-radius: 4px;
}
.chunk-section { font-size: 12px; color: var(--text-secondary, #6b7280); font-weight: 500; }
.chunk-text {
  font-size: 13px; line-height: 1.7; color: var(--text-secondary, #374151);
  white-space: pre-wrap; word-break: break-all;
}
</style>
