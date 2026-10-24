<script setup lang="ts">
import { ref, onMounted, nextTick, computed, watch } from 'vue'
import type { ThinkingMapNode } from '../types'

const props = defineProps<{
  root: ThinkingMapNode
}>()

const emit = defineEmits<{
  (e: 'fullscreen'): void
}>()

// ── 布局参数 ──
const NODE_W = 220
const EXPANDED_NODE_W = 290
const NODE_H = 62
const GAP_X = 92
const GAP_Y = 18

// ── 画布缩放 / 拖拽 ──
const scale = ref(0.85)
const panX = ref(0)
const panY = ref(0)
const dragging = ref(false)
const dragStart = ref({ x: 0, y: 0 })
const canvasRef = ref<HTMLElement | null>(null)

const onWheel = (e: WheelEvent) => {
  e.preventDefault()
  const delta = e.deltaY > 0 ? -0.08 : 0.08
  scale.value = Math.max(0.25, Math.min(2.5, scale.value + delta))
}
const onPointerDown = (e: PointerEvent) => {
  if ((e.target as HTMLElement).closest('.tm-node')) return
  dragging.value = true
  dragStart.value = { x: e.clientX - panX.value, y: e.clientY - panY.value }
}
const onPointerMove = (e: PointerEvent) => {
  if (!dragging.value) return
  panX.value = e.clientX - dragStart.value.x
  panY.value = e.clientY - dragStart.value.y
}
const onPointerUp = () => { dragging.value = false }

// ── 计算布局坐标 ──
interface LayoutNode {
  node: ThinkingMapNode
  x: number
  y: number
  w: number
  h: number
  children: LayoutNode[]
}

const expandedNodeIds = ref<string[]>([])

const isExpanded = (nodeId: string) => expandedNodeIds.value.includes(nodeId)

const nodeBoxWidth = (node: ThinkingMapNode) => (isExpanded(node.id) ? EXPANDED_NODE_W : NODE_W)

const nodeBoxHeight = (node: ThinkingMapNode) => (isExpanded(node.id) ? getExpandedNodeHeight(node) : NODE_H)

function subtreeHeight(n: ThinkingMapNode): number {
  const selfH = nodeBoxHeight(n)
  if (!n.children.length) return selfH
  const childrenH = n.children.reduce((sum, c) => sum + subtreeHeight(c), 0)
    + (n.children.length - 1) * GAP_Y
  return Math.max(selfH, childrenH)
}

function layoutTree(n: ThinkingMapNode, x: number, y: number): LayoutNode {
  const selfW = nodeBoxWidth(n)
  const selfH = nodeBoxHeight(n)
  const result: LayoutNode = { node: n, x, y, w: selfW, h: selfH, children: [] }
  if (!n.children.length) return result
  const totalH = n.children.reduce((sum, c) => sum + subtreeHeight(c), 0)
    + (n.children.length - 1) * GAP_Y
  let cy = y + selfH / 2 - totalH / 2
  for (const child of n.children) {
    const ch = subtreeHeight(child)
    const childY = cy + (ch - nodeBoxHeight(child)) / 2
    result.children.push(layoutTree(child, x + selfW + GAP_X, childY))
    cy += ch + GAP_Y
  }
  return result
}

const layoutRoot = computed(() => layoutTree(props.root, 30, 300))

// ── 递归收集节点、连线、连线标签 ──
interface FlatNode { node: ThinkingMapNode; x: number; y: number; w: number; h: number }
interface Edge { x1: number; y1: number; x2: number; y2: number; label: string }

function collectNodes(ln: LayoutNode, nodes: FlatNode[], edges: Edge[]) {
  nodes.push({ node: ln.node, x: ln.x, y: ln.y, w: ln.w, h: ln.h })
  for (const child of ln.children) {
    edges.push({
      x1: ln.x + ln.w,
      y1: ln.y + ln.h / 2,
      x2: child.x,
      y2: child.y + child.h / 2,
      label: child.node.edge_label || '',
    })
    collectNodes(child, nodes, edges)
  }
}

