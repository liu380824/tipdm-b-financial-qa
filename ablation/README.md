# 消融实验（Ablation Study）

## 目的

对增强层（Enhancement Layer）的 **7 大组件**逐一禁用，与 Baseline（全量开启）对比，量化每个组件对最终回答质量的贡献度。支持多次重复实验、组合消融、效应量显著性分析和 LLM-as-Judge 语义评估。

## 消融条件

### 单组件消融

| 条件标识 | 描述 | Monkey-patch 目标 |
|---------|------|-------------------|
| `full` | Baseline — 全部组件开启 | 无 |
| `-chart` | 禁用自动图表生成 | `auto_generate_chart_payload → None` |
| `-llm` | 禁用 LLM 深度分析 | `_build_llm_analysis → stub` |
| `-provenance` | 禁用数据溯源 | `_fetch_support_provenance_record → None` |
| `-thinking_map` | 禁用思维导图 | `_build_thinking_map → {}` |
| `-calc_traces` | 禁用计算溯源链 | `_build_calc_traces → ([], [])` |
| `-deep_think` | 禁用深度思考 | `deep_think=False` |
| `-kb` | 禁用知识库检索 | `_retrieve_kb_context → ('', [])` |

### 组合消融（`--pairwise`）

| 条件标识 | 描述 |
|---------|------|
| `-llm-kb` | 同时禁用 LLM 分析 + 知识库检索 |
| `-chart-thinking_map` | 同时禁用图表 + 思维导图 |
| `-provenance-calc_traces` | 同时禁用数据溯源 + 计算溯源链 |

## 衡量指标

| 指标 | 含义 | 越大越好 |
|------|------|---------|
| content_length | LLM 分析内容字数 | ✅ |
| has_chart | 是否生成图表 (0/1) | ✅ |
| provenance_count | 溯源记录条数 | ✅ |
| calc_traces_count | 计算溯源链条数 | ✅ |
| attribution_steps | 归因分析步骤数 | ✅ |
| has_thinking_map | 是否有思维导图 (0/1) | ✅ |
| insights_count | 数据洞察条数 | ✅ |
| follow_up_count | 后续建议条数 | ✅ |
| kb_refs_count | 知识库命中条数 | ✅ |
| thinking_count | 深度思考输出条数 | ✅ |
| elapsed | 响应耗时 (秒) | ❌ |
| judge_avg | LLM-as-Judge 平均分 (1-10) | ✅ |

## 统计分析

- **mean±std**：重复 ≥2 次时自动计算各指标均值和标准差
- **Cohen's d 效应量**：衡量消融前后差异大小
  - `***` d≥0.8 大效应 | `**` d≥0.5 中效应 | `*` d≥0.2 小效应 | `ns` 不显著

## 使用方法

```bash
# 默认1个问题，单次运行
python -m ablation.run_ablation

# 自定义问题
python -m ablation.run_ablation "贵州茅台2025年净利润趋势"

# 3个问题，每条件重复3次（推荐，可计算统计显著性）
python -m ablation.run_ablation -n 3 -r 3

# 含组合消融 + LLM 语义打分
python -m ablation.run_ablation -n 3 -r 3 --pairwise --judge

# 同时保存 JSON 数据
python -m ablation.run_ablation -n 2 -r 2 --json
```

### CLI 参数

| 参数 | 缩写 | 默认 | 说明 |
|------|------|------|------|
| `question` | | | 自定义单个问题 |
| `--questions` | `-n` | 1 | 使用前 N 个默认问题 (1-7) |
| `--repeats` | `-r` | 1 | 每条件重复次数（≥2 启用统计） |
| `--pairwise` | `-p` | off | 包含组合消融条件 |
| `--judge` | | off | 启用 LLM-as-Judge 评分 |
| `--output` | `-o` | auto | HTML 输出路径 |
| `--json` | `-j` | off | 同时保存 JSON |

## 输出

- **控制台**：实时进度 + mean±std 对比表 + 显著性标记 + 组件重要度排名
- **HTML 报告**：对比表 + 质量评分柱状图 + 热力图 + 组件重要度排名 + Cohen's d 效应量
- **JSON**（可选）：结构化原始数据 + 聚合数据 + 显著性分析，支持后续分析
