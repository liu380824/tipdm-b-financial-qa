<script setup lang="ts">
import { onMounted, onUnmounted, reactive, nextTick } from 'vue'
import { RouterLink } from 'vue-router'
import { fetchSystemSettings } from '../services/api'

const homeSettings = reactive({ enable_multi_intent: true, default_auto_chart: true, default_enable_context: true, kb_top_k: 3 })

const sections = [
  { label: '智能对话', title: '自然语言驱动的全场景财务问答', to: '/assistant', icon: '💬',
    points: ['支持单指标查询、趋势分析、排序对比、现金流分析等多种问法','自动识别公司名称、财务指标与报告期，无需手动指定字段','多意图自动拆解：一句话同时查询多公司多指标，并行执行后汇总','结合知识库与数据库双源交叉验证，回答附带数据溯源'],
    slides: [
      { t: '智能对话', tp: 'chat', lines: [{ q: '某公司最近年报净利润是多少？', a: '系统返回该公司最近年报的净利润、同比变化及简明分析。' },{ q: '最近一个完整年度营收前5名的企业？', a: '系统列出排序结果，给出表格、图表和文字总结。' }] },
      { t: '多意图并行', tp: 'chat', lines: [{ q: '对比A公司和B公司最近三年的净利润和营收增长率', a: '已拆解为 4 个子查询并行执行：\n▸ A公司净利润趋势 ▸ B公司净利润趋势\n▸ A公司营收增长率 ▸ B公司营收增长率\n结果已合并输出为对比表格。' }] },
      { t: '数据溯源', tp: 'chat', lines: [{ q: '某企业现金流状况如何？', a: '经营性现金流净额同比增长18%，投资活动净流出减少12%。\n📎 数据库: cash_flow_sheet · 研报: 行业深度分析P12' }] }
    ] },
  { label: '数据库管理', title: '财报主库的浏览、查询与维护', to: '/database', icon: '🗄️',
    points: ['可视化浏览利润表、资产负债表、现金流量表等核心财务数据表','查看任意表的字段结构、数据类型与样本数据','支持执行只读 SQL 语句，快速验证数据或完成自定义分析','管理基础公司记录，确保主库数据完整性'],
    slides: [
      { t: '数据库 · income_sheet', tp: 'table', sql: 'SELECT stock_name, report_period, revenue, net_profit, margin FROM income_sheet LIMIT 4', rows: [['企业A','最近年报','8.2亿','1.1亿','42.3%'],['企业B','最近年报','15.6亿','2.4亿','38.7%'],['企业C','最近年报','6.8亿','0.9亿','45.1%'],['企业D','最近年报','22.1亿','3.6亿','35.2%']] },
      { t: '表结构 · balance_sheet', tp: 'schema', fields: [['stock_code','VARCHAR(10)','股票代码'],['total_assets','DECIMAL(20,2)','总资产'],['total_liabilities','DECIMAL(20,2)','总负债'],['equity','DECIMAL(20,2)','股东权益'],['report_period','VARCHAR(20)','报告期']] },
      { t: '数据总览', tp: 'stats', items: [{ v: '5 张', l: '数据表' },{ v: '65 家', l: '企业数' },{ v: '12,480', l: '数据行' },{ v: '2024', l: '最新年报' }] }
    ] },
  { label: '知识库管理', title: '研报 PDF 的导入、索引与检索', to: '/knowledge', icon: '📚',
    points: ['批量导入研报 PDF 文件，自动解析并切分为知识块','基于 BM25 全文检索，精准定位相关段落与表格数据','支持按公司、文档类型过滤，减少无关文档干扰','检索结果自动作为归因参考注入智能对话，回答可溯源'],
    slides: [
      { t: '知识库 · 检索结果', tp: 'docs', query: '医保目录中药产品价格', items: [{ name: '行业主题研报示例', tag: '行业研报', chunks: 128, score: 19.4 },{ name: '企业季报点评示例', tag: '个股研报', chunks: 64, score: 12.8 },{ name: '企业年报深度分析', tag: '个股研报', chunks: 96, score: 8.5 }] },
      { t: '知识库 · 文档管理', tp: 'doclist', items: [{ name: '医药行业2024年度展望', pages: 42, st: '已索引' },{ name: '某企业深度研究报告', pages: 28, st: '已索引' },{ name: '消费行业周报第36期', pages: 16, st: '已索引' },{ name: '新能源赛道分析报告', pages: 34, st: '索引中' }] },
      { t: '知识块预览', tp: 'chunk', src: '行业研报 · P12', txt: '根据市场数据分析，中药配方颗粒市场规模持续扩大，2024年同比增长约15.3%。主要受益于医保目录扩容与基层医疗机构覆盖率提升，龙头企业市占率进一步集中……' }
    ] },
  { label: '系统设置', title: 'LLM 服务与对话行为统一配置', to: '/settings', icon: '⚙️',
    points: ['配置 LLM 服务地址、模型名称、API Key 等连接参数','调整对话默认行为：自动图表、上下文继承、多意图开关','设置知识库检索阈值、Top-K、温度等推理参数','查看系统运行状态，诊断 LLM 连通性与数据库健康状态'],
    slides: [
      { t: '运行状态', tp: 'status', items: [{ label: 'LLM 服务', value: '已连接', detail: '模型响应正常' },{ label: '知识库', value: '已接入', detail: '128 篇文档已索引' },{ label: '数据库', value: '已接入', detail: '5表 · 65家企业' },{ label: '多意图', value: '已启用', detail: '自动拆解并行' }] },
      { t: 'LLM 配置', tp: 'config', items: [{ k: 'API 地址', v: 'http://localhost:11434' },{ k: '模型名称', v: 'qwen2.5:latest' },{ k: 'Temperature', v: '0.3' },{ k: 'Max Tokens', v: '4096' }] },
      { t: '对话行为', tp: 'config', items: [{ k: '自动图表', v: '开启' },{ k: '上下文继承', v: '开启' },{ k: '多意图拆解', v: '开启' },{ k: 'KB Top-K', v: '3' }] }
    ] }
]