const allNodes = computed(() => {
  const nodes: FlatNode[] = []
  const edges: Edge[] = []
  collectNodes(layoutRoot.value, nodes, edges)
  return { nodes, edges }
})

// SVG 尺寸
const svgBounds = computed(() => {
  const ns = allNodes.value.nodes
  if (!ns.length) return { x: 0, y: 0, w: 800, h: 400 }
  const minX = Math.min(...ns.map(n => n.x)) - 40
  const minY = Math.min(...ns.map(n => n.y)) - 40
  const maxX = Math.max(...ns.map(n => n.x + n.w)) + 40
  const maxY = Math.max(...ns.map(n => n.y + n.h)) + 40
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY }
})

const resetViewport = () => {
  const canvas = canvasRef.value
  if (!canvas || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) {
    scale.value = 0.85
    panX.value = 20
    panY.value = 20
    return
  }
  const padding = 36
  const bounds = svgBounds.value
  const fitScaleX = (canvas.clientWidth - padding * 2) / Math.max(bounds.w, 1)
  const fitScaleY = (canvas.clientHeight - padding * 2) / Math.max(bounds.h, 1)
  const nextScale = Math.min(1, fitScaleX, fitScaleY)
  scale.value = Math.max(0.45, Math.min(1.1, Number.isFinite(nextScale) ? nextScale : 0.85))
  panX.value = (canvas.clientWidth - bounds.w * scale.value) / 2 - bounds.x * scale.value
  panY.value = (canvas.clientHeight - bounds.h * scale.value) / 2 - bounds.y * scale.value
}

// ── 节点颜色映射（秘塔风格：用左侧色条区分） ──
const nodeColors: Record<string, { accent: string; bg: string; text: string }> = {
  question:  { accent: '#6366f1', bg: '#f8f7ff', text: '#3730a3' },
  process:   { accent: '#10b981', bg: '#f0fdf8', text: '#065f46' },
  data:      { accent: '#3b82f6', bg: '#f0f7ff', text: '#1e40af' },
  knowledge: { accent: '#f59e0b', bg: '#fffbeb', text: '#92400e' },
  source:    { accent: '#8b5cf6', bg: '#faf5ff', text: '#5b21b6' },
  synthesis: { accent: '#f97316', bg: '#fff7ed', text: '#9a3412' },
  result:    { accent: '#14b8a6', bg: '#f0fdfa', text: '#115e59' },
}
const getColor = (type: string) => nodeColors[type] || nodeColors.process

const nodeTypeLabels: Record<ThinkingMapNode['type'], string> = {
  question: '问题起点',
  process: '处理步骤',
  data: '数据节点',
  knowledge: '知识增强',
  source: '参考来源',
  synthesis: '综合推理',
  result: '结果输出',
}

const nodeRoleHints: Record<ThinkingMapNode['type'], string> = {
  question: '这是当前归因分析的起点，表示系统围绕哪个问题展开整条推理链路。',
  process: '这是一个中间处理步骤，用来展示系统如何理解问题、拆解任务或推进分析。',
  data: '这是一个数据节点，用来承载实际检索到的数值、范围或结构化结果。',
  knowledge: '这是一个知识增强节点，表示系统引入了研报或外部文本证据来补充判断。',
  source: '这是一个来源节点，表示某条具体参考材料或证据片段。',
  synthesis: '这是一个综合推理节点，表示系统在汇总多路信息后形成结论。',
  result: '这是一个结果节点，表示某条链路最终沉淀出的回答或结论。',
}

const selectNode = (node: ThinkingMapNode) => {
  expandedNodeIds.value = isExpanded(node.id)
    ? expandedNodeIds.value.filter(id => id !== node.id)
    : [...expandedNodeIds.value, node.id]
}

const getFallbackDetail = (node: ThinkingMapNode) => {
  return [
    `节点「${node.label}」当前没有单独写出的简述，因此这里展示它在整条链路中的作用说明。`,
    nodeRoleHints[node.type],
  ].filter(Boolean).join(' ')
}

