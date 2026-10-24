# 上市公司财报「智能问数」助手

面向 A 股中药上市公司财报的自然语言问数系统，打通 **非结构化财报 PDF → 结构化财务数据库 → 自然语言理解 → SQL 生成 → 数据查询 → 可视化 → 归因分析** 的完整链路，支持多轮对话、意图澄清、多意图自主规划与结果溯源。

---

## 竞赛信息

| 项目 | 内容 |
|---|---|
| 赛事中文全称 | 2026 年（第 14 届）"泰迪杯"数据挖掘挑战赛 |
| 赛事英文名称 | 14th TipDM Cup Data Mining Challenge |
| 赛题 | B 题：上市公司财报"智能问数"助手 |
| 成绩 | 全国本科组 · B 题 · 一等奖；入围特等奖线上答辩 |

赛题原文见 [`docs/赛题-B题-题目说明.md`](docs/赛题-B题-题目说明.md)。

---

## 赛题任务与解法对照

| 赛题任务 | 核心要求 | 本项目的做法 |
|---|---|---|
| **任务一**：构建结构化财报数据库 | 从非结构化财报 PDF 提取业绩数据，写入核心业绩表、资产负债表、现金流量表、利润表；入库前做一致性校验 | `pdf_extractor.py` 做版式解析与字段抽取，`create_database.py` 建表并写入 `field_provenance` / `import_issues` 留痕，`data_importer.py` 完成导入与交叉校验，`quality_report.py` 输出数据质量报告 |
| **任务二**：搭建"智能问数"助手 | 自然语言 → 意图识别 → SQL 生成 → 查询 → 可视化 → 分析结论；支持多轮对话与意图澄清 | `backend/services/` 下的意图路由、对话状态、SQL 查询层、可视化模块；`backend/tasks/task2_runtime.py` 批量跑附件 4 问题并导出 `result_2.xlsx` |
| **任务三**：增强可靠性 | 融合研报知识库、多意图自主规划、归因分析与可解释性 | `backend/services/knowledge.py` 构建 RAG 知识库，`planner.py` / `llm_query_planner.py` 做多意图拆解与调度，`*_tracer.py` 记录数据溯源与计算溯源链，`task3_runtime.py` 批量跑附件 6 并导出 `result_3.xlsx` |

---

## 系统架构

```
                     ┌──────────────────────────────────────────┐
   非结构化输入 ───▶ │  pdf_extractor.py   财报解析 / 字段抽取    │
   （财报 PDF、      │  data_importer.py   导入 + 一致性校验      │
     研报 PDF）      │  create_database.py 建库建表（4 张主表）   │
                     └───────────────────┬──────────────────────┘
                                         ▼
                     ┌──────────────────────────────────────────┐
                     │  SQLite 财务数据库  +  研报向量知识库       │
                     └───────────────────┬──────────────────────┘
                                         ▼
   用户自然语言 ───▶ ┌──────────────────────────────────────────┐
                     │  意图路由 intent_router                   │
                     │  多意图规划 planner / llm_query_planner   │
                     │  SQL 生成与执行 database                  │
                     │  知识库检索 knowledge（BM25 + 向量 + RRF） │
                     │  重排序 rerank → 结论合成 → 图表生成       │
                     │  溯源追踪 query_layer / enhancement_layer │
                     └───────────────────┬──────────────────────┘
                                         ▼
                     FastAPI 后端  ◀──▶  Vue 3 前端（图表 / 溯源面板）
```

### 模块职责

| 模块 | 位置 | 职责 |
|---|---|---|
| 数据管线 | 根目录 `*.py` | PDF 解析、字段抽取、建库、导入、质量报告、知识库构建与压缩 |
| 后端服务 | `backend/` | FastAPI 应用：意图路由、对话状态、SQL 层、知识库、可视化、性能埋点 |
| 前端界面 | `smart-query-ui/` | Vue 3 + Vite + TypeScript：问数工作台、数据库视图、知识库视图、溯源与思维导图面板 |
| 模型横评 | `benchmark/` | 多模型对比、回答采集、 ground truth 生成、评分与 HTML 报告 |
| 消融实验 | `ablation/` | 对增强层 7 大组件逐一消融，量化各组件贡献 |
| 结果产物 | `result/`、`result_2.xlsx`、`result_3.xlsx` | 生成的图表与赛题要求的提交文件 |

---

## 目录结构

```
.
├── backend/                    # FastAPI 后端
│   ├── app.py                  # 应用入口与路由
│   ├── core/                   # 数据库、知识库存储、数据模型、上下文
│   ├── services/               # 意图路由 / 规划 / 知识库 / 可视化 / 溯源
│   └── tasks/                  # task2、task3 批量执行运行时
├── smart-query-ui/             # Vue 3 前端
│   └── src/{views,components,services,store}/
├── benchmark/                  # 模型横评
├── ablation/                   # 消融实验
├── quality_visual_reports/     # 数据质量可视化报告（HTML + SVG）
├── result/                     # 助手生成的可视化图表（按【问题编号_顺序编号】命名）
├── docs/                       # 赛题文档与数据库字段说明
├── result_2.xlsx               # 任务二提交结果
├── result_3.xlsx               # 任务三提交结果
├── config.example.json         # 配置模板（密钥留空）
└── requirements.txt            # Python 依赖
```

---

## 环境

| 组件 | 版本 |
|---|---|
| Python | 3.11 |
| Node.js | 18+（前端构建需要） |
| 主要依赖 | fastapi 0.115、pydantic 2.10、uvicorn 0.34、httpx 0.28、PyMuPDF 1.25、pdfplumber、openpyxl 3.1、numpy 2.1、matplotlib 3.9、rank_bm25、jieba |
| 前端 | vue 3.5、element-plus 2.13、echarts 5.6、pinia 3.0、vite 6.4、typescript 5.6 |