/* ── Carousel ── */
const slideIdx = reactive<Record<number, number>>({})
sections.forEach((_, i) => { slideIdx[i] = 0 })
let timers: number[] = []

/* ── Scroll reveal ── */
let obs: IntersectionObserver | null = null

const scrollDown = () => document.querySelector('.section-block')?.scrollIntoView({ behavior: 'smooth' })

onMounted(async () => {
  try { const s = await fetchSystemSettings(); Object.assign(homeSettings, { enable_multi_intent: s.assistant.enable_multi_intent, default_auto_chart: s.assistant.default_auto_chart, default_enable_context: s.assistant.default_enable_context, kb_top_k: s.assistant.kb_top_k }) } catch {}
  sections.forEach((sec, i) => {
    timers.push(window.setInterval(() => { slideIdx[i] = (slideIdx[i] + 1) % sec.slides.length }, 4500))
  })
  nextTick(() => {
    obs = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) { (e.target as HTMLElement).classList.add('in-view'); obs?.unobserve(e.target) } })
    }, { threshold: 0.12, rootMargin: '0px 0px -30px 0px' })
    document.querySelectorAll('.reveal').forEach(el => obs?.observe(el))
  })
})
onUnmounted(() => { timers.forEach(t => clearInterval(t)); obs?.disconnect() })
</script>