const getExpandedDetail = (node: ThinkingMapNode) => {
  const detail = String(node.detail || '').trim()
  return detail || getFallbackDetail(node)
}

const estimateTextLines = (text: string, charsPerLine = 22) => {
  const normalized = text.replace(/\s+/g, '')
  return Math.max(1, Math.ceil((normalized.length || 1) / charsPerLine))
}

const estimateChildRows = (node: ThinkingMapNode) => {
  if (!node.children.length) return 2
  const visible = visibleChildren(node)
  let rows = 1
  let currentWidth = 0
  for (const child of visible) {
    const chipWidth = Math.min(child.label.length + 4, 22)
    if (currentWidth > 0 && currentWidth + chipWidth > 22) {
      rows += 1
      currentWidth = 0
    }
    currentWidth += chipWidth
  }
  return rows
}

const getExpandedNodeHeight = (node: ThinkingMapNode) => {
  const detailLines = estimateTextLines(getExpandedDetail(node), 22)
  const relationHeight = node.edge_label ? 30 : 0
  const childHeight = node.children.length ? 22 + estimateChildRows(node) * 20 : 30
  return Math.max(172, Math.min(300, 68 + detailLines * 12 + relationHeight + childHeight))
}

const visibleChildren = (node: ThinkingMapNode) => node.children.slice(0, 4)

const collectNodeIds = (node: ThinkingMapNode): string[] => {
  const ids = [node.id]
  for (const child of node.children) {
    ids.push(...collectNodeIds(child))
  }
  return ids
}

const allNodeIds = computed(() => collectNodeIds(props.root))

const allExpanded = computed(() => allNodeIds.value.length > 0 && allNodeIds.value.every(id => isExpanded(id)))

const toggleAllNodes = (expand: boolean) => {
  expandedNodeIds.value = expand ? [...allNodeIds.value] : []
  nextTick(() => resetViewport())
}

// 连线中点（用于标签定位）
const edgeMid = (e: Edge) => {
  const mx = (e.x1 + e.x2) / 2
  const my = (e.y1 + e.y2) / 2
  return { x: mx, y: my - 8 }
}

// 自动居中
onMounted(() => {
  nextTick(() => {
    if (canvasRef.value) resetViewport()
  })
})

watch(() => props.root, () => {
  expandedNodeIds.value = []
  nextTick(() => resetViewport())
}, { deep: true })

const hovered = ref<string | null>(null)
</script>