---

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
```

### 2. 准备数据

赛题官方数据（附件 1–7）**不随本仓库分发**，请自行从赛事官方渠道获取，放置到任意本地目录。

### 3. 配置

```bash
cp config.example.json config.json
```

编辑 `config.json`，填入：

- `reports_dir`：附件 2（财务报告）目录
- `research_reports_dir`：附件 5（研报数据）目录
- `embedding.api_key` / `llm.api_key` / `intent_llm.api_key` / `extract_llm.api_key`：各服务的 API 密钥

> ⚠️ 密钥不要提交到仓库。`config.json` 已在 `.gitignore` 中排除，仓库只提供 `config.example.json` 模板。

### 4. 建库与导入

```bash
python create_database.py       # 建表（核心业绩表 / 资产负债表 / 现金流量表 / 利润表）
python data_importer.py         # 解析财报 PDF 并导入，同时做一致性校验
```

### 5. 数据质量校验

```bash
python quality_report.py --db-path financial_reports.db \
                         --output-dir quality_reports \
                         --visual-output-dir quality_visual_reports
```

生成的 HTML 报告位于 `quality_visual_reports/`，本仓库保留了一次实测输出。

### 6. 构建研报知识库

```bash
python rebuild_kb.py            # 全量重建知识库（ ingestion + 索引）
python compact_kb.py            # 无损压缩知识库体积
```

### 7. 启动系统

```bash
# 后端（默认 http://127.0.0.1:8000）
python api_server.py

# 前端
cd smart-query-ui
npm install
npm run dev
```

### 8. 批量跑题与导出

```bash
python run_batch_export.py      # 依次执行任务二（附件4）与任务三（附件6）
python test_single_batch.py     # 单题调试
```

问题文件路径可用参数或环境变量指定，默认为当前目录下的文件名：

```bash
python run_batch_export.py --task2-file ./data/附件4：问题汇总.xlsx \
                           --task3-file ./data/附件6：问题汇总.xlsx
```

产物写入 `result_2.xlsx`、`result_3.xlsx`，图表写入 `result/`。

---

## 评测与消融

### 模型横评（`benchmark/`）

对 GLM-4-Flash、DeepSeek-V3、Qwen2.5-72B-Instruct、GLM-5.1 四个模型在财报问数场景下做响应时延、回答质量、成本、安全合规性四维对比，输出 HTML 报告与 CSV 评分表。

```bash
python -m benchmark.model_compare              # 自动跑批
python -m benchmark.model_compare --finalize   # 填入人工评分后生成最终报告
```

密钥通过环境变量注入：

```bash
export SILICONFLOW_API_KEY=...
export BIGMODEL_API_KEY=...
```

### 消融实验（`ablation/`）

对增强层 7 个组件逐一禁用，与全量基线对比，量化各组件对回答质量的贡献，并支持组合消融与 LLM-as-Judge 语义评估。详见 [`ablation/README.md`](ablation/README.md)。

| 消融条件 | 含义 |
|---|---|
| `-chart` | 禁用自动图表生成 |
| `-llm` | 禁用 LLM 深度分析 |
| `-provenance` | 禁用数据溯源 |
| `-thinking_map` | 禁用思维导图 |
| `-calc_traces` | 禁用计算溯源链 |
| `-deep_think` | 禁用深度思考 |
| `-kb` | 禁用知识库检索 |

---

## 关键参数

知识库检索与生成相关的可调参数集中在 `config.json` 的 `knowledge_base` 段：

| 参数 | 默认 | 说明 |
|---|---|---|
| `bm25_weight` / `vector_weight` | 0.3 / 0.7 | 稀疏检索与稠密检索的融合权重 |
| `rrf_k` | 60 | RRF 融合排序参数 |
| `rerank_candidates` | 30 | 送入重排模型的候选数 |
| `top_k` | 5 | 最终返回的片段数 |
| `child_chunk_min` / `child_chunk_max` | 100 / 300 | 子块切分长度 |
| `parent_chunk_max` | 900 | 父块最大长度（用于上下文回补） |
| `ocr_dpi` / `ocr_workers` | 200 / 4 | 扫描件 OCR 的 DPI 与并发数 |

---

## 结果产物说明

| 文件 | 说明 |
|---|---|
| `result/*.jpg` | 助手生成的可视化图表，按赛题要求以【问题编号_顺序编号】命名 |
| `result_2.xlsx` | 任务二回答提交文件（对应附件 7 表 3 样式） |
| `result_3.xlsx` | 任务三回答提交文件（对应附件 7 表 6 样式） |
| `quality_visual_reports/` | 数据质量可视化报告（HTML + SVG 图表） |
| `benchmark/model_compare_results/` | 模型横评的原始结果、评分 CSV 与 HTML 报告 |
| `ablation/ablation_results_*.json` | 各轮消融实验的原始结果 |

---

## 数据与版权说明

- 赛题原文与附件数据版权归 **"泰迪杯"数据挖掘挑战赛组委会** 所有。仓库中的 `docs/` 仅收录赛题说明与字段说明用于理解代码，正式使用请以官方发布版本为准。
- 官方赛题数据（附件 1–7）、构建出的数据库文件（`*.db`）与大规模中间结果均**未随仓库分发**，需按上文步骤自行准备。
- 仓库中出现的电话号码、金额等均为赛题数据或模型生成的样例内容，不含任何真实个人信息。

---

## 许可

代码部分采用 [MIT License](LICENSE)。赛题文档与数据的版权归原作者与组委会所有。