<template>
  <div class="home-page">

    <!-- ══ Hero ══ -->
    <section class="hero">
      <div class="hero-orb hero-orb--1"></div>
      <div class="hero-orb hero-orb--2"></div>
      <div class="hero-inner">
        <div class="hero-text">
          <div class="hero-badge h-enter" style="--di:0">上市公司财报智能分析平台</div>
          <h1 class="hero-title h-enter" style="--di:1">智能问数<br/><span class="hero-accent">全链路财务分析</span></h1>
          <p class="hero-desc h-enter" style="--di:2">打通财务报表数据与研报知识，构建结构化+非结构化融合分析链路。全场景智能问答，帮助用户高效完成财务分析、数据查询与报告生成。</p>
          <div class="hero-actions h-enter" style="--di:3">
            <RouterLink class="hero-btn hero-btn--primary" to="/assistant"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>立即开始对话</RouterLink>
            <button class="hero-btn hero-btn--ghost" @click="scrollDown">了解更多 <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/></svg></button>
          </div>
          <div class="hero-chips h-enter" style="--di:4">
            <span class="hero-chip" :class="{'hero-chip--on': homeSettings.enable_multi_intent}"><i class="chip-dot"></i>多意图拆解</span>
            <span class="hero-chip" :class="{'hero-chip--on': homeSettings.default_auto_chart}"><i class="chip-dot"></i>自动图表</span>
            <span class="hero-chip" :class="{'hero-chip--on': homeSettings.default_enable_context}"><i class="chip-dot"></i>上下文继承</span>
          </div>
        </div>
        <div class="hero-visual h-enter" style="--di:2">
          <div class="hg-circle hg-circle--1"></div>
          <div class="hg-circle hg-circle--2"></div>
          <div class="hg-card hg-card--1"><div class="hg-bar" style="width:80%"></div><div class="hg-bar" style="width:55%"></div><div class="hg-bar" style="width:92%"></div><div class="hg-bar" style="width:40%"></div></div>
          <div class="hg-card hg-card--2"><span class="hg-dot"></span><span class="hg-line"></span><span class="hg-lbl">在线</span></div>
          <div class="hg-card hg-card--3"><span class="hg-num">+12.4%</span><span class="hg-sub">同比增长</span></div>
          <div class="hg-card hg-card--4"><span class="hg-num hg-num--teal">65 家</span><span class="hg-sub">上市公司</span></div>
        </div>
      </div>
      <div class="hero-scroll" @click="scrollDown"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5"/></svg></div>
    </section>

    <!-- ══ Feature sections — carousel + scroll reveal ══ -->
    <section v-for="(sec, idx) in sections" :key="sec.label" class="section-block" :class="{'section-block--alt': idx % 2 === 1}">
      <div class="sec-inner" :class="{'sec-inner--rev': idx % 2 === 1}">
        <div class="sec-text reveal" :class="idx % 2 === 0 ? 'reveal--left' : 'reveal--right'">
          <div class="sec-label"><span>{{ sec.icon }}</span> {{ sec.label }}</div>
          <h2 class="sec-title">{{ sec.title }}</h2>
          <ul class="sec-points">
            <li v-for="p in sec.points" :key="p"><svg class="ck" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg>{{ p }}</li>
          </ul>
          <RouterLink :to="sec.to" class="sec-goto">进入{{ sec.label }} <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3"/></svg></RouterLink>
        </div>
        <div class="sec-mock reveal" :class="idx % 2 === 0 ? 'reveal--right' : 'reveal--left'">
          <div class="carousel">
            <template v-for="(sl, si) in sec.slides" :key="si">
              <div class="cr-panel" :class="{ 'cr-panel--on': slideIdx[idx] === si }">
                <div class="mk">
                  <div class="mk-head"><span class="mk-d" style="background:#ff5f57"></span><span class="mk-d" style="background:#ffbd2e"></span><span class="mk-d" style="background:#28c840"></span><span class="mk-t">{{ sl.t }}</span></div>
                  <div class="mk-body" :class="{'mk-body--tbl': sl.tp==='table'||sl.tp==='schema'}">
                    <template v-if="sl.tp==='chat'"><div v-for="l in (sl as any).lines" :key="l.q" class="mk-pair"><div class="mk-q"><span class="mk-qb">{{ l.q }}</span></div><div class="mk-a"><span class="mk-ai">AI</span><span class="mk-ab">{{ l.a }}</span></div></div></template>
                    <template v-else-if="sl.tp==='table'"><div class="mk-sql">▶ {{ (sl as any).sql }}</div><table class="mk-tbl"><thead><tr><th>公司</th><th>报告期</th><th>营收</th><th>净利润</th><th>毛利率</th></tr></thead><tbody><tr v-for="(r,ri) in (sl as any).rows" :key="ri"><td v-for="(c,ci) in r" :key="ci">{{ c }}</td></tr></tbody></table><div class="mk-rc">✓ 返回 {{ (sl as any).rows.length }} 行</div></template>
                    <template v-else-if="sl.tp==='schema'"><table class="mk-tbl"><thead><tr><th>字段名</th><th>类型</th><th>说明</th></tr></thead><tbody><tr v-for="(f,fi) in (sl as any).fields" :key="fi"><td class="mk-field">{{ f[0] }}</td><td>{{ f[1] }}</td><td>{{ f[2] }}</td></tr></tbody></table></template>
                    <template v-else-if="sl.tp==='stats'"><div class="mk-stats"><div v-for="s in (sl as any).items" :key="s.l" class="mk-stat-card"><span class="mk-stat-v">{{ s.v }}</span><span class="mk-stat-l">{{ s.l }}</span></div></div></template>
                    <template v-else-if="sl.tp==='docs'"><div class="mk-sb">🔍 {{ (sl as any).query }}</div><div v-for="d in (sl as any).items" :key="d.name" class="mk-dc"><div class="mk-dc-top"><span class="mk-dc-n">{{ d.name }}</span><span class="mk-dc-s">{{ d.score }}</span></div><div class="mk-dc-meta"><span class="mk-dc-tag">{{ d.tag }}</span><span class="mk-dc-c">{{ d.chunks }} 个块</span></div></div></template>
                    <template v-else-if="sl.tp==='doclist'"><div v-for="d in (sl as any).items" :key="d.name" class="mk-dl"><span class="mk-dl-icon">📄</span><div class="mk-dl-info"><span class="mk-dl-name">{{ d.name }}</span><span class="mk-dl-pages">{{ d.pages }} 页</span></div><span class="mk-dl-st" :class="{'mk-dl-st--ing': d.st!=='已索引'}">{{ d.st }}</span></div></template>
                    <template v-else-if="sl.tp==='chunk'"><div class="mk-chunk"><div class="mk-chunk-src">📎 {{ (sl as any).src }}</div><p class="mk-chunk-txt">{{ (sl as any).txt }}</p></div></template>
                    <template v-else-if="sl.tp==='status'"><div v-for="s in (sl as any).items" :key="s.label" class="mk-sr"><div class="mk-sr-l"><span class="mk-sr-dot"></span><span>{{ s.label }}</span></div><div class="mk-sr-r"><span class="mk-sr-v">{{ s.value }}</span><span class="mk-sr-d">{{ s.detail }}</span></div></div></template>
                    <template v-else-if="sl.tp==='config'"><div v-for="c in (sl as any).items" :key="c.k" class="mk-cfg"><span class="mk-cfg-l">{{ c.k }}</span><span class="mk-cfg-v">{{ c.v }}</span></div></template>
                  </div>
                </div>
              </div>
            </template>
            <div class="cr-dots"><span v-for="(_, si) in sec.slides" :key="si" class="cr-dot" :class="{ 'cr-dot--on': slideIdx[idx] === si }" @click="slideIdx[idx] = si"></span></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ══ Footer ══ -->
    <footer class="home-footer">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z"/></svg>
      <span>智能问数 · Financial AI &nbsp;—&nbsp; 上市公司财报智能分析平台</span>
    </footer>

  </div>