<template>
  <div class="tm-wrapper">
    <div class="tm-stage">
      <div class="tm-toolbar">
        <button class="tm-tool-btn" @click="scale = Math.min(2.5, scale + 0.15)" title="放大">+</button>
        <button class="tm-tool-btn" @click="scale = Math.max(0.25, scale - 0.15)" title="缩小">−</button>
        <button class="tm-tool-btn" @click="resetViewport" title="重置">⟲</button>
        <button class="tm-tool-btn tm-text-btn" @click="toggleAllNodes(true)" title="全部展开">全展</button>
        <button class="tm-tool-btn tm-text-btn" @click="toggleAllNodes(false)" title="全部收起">{{ allExpanded ? '全收' : '收起' }}</button>
        <button class="tm-tool-btn tm-full-btn" @click="emit('fullscreen')" title="全屏查看">⤢</button>
      </div>
      <div
        ref="canvasRef"
        class="tm-canvas"
        :class="{ dragging }"
        @wheel.prevent="onWheel"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointerleave="onPointerUp"
      >
        <div class="tm-inner" :style="{ transform: `translate(${panX}px, ${panY}px) scale(${scale})` }">
          <!-- 连线 SVG -->
          <svg class="tm-edges" :viewBox="`${svgBounds.x} ${svgBounds.y} ${svgBounds.w} ${svgBounds.h}`" :style="{ width: svgBounds.w + 'px', height: svgBounds.h + 'px', left: svgBounds.x + 'px', top: svgBounds.y + 'px' }">
            <defs>
              <marker id="tm-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6" fill="none" stroke="#94a3b8" stroke-width="1" />
              </marker>
            </defs>
            <path
              v-for="(edge, i) in allNodes.edges"
              :key="'e' + i"
              :d="`M ${edge.x1} ${edge.y1} C ${edge.x1 + GAP_X * 0.5} ${edge.y1}, ${edge.x2 - GAP_X * 0.5} ${edge.y2}, ${edge.x2} ${edge.y2}`"
              class="tm-edge-path"
            />
            <!-- 连线标签 -->
            <template v-for="(edge, i) in allNodes.edges" :key="'lbl' + i">
              <text
                v-if="edge.label"
                :x="edgeMid(edge).x"
                :y="edgeMid(edge).y"
                class="tm-edge-label"
              >{{ edge.label }}</text>
            </template>
          </svg>
          <!-- 节点 -->
          <div
            v-for="fn in allNodes.nodes"
            :key="fn.node.id"
            class="tm-node"
            :class="[
              `tm-type-${fn.node.type}`,
              { hovered: hovered === fn.node.id, active: isExpanded(fn.node.id) }
            ]"
            :style="{
              left: fn.x + 'px',
              top: fn.y + 'px',
              width: fn.w + 'px',
              height: fn.h + 'px',
              '--accent': getColor(fn.node.type).accent,
              '--node-bg': getColor(fn.node.type).bg,
              '--node-text': getColor(fn.node.type).text,
            }"
            tabindex="0"
            @mouseenter="hovered = fn.node.id"
            @mouseleave="hovered = null"
            @click.stop="selectNode(fn.node)"
            @keydown.enter.prevent="selectNode(fn.node)"
            @keydown.space.prevent="selectNode(fn.node)"
          >
            <div class="tm-node-inner">
              <div class="tm-node-header">
                <span class="tm-type-badge">■</span>
                <span class="tm-label">{{ fn.node.label }}</span>
                <span class="tm-expand-indicator">{{ isExpanded(fn.node.id) ? '−' : '+' }}</span>
              </div>
              <div class="tm-meta-row">
                <span class="tm-node-kind">{{ nodeTypeLabels[fn.node.type] }}</span>
                <span v-if="fn.node.edge_label" class="tm-node-edge">{{ fn.node.edge_label }}</span>
              </div>
              <template v-if="isExpanded(fn.node.id)">
                <div class="tm-expanded">
                  <div class="tm-expanded-section">
                    <div class="tm-expanded-title">详细说明</div>
                    <div class="tm-expanded-text">{{ getExpandedDetail(fn.node) }}</div>
                  </div>

                  <div v-if="fn.node.edge_label" class="tm-expanded-section">
                    <div class="tm-expanded-title">与上一步的关系</div>
                    <div class="tm-expanded-chip">{{ fn.node.edge_label }}</div>
                  </div>

                  <div class="tm-expanded-section">
                    <div class="tm-expanded-title">后续节点</div>
                    <div v-if="fn.node.children.length" class="tm-inline-children">
                      <span v-for="child in visibleChildren(fn.node)" :key="child.id" class="tm-inline-child">
                        {{ child.label }}
                      </span>
                    </div>
                    <div v-else class="tm-expanded-text">当前节点已经是该分支的末端，没有继续向下展开的节点。</div>
                  </div>
                </div>
              </template>
              <div v-else class="tm-detail">{{ fn.node.detail || getFallbackDetail(fn.node) }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tm-wrapper {
  position: relative; width: 100%; height: 340px;
  border: 1px solid #e2e8f0; border-radius: 14px; overflow: hidden;
  background: linear-gradient(135deg, #fafbfe 0%, #f4f6fb 100%);
}
.tm-stage {
  position: relative;
  height: 100%;
  min-width: 0;
}
.tm-toolbar {
  position: absolute; top: 8px; right: 8px; z-index: 10;
  display: flex; gap: 4px;
}
.tm-tool-btn {
  width: 28px; height: 28px; border-radius: 6px; border: 1px solid #e5e7eb;
  background: #fff; color: #374151; font-size: 14px; font-weight: 700;
  cursor: pointer; display: grid; place-items: center; transition: all 0.12s;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.tm-tool-btn:hover { background: #f3f4f6; border-color: #d1d5db; }
.tm-text-btn {
  width: auto;
  min-width: 42px;
  padding: 0 8px;
  font-size: 12px;
}
.tm-full-btn { font-size: 16px; }
.tm-canvas {
  width: 100%; height: 100%; cursor: grab; overflow: hidden;
  user-select: none;
}
.tm-canvas.dragging { cursor: grabbing; }
.tm-inner {
  position: relative; transform-origin: 0 0;
  width: 1px; height: 1px;
}
.tm-edges {
  position: absolute; pointer-events: none; overflow: visible;
}
.tm-edge-path {
  fill: none; stroke: #94a3b8; stroke-width: 1.5;
  stroke-dasharray: 6 4;
}
.tm-edge-label {
  font-size: 10px; fill: #64748b; text-anchor: middle;
  font-weight: 600;
}
/* ── 节点卡片 ── */
.tm-node {
  position: absolute;
  border-radius: 10px;
  background: var(--node-bg, #fff);
  border: 1px solid #e2e8f0;
  border-left: 3.5px solid var(--accent, #6366f1);
  box-sizing: border-box;
  cursor: pointer;
  transition: box-shadow 0.18s, transform 0.18s, border-color 0.18s;
  box-shadow: 0 1px 4px rgba(0,0,0,0.05);
}
.tm-node.hovered {
  box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  transform: scale(1.03);
  z-index: 5;
}
.tm-node.active {
  border-color: color-mix(in srgb, var(--accent, #6366f1) 45%, #cbd5e1);
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.16);
  z-index: 6;
}
.tm-node-inner {
  padding: 8px 10px 8px 10px;
  display: flex;
  flex-direction: column;
  height: 100%;
  box-sizing: border-box;
}
.tm-node-header {
  display: flex; align-items: center; gap: 6px; margin-bottom: 3px;
}
.tm-type-badge {
  color: var(--accent, #6366f1); font-size: 10px; flex-shrink: 0;
}
.tm-label {
  font-size: 12px; font-weight: 700; line-height: 1.35;
  color: var(--node-text, #1f2937);
  flex: 1;
  display: -webkit-box; line-clamp: 2; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.tm-expand-indicator {
  color: var(--accent, #6366f1);
  font-size: 14px;
  font-weight: 800;
  flex-shrink: 0;
}
.tm-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 4px;
}
.tm-node-kind,
.tm-node-edge {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 9.5px;
  font-weight: 700;
  line-height: 1.5;
}
.tm-node-kind {
  background: rgba(255,255,255,0.72);
  color: var(--node-text, #334155);
}
.tm-node-edge {
  background: rgba(15, 23, 42, 0.06);
  color: #64748b;
}
.tm-detail {
  font-size: 10.5px; color: #64748b; line-height: 1.4;
  display: -webkit-box; line-clamp: 3; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden;
}
.tm-expanded {
  margin-top: 4px;
  padding-top: 5px;
  border-top: 1px dashed rgba(148, 163, 184, 0.45);
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 0;
  overflow: auto;
}
.tm-expanded-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.tm-expanded-title {
  font-size: 9.5px;
  font-weight: 800;
  color: var(--node-text, #334155);
  opacity: 0.86;
}
.tm-expanded-text {
  font-size: 10px;
  line-height: 1.48;
  color: #475569;
}
.tm-expanded-chip {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(255,255,255,0.82);
  color: var(--node-text, #334155);
  font-size: 9.5px;
  font-weight: 700;
}
.tm-inline-children {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.tm-inline-child {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  padding: 2px 6px;
  border-radius: 999px;
  background: rgba(255,255,255,0.8);
  color: #475569;
  font-size: 9.5px;
  line-height: 1.4;
}
/* 根节点特殊样式 */
.tm-type-question {
  border-left-width: 4px;
  background: linear-gradient(135deg, #eef2ff, #e0e7ff) !important;
  border-color: #a5b4fc;
}
</style>