</template>

<style scoped>
/* ═══════════════════════════════════════════════
   HomeView — Animated Landing with Carousel
   ═══════════════════════════════════════════════ */
.home-page{flex:1;min-width:0;display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;background:var(--bg-primary);scroll-behavior:smooth}

/* ══ Animations ══ */
@keyframes heroIn{from{opacity:0;transform:translateY(36px)}to{opacity:1;transform:translateY(0)}}
@keyframes g-fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
@keyframes bounce{0%,100%{transform:translateX(-50%) translateY(0)}50%{transform:translateX(-50%) translateY(10px)}}

.h-enter{opacity:0;transform:translateY(36px);animation:heroIn .7s cubic-bezier(.22,1,.36,1) forwards;animation-delay:calc(var(--di) * 0.13s + .15s)}

/* Scroll reveal */
.reveal{opacity:0;transition:opacity .85s cubic-bezier(.22,1,.36,1),transform .85s cubic-bezier(.22,1,.36,1)}
.reveal--left{transform:translateX(-60px)}
.reveal--right{transform:translateX(60px)}
.reveal.in-view{opacity:1;transform:translateX(0)}

/* ══ Hero ══ */
.hero{min-height:100vh;display:flex;flex-direction:column;justify-content:center;align-items:center;position:relative;overflow:hidden;background:var(--bg-secondary);padding:0 4% 0 6%}
.hero-orb{position:absolute;border-radius:50%;pointer-events:none;filter:blur(100px)}
.hero-orb--1{width:700px;height:700px;top:-18%;right:-10%;background:rgba(37,99,235,.07)}
.hero-orb--2{width:500px;height:500px;bottom:-12%;left:-8%;background:rgba(5,150,105,.05)}
.hero-inner{display:flex;align-items:center;gap:48px;width:100%;position:relative;z-index:1}
.hero-text{flex:1;min-width:0}
.hero-badge{display:inline-flex;padding:7px 20px;border-radius:999px;background:var(--accent-bg);color:var(--accent);font-size:15px;font-weight:600;letter-spacing:.03em;border:1px solid var(--accent-border)}
.hero-title{margin:28px 0 24px;font-family:var(--font-display);font-size:72px;font-weight:800;line-height:1.1;color:var(--text-primary);letter-spacing:-.04em}
.hero-accent{background:linear-gradient(135deg,var(--accent),#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-desc{margin:0;max-width:600px;font-size:20px;line-height:1.75;color:var(--text-secondary)}
.hero-actions{display:flex;gap:16px;margin-top:36px}
.hero-btn{display:inline-flex;align-items:center;gap:10px;padding:14px 32px;border-radius:var(--radius-lg);font-size:17px;font-weight:600;text-decoration:none;cursor:pointer;transition:all .2s ease;border:none}
.hero-btn--primary{background:var(--accent);color:#fff;box-shadow:0 4px 14px rgba(37,99,235,.3)}
.hero-btn--primary:hover{background:var(--accent-hover);box-shadow:0 6px 24px rgba(37,99,235,.4);transform:translateY(-2px)}
.hero-btn--ghost{background:transparent;color:var(--text-secondary);border:1.5px solid var(--border-primary);padding:13px 30px}
.hero-btn--ghost:hover{border-color:var(--accent-border);color:var(--accent);background:var(--accent-bg)}
.hero-chips{display:flex;gap:14px;margin-top:40px;flex-wrap:wrap}
.hero-chip{display:inline-flex;align-items:center;gap:8px;padding:8px 18px;border-radius:999px;font-size:15px;font-weight:500;color:var(--text-tertiary);background:var(--bg-inset);border:1px solid var(--border-secondary);transition:all .2s}
.hero-chip--on{color:var(--success);border-color:var(--success-border);background:var(--success-bg)}
.chip-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--text-quaternary);transition:background .2s;font-style:normal}
.hero-chip--on .chip-dot{background:var(--success);box-shadow:0 0 6px rgba(5,150,105,.5)}

/* Hero visual */
.hero-visual{flex-shrink:0;width:460px;height:400px;position:relative}
.hg-circle{position:absolute;border-radius:50%}
.hg-circle--1{width:320px;height:320px;top:-20px;right:-30px;background:radial-gradient(circle,rgba(37,99,235,.08),transparent)}
.hg-circle--2{width:180px;height:180px;bottom:0;left:0;background:radial-gradient(circle,rgba(5,150,105,.07),transparent)}
.hg-card{position:absolute;background:var(--bg-secondary);border-radius:var(--radius-lg);box-shadow:var(--shadow-lg);border:1px solid var(--border-secondary);padding:20px}
.hg-card--1{top:0;left:10px;width:200px;display:flex;flex-direction:column;gap:10px;animation:g-fadeUp .6s ease .6s both}
.hg-bar{height:10px;border-radius:5px;background:linear-gradient(90deg,var(--accent),#60a5fa)}
.hg-card--2{bottom:20px;left:30px;display:flex;align-items:center;gap:10px;padding:14px 20px;animation:g-fadeUp .6s ease .75s both}
.hg-dot{width:12px;height:12px;border-radius:50%;background:var(--success);flex-shrink:0;box-shadow:0 0 6px rgba(5,150,105,.5)}
.hg-line{width:70px;height:5px;border-radius:3px;background:linear-gradient(90deg,#d1fae5,var(--success))}
.hg-lbl{font-size:15px;font-weight:700;color:var(--success)}
.hg-card--3{top:70px;right:0;padding:16px 24px;text-align:center;animation:g-fadeUp .6s ease .85s both}
.hg-card--4{bottom:60px;right:20px;padding:16px 24px;text-align:center;animation:g-fadeUp .6s ease .95s both}
.hg-num{display:block;font-family:var(--font-mono);font-size:26px;font-weight:700;color:var(--success)}
.hg-num--teal{color:var(--teal)}
.hg-sub{display:block;font-size:13px;color:var(--text-tertiary);margin-top:4px;font-weight:500}

/* Scroll hint */
.hero-scroll{position:absolute;bottom:32px;left:50%;transform:translateX(-50%);color:var(--text-quaternary);cursor:pointer;animation:bounce 2s infinite;transition:color .2s}
.hero-scroll:hover{color:var(--accent)}

/* ══ Feature sections ══ */
.section-block{padding:80px 6%;background:var(--bg-secondary)}
.section-block--alt{background:var(--bg-primary)}
.sec-inner{display:flex;align-items:center;gap:56px;max-width:1400px;margin:0 auto}
.sec-inner--rev{flex-direction:row-reverse}
.sec-text{flex:0 0 44%;min-width:0}
.sec-label{display:inline-flex;align-items:center;gap:8px;padding:6px 16px;border-radius:999px;font-size:15px;font-weight:600;color:var(--accent);background:var(--accent-bg);border:1px solid var(--accent-border);margin-bottom:16px}
.sec-title{font-family:var(--font-display);font-size:36px;font-weight:700;color:var(--text-primary);margin:0 0 24px;line-height:1.3;letter-spacing:-.02em}
.sec-points{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:16px}
.sec-points li{display:flex;align-items:flex-start;gap:12px;font-size:17px;line-height:1.7;color:var(--text-secondary)}
.sec-points .ck{flex-shrink:0;margin-top:5px;width:18px;height:18px}
.sec-goto{display:inline-flex;align-items:center;gap:10px;margin-top:32px;padding:13px 28px;border-radius:var(--radius-lg);background:var(--accent);color:#fff;font-size:17px;font-weight:600;text-decoration:none;transition:all .2s ease;box-shadow:0 3px 12px rgba(37,99,235,.22)}
.sec-goto:hover{background:var(--accent-hover);transform:translateY(-2px);box-shadow:0 6px 20px rgba(37,99,235,.35)}

/* ══ Carousel — grid-stacked, equal height ══ */
.sec-mock{flex:1;min-width:0}
.carousel{display:grid;grid-template-rows:1fr auto;gap:18px}
.cr-panel{grid-row:1;grid-column:1;opacity:0;transition:opacity .7s cubic-bezier(.4,0,.2,1);pointer-events:none;min-width:0}
.cr-panel--on{opacity:1;pointer-events:auto}
.cr-dots{grid-row:2;grid-column:1;display:flex;justify-content:center;gap:10px}
.cr-dot{width:9px;height:9px;border-radius:50%;background:var(--text-quaternary);cursor:pointer;transition:all .3s ease;opacity:.5}
.cr-dot--on{background:var(--accent);opacity:1;transform:scale(1.35);box-shadow:0 0 8px rgba(37,99,235,.45)}

/* ══ Mock panels ══ */
.mk{background:#111318;border-radius:16px;overflow:hidden;box-shadow:0 16px 56px rgba(0,0,0,.2);border:1px solid rgba(255,255,255,.06);display:flex;flex-direction:column}
.mk-head{display:flex;align-items:center;gap:7px;padding:12px 18px;background:#0a0b0f;border-bottom:1px solid rgba(255,255,255,.06)}
.mk-t{margin-left:8px;font-family:var(--font-mono);font-size:13px;color:#4b5563;font-weight:500}
.mk-d{width:12px;height:12px;border-radius:50%}
.mk-body{padding:22px;display:flex;flex-direction:column;gap:16px;overflow:hidden}

/* Chat */
.mk-pair{display:flex;flex-direction:column;gap:12px}
.mk-q{display:flex;justify-content:flex-end}
.mk-qb{background:var(--accent);color:#fff;font-size:15px;line-height:1.6;padding:12px 18px;border-radius:14px 14px 4px 14px;max-width:88%}
.mk-a{display:flex;align-items:flex-start;gap:10px}
.mk-ai{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px}
.mk-ab{background:#1e2028;color:#d1d5db;font-size:15px;line-height:1.6;padding:12px 18px;border-radius:4px 14px 14px 14px;max-width:88%;white-space:pre-line}

/* Table / Schema */
.mk-body--tbl{gap:10px}
.mk-sql{font-family:var(--font-mono);font-size:13px;color:#60a5fa;background:#0a0b0f;padding:8px 14px;border-radius:var(--radius-sm);line-height:1.5}
.mk-tbl{width:100%;border-collapse:collapse;font-size:14px}
.mk-tbl th{background:#0a0b0f;color:#6b7280;padding:8px 10px;text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.mk-tbl td{padding:8px 10px;color:#d1d5db;border-top:1px solid rgba(255,255,255,.04)}
.mk-tbl tr:nth-child(even) td{background:rgba(255,255,255,.02)}
.mk-rc{font-family:var(--font-mono);font-size:13px;color:#10b981;padding:4px 0}
.mk-field{color:#60a5fa !important;font-family:var(--font-mono)}

/* Stats grid */
.mk-stats{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.mk-stat-card{background:#1e2028;border-radius:var(--radius-md);padding:18px;text-align:center;border:1px solid rgba(255,255,255,.04)}
.mk-stat-v{display:block;font-family:var(--font-mono);font-size:26px;font-weight:700;color:#60a5fa}
.mk-stat-l{display:block;font-size:13px;color:#6b7280;margin-top:6px}

/* Docs search */
.mk-sb{display:flex;align-items:center;gap:8px;background:#0a0b0f;padding:8px 14px;border-radius:var(--radius-sm);font-size:14px;color:#6b7280}
.mk-dc{background:#1e2028;border-radius:var(--radius-md);padding:12px 16px;border:1px solid rgba(255,255,255,.04)}
.mk-dc-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.mk-dc-n{font-size:14px;color:#d1d5db;font-weight:600}
.mk-dc-s{font-family:var(--font-mono);font-size:14px;color:#fbbf24;font-weight:700}
.mk-dc-meta{display:flex;gap:10px}
.mk-dc-tag{font-size:12px;color:#60a5fa;background:rgba(37,99,235,.12);padding:2px 8px;border-radius:4px}
.mk-dc-c{font-size:12px;color:#6b7280}

/* Doc list */
.mk-dl{display:flex;align-items:center;gap:12px;background:#1e2028;border-radius:var(--radius-sm);padding:12px 16px;border:1px solid rgba(255,255,255,.04)}
.mk-dl-icon{font-size:20px;flex-shrink:0}
.mk-dl-info{flex:1;display:flex;flex-direction:column}
.mk-dl-name{font-size:14px;color:#d1d5db;font-weight:600}
.mk-dl-pages{font-size:12px;color:#6b7280;margin-top:2px}
.mk-dl-st{font-size:12px;font-weight:600;color:#10b981;flex-shrink:0}
.mk-dl-st--ing{color:#fbbf24}

/* Chunk preview */
.mk-chunk{background:#1e2028;border-radius:var(--radius-md);padding:18px;border:1px solid rgba(255,255,255,.04)}
.mk-chunk-src{font-size:13px;color:#60a5fa;margin-bottom:10px;font-weight:600}
.mk-chunk-txt{font-size:14px;color:#b0b6c3;line-height:1.8;margin:0}

/* Status */
.mk-sr{display:flex;align-items:center;justify-content:space-between;background:#1e2028;border-radius:var(--radius-md);padding:12px 16px;border:1px solid rgba(255,255,255,.04)}
.mk-sr-l{display:flex;align-items:center;gap:10px;font-size:14px;color:#9ca3af}
.mk-sr-dot{width:9px;height:9px;border-radius:50%;background:#10b981;flex-shrink:0;box-shadow:0 0 5px rgba(16,185,129,.45)}
.mk-sr-r{text-align:right}
.mk-sr-v{display:block;font-size:14px;color:#d1d5db;font-weight:600}
.mk-sr-d{display:block;font-size:12px;color:#6b7280;margin-top:2px}

/* Config */
.mk-cfg{display:flex;align-items:center;justify-content:space-between;background:#1e2028;border-radius:var(--radius-sm);padding:12px 16px;border:1px solid rgba(255,255,255,.04)}
.mk-cfg-l{font-size:14px;color:#9ca3af}
.mk-cfg-v{font-size:14px;color:#d1d5db;font-weight:600;font-family:var(--font-mono)}

/* ══ Footer ══ */
.home-footer{display:flex;align-items:center;justify-content:center;gap:12px;padding:48px 24px;background:var(--bg-secondary);border-top:1px solid var(--border-secondary);color:var(--text-tertiary);font-size:15px}

/* ══ Responsive ══ */
@media(max-width:1100px){
  .hero-inner{flex-direction:column;gap:36px;text-align:center}
  .hero-text{align-items:center;display:flex;flex-direction:column}
  .hero-title{font-size:52px}
  .hero-desc{max-width:100%}
  .hero-actions{justify-content:center}
  .hero-chips{justify-content:center}
  .hero-visual{width:360px;height:320px}
  .sec-inner,.sec-inner--rev{flex-direction:column;gap:36px;text-align:center}
  .sec-text{flex:none;width:100%}
  .sec-points li{text-align:left}
  .sec-goto{align-self:center}
  .sec-label{align-self:center}
  .section-block{padding:60px 5%}
  .reveal--left,.reveal--right{transform:translateY(40px)}
  .reveal.in-view{transform:translateY(0)}
}
@media(max-width:640px){
  .hero-title{font-size:40px}
  .hero-desc{font-size:17px}
  .hero{padding:0 4%}
  .sec-title{font-size:28px}
  .section-block{padding:48px 4%}
}
</style>
