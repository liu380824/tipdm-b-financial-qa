"""Task 3 batch runner: reads 附件6 questions, answers with enhanced assistant
(multi-intent + KB references), outputs result_3.xlsx in 表6 format."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import jieba

from .task2_runtime import save_chart_image, _unique_keep_order, _normalize_batch_text
from ..services.llm import llm_available

from openpyxl import Workbook, load_workbook

QuestionRecord = dict[str, Any]
ReplyBuilder = Callable[..., dict[str, Any]]

# ── question loading ──────────────────────────────────────────────────────────

def find_task3_question_file(base_dir: Path) -> Path:
    matches = [
        p for p in base_dir.rglob('*.xlsx')
        if '附件6' in p.name and '问题汇总' in p.name
    ]
    if not matches:
        raise FileNotFoundError('未找到任务三问题文件（附件6：问题汇总.xlsx）')
    return sorted(matches, key=lambda p: (len(p.parts), str(p)))[0]


def load_task3_questions(base_dir: Path, question_file: Path | None = None) -> list[QuestionRecord]:
    path = question_file or find_task3_question_file(base_dir)
    wb = load_workbook(path, data_only=True, read_only=True)
    sheet = wb.active
    records: list[QuestionRecord] = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        qid = str(row[0] or '').strip()
        qtype = str(row[1] or '').strip()
        raw = str(row[2] or '').strip()
        if not qid or not raw:
            continue
        turns = json.loads(raw)
        normalized = [
            {'Q': str(t.get('Q', '')).strip()}
            for t in turns
            if isinstance(t, dict) and str(t.get('Q', '')).strip()
        ]
        records.append({'id': qid, 'questionType': qtype, 'questions': normalized, 'raw': raw})
    wb.close()
    return records


# ── workbook builder (表6 format) ─────────────────────────────────────────────
# Columns: 编号 | 问题 | SQL查询语法 | 回答

def _build_task3_workbook(rows: list[dict[str, str]], path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = '任务三结果'
    ws.append(['编号', '问题', 'SQL查询语法', '回答'])
    for row in rows:
        ws.append([row['编号'], row['问题'], row['SQL查询语法'], row['回答']])
    for col, width in [('A', 12), ('B', 48), ('C', 72), ('D', 120)]:
        ws.column_dimensions[col].width = width
    wb.save(path)


# ── answer format (表5) ───────────────────────────────────────────────────────
# Each turn answer: {"content": "...", "image": [...], "references": [...]}
#
# Reference generation follows an "anchor-based" logic:
#   1. Group chunks by document  (paper_path)
#   2. Keep top-3 highest-scoring chunks per document
#   3. Choose best figure title as paper_image  (scored by question/text overlap)
#   4. Extract 1-2 explanation sentences near that figure from parent_text
#   5. Fall back to child_text evidence if no good sentences exist

# ── helpers ────────────────────────────────────────────────────────────────────

_FIGURE_TITLE_RE = re.compile(r'(?:图表?|表格?)\s*\d+[：:．.\s]')
_TABLE_NOISE_RE = re.compile(r'[\|\t]{2,}|(\d[\d,.%]+\s*){5,}')
_CONCLUSION_KEYWORDS = re.compile(
    r'(根据|显示|表明|最终|主要|共|包括|同比|环比|增长|下降|受益|驱动|原因|'
    r'入选|纳入|新增|合计|占比|达到|超过|其中|预计|建议|关注|维持|上调|下调)'
)


def _strip_punct(text: str) -> str:
    """Remove punctuation / whitespace for token matching."""
    return re.sub(
        r'[\s\u3000:：,，.。;；、/\\|()（）\[\]【】"""\x27\x27\x27《》<>·\x2d_—]+', '',
        str(text or ''),
    )


_STOPWORDS = {'图表', '表格', '图片', '图', '表', '的', '了', '在', '是', '和',
              '与', '及', '等', '年', '月', '日', '为', '有', '个', '从', '到',
              '中', '上', '下', '以', '其', '对', '将', '被', '把', '由'}


def _extract_tokens(text: str) -> list[str]:
    """Extract meaningful tokens using jieba segmentation + numbers."""
    tokens = []
    for tok in jieba.lcut(str(text or '')):
        tok = tok.strip()
        if not tok or len(tok) < 2:
            continue
        if tok in _STOPWORDS:
            continue
        tokens.append(tok)
    # Also extract standalone numbers
    for num in re.findall(r'\d+', str(text or '')):
        if len(num) >= 1 and num not in tokens:
            tokens.append(num)
    return tokens


_SOURCE_LINE_RE = re.compile(r'^(来源|数据来源|资料来源|注[：:])')
_FIGURE_LINE_RE = re.compile(r'^(?:图表?|表格?)\s*\d+[：:．.\s]')


def _split_sentences(text: str) -> list[str]:
    """Split into sentences; filter out table rows, figure titles, source lines."""
    raw = re.split(r'(?<=[。！？\n])', str(text or ''))
    sentences: list[str] = []
    for s in raw:
        s = s.strip()
        if len(s) < 10:
            continue
        if _TABLE_NOISE_RE.search(s):
            continue
        if s.count('|') > 2:
            continue
        # Skip lines that start with figure/table title pattern
        if _FIGURE_LINE_RE.match(s):
            continue
        # Skip source attribution lines
        if _SOURCE_LINE_RE.match(s):
            continue
        sentences.append(s)
    return sentences


def _score_sentence(sentence: str, question: str, figure_title: str) -> float:
    """Score a sentence for suitability as reference text."""
    score = 0.0
    s_norm = _strip_punct(sentence)
    q_norm = _strip_punct(question)
    ft_norm = _strip_punct(figure_title)

    # Token overlap with question
    q_tokens = _extract_tokens(question)
    for tok in q_tokens:
        if tok in s_norm:
            score += 8.0 if not tok.isdigit() else 2.0

    # Token overlap with figure title
    ft_tokens = _extract_tokens(figure_title)
    for tok in ft_tokens:
        if tok in s_norm:
            score += 6.0 if not tok.isdigit() else 2.0

    # Contains conclusion keywords
    conclusion_hits = _CONCLUSION_KEYWORDS.findall(sentence)
    score += len(conclusion_hits) * 5.0

    # Contains numbers (data density)
    numbers = re.findall(r'\d+(?:\.\d+)?', sentence)
    score += min(len(numbers), 5) * 2.0

    # Completeness bonus: sentence looks like a proper conclusion
    if len(sentence) >= 20 and sentence[-1] in '。！？':
        score += 3.0

    # Length preference: 30-200 chars is ideal
    slen = len(sentence)
    if 30 <= slen <= 200:
        score += 5.0
    elif slen > 200:
        score += 2.0

    # Penalise if it's just a figure title repeated
    if ft_norm and s_norm == ft_norm:
        score -= 50.0

    # Penalise lines that look like figure titles or source attributions
    if _FIGURE_LINE_RE.match(sentence.strip()):
        score -= 30.0
    if _SOURCE_LINE_RE.match(sentence.strip()):
        score -= 40.0

    return score


# ── paper_image selection ─────────────────────────────────────────────────────

def _choose_paper_image(
    fig_titles: list[str],
    parent_texts: list[str],
    question: str = '',
) -> str:
    """Pick the most relevant figure/table title for paper_image."""
    if not fig_titles:
        return ''
    combined_norm = _strip_punct(' '.join(parent_texts))
    q_norm = _strip_punct(question)

    best_title = fig_titles[0]
    best_score = -1.0
    for idx, title in enumerate(fig_titles):
        t_norm = _strip_punct(title)
        score = 0.0

        # Full title found in text → very strong signal
        if t_norm and t_norm in combined_norm:
            score += 1000.0

        # Title prefix (e.g. "图表8") found in text
        m = re.search(r'(?:图表?|表格?)\s*\d+', title)
        if m:
            prefix_norm = _strip_punct(m.group(0))
            if prefix_norm in combined_norm:
                score += 300.0

        # Token overlap with text
        for tok in _extract_tokens(title):
            w = min(len(tok), 12)
            if tok in combined_norm:
                score += w * (1.5 if tok.isdigit() else 4.0)
            # Question overlap is the primary signal
            if tok in q_norm:
                score += w * (3.0 if tok.isdigit() else 12.0)

        # Penalise titles with zero question keyword overlap
        q_toks = set(_extract_tokens(question))
        title_toks = set(_extract_tokens(title))
        overlap = q_toks & title_toks
        if q_toks and not overlap:
            score -= 50.0
        else:
            score += len(overlap) * 10.0

        score -= idx * 0.001  # tie-break: earlier = better
        if score > best_score:
            best_score = score
            best_title = title
    return best_title


# ── explanation sentence extraction ───────────────────────────────────────────

def _extract_explanation_text(
    parent_texts: list[str],
    figure_title: str,
    question: str,
    max_sentences: int = 2,
    max_chars: int = 200,
) -> str:
    """Extract 1-2 explanation sentences near the chosen figure from parent_text.

    Strategy:
      1. In each parent_text, locate the figure title
      2. Prefer sentences *before* the figure (conclusion → figure pattern)
      3. Then sentences *after* the figure
      4. Score every candidate sentence, pick top N
      5. If nothing good, return empty string (caller will fall back)
    """
    candidates: list[tuple[float, str]] = []

    for pt in parent_texts:
        if not pt:
            continue
        sentences = _split_sentences(pt)
        if not sentences:
            continue

        # Try to locate figure title position for proximity scoring
        ft_norm = _strip_punct(figure_title)
        pt_norm = _strip_punct(pt)
        ft_pos = pt_norm.find(ft_norm) if ft_norm else -1

        for sent in sentences:
            base_score = _score_sentence(sent, question, figure_title)

            # Proximity bonus if we know the figure position
            if ft_pos >= 0:
                sent_norm = _strip_punct(sent)
                sent_pos = pt_norm.find(sent_norm)
                if sent_pos >= 0:
                    dist = abs(sent_pos - ft_pos)
                    # sentences before figure get extra bonus (conclusion→figure pattern)
                    if sent_pos < ft_pos:
                        base_score += max(0, 50 - dist * 0.05)
                    else:
                        base_score += max(0, 30 - dist * 0.05)

            candidates.append((base_score, sent))

    if not candidates:
        return ''

    # Sort by score descending, pick top N
    candidates.sort(key=lambda x: -x[0])
    picked: list[str] = []
    total_len = 0
    seen_norm: set[str] = set()
    for _score, sent in candidates:
        s_norm = _strip_punct(sent)
        if s_norm in seen_norm:
            continue
        if total_len + len(sent) > max_chars and picked:
            break
        seen_norm.add(s_norm)
        picked.append(sent)
        total_len += len(sent)
        if len(picked) >= max_sentences:
            break

    return ''.join(picked)


# ── normalise path ────────────────────────────────────────────────────────────

def _to_relative_path(raw_path: str) -> str:
    _rel = raw_path
    idx = _rel.find('附件5')
    if idx >= 0:
        _rel = './' + _rel[idx:].replace('\\', '/')
    return _rel


# ── LLM-based reference extraction ────────────────────────────────────────────

_REF_SYSTEM_PROMPT = (
    '你是研报分析助手。请根据用户问题，从每篇文档的原文片段中提取最相关的参考信息。\n'
    '严格要求：\n'
    '1. text：从原文中摘录1-2句与问题最相关的完整句子，不超过200字。'
    '优先选择直接回答问题的事实性句子（含具体数字、结论），而非投资建议或概括性推荐语。'
    '尽量保持原文用词，但允许做以下轻微润色：'
    '①去掉开头的乱码符号（如孤立的百分号、数字碎片）；'
    '②如果两句话来自不同段落不连贯，用分号或逗号自然衔接；'
    '③如果句子明显被截断，可根据上下文补全结尾使其通顺。'
    '不要大幅改写或总结，保持原文的数据和措辞。'
    '【严禁】text中绝对不能包含以下内容：表格单元格数据（如"华润三九 益气清肺颗粒 13.60元"等表格行）、'
    '图表标题行（如"图表8：..."）、来源注释行（如"来源：国金证券研究所"）、OCR乱码碎片、大量连续数字预测值。'
    '只保留正文段落中有分析、结论、判断意义的自然语言完整句子。如果原文中全是表格数据没有正文句子，则text返回空字符串。\n'
    '2. paper_image：从给定的"可用图表标题"中选择与问题最相关的一个，只保留图表编号和标题（如"图表8：2025年国家医保目录新增7个中药产品"），'
    '去掉"来源：..."等后缀。如果没有相关图表则返回空字符串。不要编造标题。\n'
    '3. 只返回JSON数组，不输出其他内容。'
)


def _llm_extract_references(
    question: str,
    doc_list: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Call LLM once to extract text and paper_image for all documents.

    Returns {paper_path: {"text": ..., "paper_image": ...}}.
    On failure returns empty dict (caller falls back to regex).
    """
    # Build prompt describing each document
    doc_sections: list[str] = []
    for idx, doc in enumerate(doc_list, 1):
        fig_list = doc.get('fig_candidates') or []
        fig_str = '\n'.join(f'  - {ft}' for ft in fig_list) if fig_list else '  （无）'
        # Combine parent_texts and child_texts, pre-clean before sending to LLM
        ptexts = doc.get('parent_texts') or []
        ctexts = doc.get('child_texts') or []
        all_texts = ptexts + [ct for ct in ctexts if ct not in ptexts]
        cleaned_texts = [_clean_ocr_text(t) for t in all_texts]
        cleaned_texts = [t for t in cleaned_texts if len(t) > 15]
        combined = '\n'.join(cleaned_texts)[:1500]
        doc_sections.append(
            f'--- 文档{idx}: {doc["paper_path"]} ---\n'
            f'可用图表标题：\n{fig_str}\n'
            f'原文片段：\n{combined}\n'
        )

    user_prompt = (
        f'用户问题：{question}\n\n'
        + '\n'.join(doc_sections)
        + '\n请返回JSON数组，每个元素对应一篇文档（与上面顺序一致）：\n'
        '[\n'
        '  {"paper_path": "文档路径", "text": "提取的关键句子", "paper_image": "选中的图表标题或空字符串"}\n'
        ']'
    )

    try:
        from ..services.llm import llm_temperature, request_llm_text as _ref_llm_text
        # Use plain text mode (no json_mode) to avoid models that don't support json_object well
        raw_text = _ref_llm_text(
            _REF_SYSTEM_PROMPT, user_prompt,
            temperature=llm_temperature('ref_extract_temperature', 0.1),
        )
        # Robust JSON array extraction with multiple fallback strategies
        items: list[Any] = []
        # Strategy 1: direct parse
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, dict):
                for key in ('items', 'result', 'data', 'references'):
                    if isinstance(parsed.get(key), list):
                        items = parsed[key]
                        break
                if not items and 'paper_path' in parsed:
                    items = [parsed]
        except json.JSONDecodeError:
            pass
        # Strategy 2: extract first [...] with balanced brackets
        if not items:
            _depth = 0
            _start = -1
            for _ci, _ch in enumerate(raw_text):
                if _ch == '[':
                    if _depth == 0:
                        _start = _ci
                    _depth += 1
                elif _ch == ']':
                    _depth -= 1
                    if _depth == 0 and _start >= 0:
                        try:
                            parsed = json.loads(raw_text[_start:_ci + 1])
                            if isinstance(parsed, list):
                                items = parsed
                        except json.JSONDecodeError:
                            pass
                        break
        # Strategy 3: extract individual {...} objects
        if not items:
            for _m in re.finditer(r'\{[^{}]{10,}\}', raw_text):
                try:
                    obj = json.loads(_m.group())
                    if isinstance(obj, dict) and 'paper_path' in obj:
                        items.append(obj)
                except json.JSONDecodeError:
                    pass
        result: dict[str, dict[str, str]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            pp = str(item.get('paper_path', '')).strip()
            if not pp:
                continue
            result[pp] = {
                'text': str(item.get('text', '')).strip(),
                'paper_image': str(item.get('paper_image', '')).strip(),
            }
        # Also try matching by index if paper_path didn't match
        if not result and len(items) == len(doc_list):
            for doc, item in zip(doc_list, items):
                if isinstance(item, dict):
                    result[doc['paper_path']] = {
                        'text': str(item.get('text', '')).strip(),
                        'paper_image': str(item.get('paper_image', '')).strip(),
                    }
        return result
    except Exception as exc:
        import sys
        print(f'[Task3] LLM ref extraction failed: {exc}', file=sys.stderr)
        return {}


# ── reference text / image cleanup helpers ───────────────────────────────────

def _clean_ocr_text(raw: str) -> str:
    """Remove OCR noise, excessive whitespace, and stray symbols from chunk text."""
    s = str(raw).strip()
    # Normalize special Unicode spaces to ASCII space
    s = re.sub(r'[\u00a0\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', ' ', s)
    # Remove leading section numbers: （7）, (3), ①, 2.1.2, 1.3 etc.
    s = re.sub(r'^[（(]\d+[)）]\s*', '', s)
    s = re.sub(r'^\d+(\.\d+)+\.?\s*', '', s)
    s = re.sub(r'^[①②③④⑤⑥⑦⑧⑨⑩]\s*', '', s)
    # Remove block/box/garbled unicode characters — broad coverage
    s = re.sub(r'[\u0000-\u001f\u007f-\u009f'
               r'\u2022-\u2023\u2027\u2043\u204c-\u204d'  # bullets ‣•⁃⁌⁍
               r'\u2300-\u23ff'  # misc technical
               r'\u2500-\u259f'  # box drawing + block elements
               r'\u25a0-\u25ff'  # geometric shapes ■□▲△●○
               r'\u2600-\u26ff'  # misc symbols
               r'\u2700-\u27bf'  # dingbats
               r'\ue000-\uf8ff'  # PUA (Private Use Area) — custom PDF font glyphs
               r'\ufffd-\uffff'  # replacement chars
               r']+', '', s)
    # Remove "单位：%" or similar unit headers (short, before actual content)
    s = re.sub(r'单位[：:]\s*%', '', s)
    # Remove stray percent-number fragments like "%-6.67-10.71" or "%3.5-8.2"
    s = re.sub(r'%\s*-?\d+[\d.\-\s]*(?=\s|$|[◼█■])', '', s)
    # Remove orphan leading number-dash-number like "-6.67-10.71"
    s = re.sub(r'(?<=\s)-?\d+\.\d+-\d+\.\d+(?=\s)', '', s)
    s = re.sub(r'\s*\|\s*', ' ', s)
    # Remove bracket-enclosed OCR garbage with spaced-out letters (e.g. [T T a a b b l l e e ...])
    s = re.sub(r'\[[A-Za-z_ ]{6,}\]', '', s)
    # Remove scattered single letters/digits surrounded by spaces (OCR noise)
    s = re.sub(r'(?<!\w)\s+([a-zA-Z]\s+){2,}', ' ', s)
    # Remove stray isolated punctuation clusters like ") ) )"
    s = re.sub(r'(\s*[)\]}>）】]\s*){2,}', ' ', s)
    # Remove orphan number-dash-number fragments not part of sentences (e.g. "6.67-10.71")
    s = re.sub(r'(?<![.\d\u4e00-\u9fff])\d+\.\d+-\d+\.\d+(?![%\u4e00-\u9fff])', '', s)
    # Remove table-header/cell fragments
    s = re.sub(r'序\s*号\s*公\s*司\s*药\s*品.*?(?:注\s*册\s*分\s*类|用\s*法\s*用\s*量|适\s*应\s*症)', '', s)
    s = re.sub(r'用\s*法\s*用\s*量\s*注\s*册\s*分\s*类', '', s)
    s = re.sub(r'注\s*册\s*分\s*类', '', s)
    # Remove table data rows: "公司名 产品名 价格(每袋装Xg..." patterns
    s = re.sub(r'[\u4e00-\u9fff]{2,6}\s+[\u4e00-\u9fff]{2,10}\s+\d+\.\d+元[^。；]*', '', s)
    # Remove inline product+price: "华润三九益气清肺颗粒13.60元..." (no space separator)
    s = re.sub(r'[\u4e00-\u9fff]{2,6}[\u4e00-\u9fff]{2,10}\d+\.\d+元[^。；]{0,60}', '', s)
    # Remove table data rows: "公司名 数字 数字% 数字%" (company + multiple numbers)
    s = re.sub(r'[\u4e00-\u9fff]{2,6}\s+-?\d+[\d.]*%?\s+-?\d+[\d.]*%?(?:\s+-?\d+[\d.]*%?)+', '', s)
    # Remove price/dosage fragments like "13.60元(每袋装15g(相当于饮片52g"
    s = re.sub(r'\d+\.\d+元\([^)]*\)', '', s)
    # Handle unmatched parens: "13.60元(每袋装15g(相当于饮片52g 益气" (no closing paren)
    s = re.sub(r'\d+\.\d+元[（(][^。；）)]{0,80}', '', s)
    # Remove "图表N：..." inline figure captions (with or without source attribution)
    s = re.sub(r'图表?\s*\d+\s*[：:][^。；\n]*', '', s)
    # Remove "[表格]" placeholders
    s = re.sub(r'\[表格\]', '', s)
    # Remove "来源：xxx研究所" fragments
    s = re.sub(r'来源[：:][^。；\n]{0,50}(?:研究所|证券)', '', s)
    # Remove vertical OCR noise: sequences of single chars separated by spaces (e.g. "序 号 公 司")
    s = re.sub(r'(?:[\u4e00-\u9fff]\s){4,}[\u4e00-\u9fff]', '', s)
    # Remove OCR table cell noise: scattered digits/letters with spaces like "2 0 2 5 年"
    s = re.sub(r'(?:\d\s){3,}\d', '', s)
    # Remove $$...$$ OCR garbage
    s = re.sub(r'\$\$[^$]*\$\$', '', s)
    # Remove report metadata: "证券研究报告", "华安证券研究所" etc.
    s = re.sub(r'(?:[\u4e00-\u9fff]{2,4})?(?:证券|投资)?(?:研究报告|研究部|研究院|研究所|研报)[会]?', '', s)
    # Remove research firm slogans: "华安研究 拓展投资价值" etc.
    s = re.sub(r'[\u4e00-\u9fff]{2,4}研究\s+[\u4e00-\u9fff]{2,8}', '', s)
    # Remove analyst metadata: "分析师: 黄婧婧 执业证书编号：S0270522030001 电话：邮箱："
    s = re.sub(r'分析师[：:]\s*[\u4e00-\u9fff]{2,4}(?:\s*执业证书编号[：:]\s*S?\d+)?(?:\s*电话[：:])?(?:\s*邮箱[：:])?', '', s)
    s = re.sub(r'执业证书编号[：:]\s*S?\d+', '', s)
    # Remove chart axis data: "@0%5%10%15%20%" or similar
    s = re.sub(r'@?\d+%(?:\d+%)+', '', s)
    # Remove report category labels: "行业跟踪报告 行业研究" "行业深度报告" etc.
    s = re.sub(r'(?:行业|公司|个股)(?:跟踪|深度|专题|点评)?(?:报告|研究|周报|月报)', '', s)
    # Remove research institute page headers (wlzq.cn 第3页共4页 etc.)
    s = re.sub(r'[\w.]+\.cn\s*第?\d*页?共?\d*页?', '', s)
    s = re.sub(r'第\d+页共\d+页', '', s)
    # Remove year+letter codes: "2024A", "2025E" standalone
    s = re.sub(r'\b20\d{2}[A-Fa-f]\b', '', s)
    # Remove isolated metric labels with units: "同比增速(%)" "应付票净 (%)" etc.
    s = re.sub(r'[\u4e00-\u9fff]{2,6}\s*[（(][%％万亿元百千][)）]', '', s)
    # Remove financial table headers: "利润表（百万元）资产负债（百万元）2024 2025E..."
    s = re.sub(r'(?:利润表|资产负债表?|现金流量表|资产负债)\s*[（(][^)）]*[)）]', '', s)
    # Remove projection year sequences: "2024E 2025E 2026E 2027E" or "2024 2025E 2026E"
    s = re.sub(r'(?:20\d{2}[EeFf]?\s+){2,}20\d{2}[EeFf]?', '', s)
    # Remove any run of 3+ consecutive English words (with optional numbers/punctuation)
    # This broadly catches English OCR fragments like "cosmetics, down, downrevenue billion RMB..."
    s = re.sub(r'(?:[a-zA-Z]{2,}[\s,;，；.%\d]*){3,}[a-zA-Z%\d.]*', '', s, flags=re.IGNORECASE)
    # Remove sequences of space-separated short financial terms (table column headers)
    s = re.sub(r'(?:(?:净利润|流动负债|非流动负债|负债合计|少数股东损益|长期借款|归属?母?公司|'
               r'其他流动负债|总资产|总负债|营业收入|营业成本|管理费用|销售费用|财务费用|'
               r'资产总计|股东权益|未分配利润|应收账款|存货|主要财务比率|摊薄)\s*)+', '', s)
    # Remove stray standalone numbers/percentages not in a sentence context
    s = re.sub(r'(?<=[。，；\s])\s*\d+\.?\d*%?\s*(?=[，。；\s]|$)', ' ', s)
    # Remove repeated punctuation and mixed garbled sequences
    s = re.sub(r'[；;,，。.]{2,}', '，', s)
    # Remove spaced double punctuation: "。 。" "， 。" "， ，" etc.
    s = re.sub(r'[。，；！？]\s*[。，；！？]', '。', s)
    # Remove very short fragment (<8 chars) between two sentence endings
    s = re.sub(r'。[^。；！？]{1,8}。', '。', s)
    # Collapse whitespace
    s = re.sub(r'\s{2,}', ' ', s)
    # Remove trailing table column headers (short space-separated Chinese terms at end)
    s = re.sub(r'\s+(?:[\u4e00-\u9fff]{2,6}\s+){2,}[\u4e00-\u9fff]{2,6}\s*$', '', s)
    # Remove leading garbage (non-Chinese, non-digit, non-letter) up to first real content
    s = re.sub(r'^[^a-zA-Z0-9\u4e00-\u9fff]{3,}', '', s)
    # Remove leading fragments that start mid-sentence (no Chinese char in first 5 chars)
    s = s.lstrip('% ，。；、·-')
    s = s.strip()
    # Remove dense comma-separated number sequences (EPS/PE/YOY prediction lists)
    s = re.sub(r'(?:\d+\.?\d*[%X元亿万]*[,，、\s]+){4,}\d+\.?\d*[%X元亿万]*', '', s)
    # Remove "分别为A, B, C" prediction patterns
    s = re.sub(r'分别[为是约]?[^。；]{0,6}?(?:\d+\.?\d*[%X元亿万]*[,，、\s]*){3,}', '', s)
    # Remove "对应PE分别为20X, 17X, 15X" style
    s = re.sub(r'对应[^。；]{0,10}?(?:\d+\.?\d*[%X倍]*[,，、\s]*){2,}', '', s)
    # Remove YOY sequences like "+21.4%, +17.3%, +15.0%"
    s = re.sub(r'(?:[+\-]\d+\.?\d*%[,，、\s]*){2,}', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    # ── Sentence boundary enforcement ──
    # Trim leading incomplete fragment: if text starts mid-sentence (e.g. "量潜力，如华润...")
    # detect by checking if first segment before a comma/period is very short (<6 CJK chars)
    if s:
        _lead_m = re.match(r'^([\u4e00-\u9fff0-9A-Za-z%％]{1,5})[，,；]', s)
        if _lead_m:
            # Very short leading fragment before first comma — likely truncated start
            s = s[_lead_m.end():].lstrip()
    # Trim trailing incomplete fragment: ensure text ends near a sentence boundary
    if s and not re.search(r'[。；！？]$', s):
        # Find the last sentence-ending punctuation
        _last_end = -1
        for _m in re.finditer(r'[。；！？]', s):
            _last_end = _m.start()
        if _last_end > len(s) * 0.3:
            s = s[:_last_end + 1]
        elif s and not s[-1] in '，,、':
            # No good sentence end found but text is substantial — add period
            s = s.rstrip('，,、；; ') + '。'
    # Remove trailing short garbled fragments: space + ≤4 mixed chars at end (OCR noise)
    s = re.sub(r'\s+[\d\u4e00-\u9fff]{1,4}$', '', s)
    s = s.strip()
    # Final density check: if >35% of chars are digits/percent/minus, likely table data
    if s:
        _num_chars = sum(1 for c in s if c in '0123456789.%-')
        if _num_chars / len(s) > 0.35:
            return ''
    return s


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the nearest sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    # Find the last sentence-ending punctuation before max_chars
    truncated = text[:max_chars]
    for delim in ['。', '；', '，', '、', ' ']:
        pos = truncated.rfind(delim)
        if pos > max_chars // 3:  # at least 1/3 of the text
            return truncated[:pos + 1].rstrip()
    return truncated.rstrip()


def _clean_paper_image(title: str) -> str:
    """Keep only the figure/table title, strip source info and trailing content."""
    if not title:
        return ''
    # Remove everything after "来源：" / "来源:" / "Source:"
    title = re.split(r'\s*来源[：:]', title, maxsplit=1)[0]
    # Remove vertical OCR noise (single chars separated by spaces)
    title = re.sub(r'(?:[\u4e00-\u9fff]\s){4,}[\u4e00-\u9fff]', '', title)
    title = re.sub(r'(?:\d\s){3,}\d', '', title)
    # Handle multiple figure titles: split on whitespace before "图表N：" / "图N：" / "表N："
    _FIG_BOUNDARY_RE = re.compile(r'\s+(?=(?:图表?|表格?|图片)\s*\d+\s*[：:])')
    parts = _FIG_BOUNDARY_RE.split(title.strip())
    cleaned_parts: list[str] = []
    _FIG_EXTRACT_RE = re.compile(
        r'((?:图表?|表格?|图片)\s*\d+\s*[：:]\s*[\u4e00-\u9fff][\u4e00-\u9fff0-9A-Za-z、，（）()\s]{2,40}?)'
        r'(?:\s{2,}|\s+\d\s|\s+[a-zA-Z]\s|$)'
    )
    # Regex to truncate at table-like content (column headers / data rows)
    _TABLE_NOISE_RE = re.compile(
        r'\s+(?:毛利率|同比|公司|收入|营收|净利|净利润|归母|扣非|yoy|pct|占比|变动|单位'
        r'|考核|解除|限售|净资产|增长率|复合|年度|基准|期间|合计|小计|百万|万元|亿元)'
        r'|\s+\d+[\d.,%]*\s'
        r'|\s+[a-zA-Z]{2,}\s'
    )
    # General pattern: 3+ space-separated short Chinese segments → table column headers
    _COL_HEADER_RE = re.compile(
        r'(\s+[\u4e00-\u9fff]{2,6}){3,}'
    )
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _FIG_EXTRACT_RE.match(part)
        if m:
            cleaned_parts.append(m.group(1).strip())
        elif re.match(r'(?:图表?|表格?|图片)\s*\d+\s*[：:]', part):
            # Truncate at first sign of table data
            m2 = _TABLE_NOISE_RE.search(part)
            if not m2:
                m2 = _COL_HEADER_RE.search(part)
            cleaned = part[:m2.start()].strip() if m2 else part.strip()
            cleaned_parts.append(cleaned.rstrip('，, '))
    if cleaned_parts:
        # Only keep the first (most relevant) figure title
        result = cleaned_parts[0]
        # Strip trailing non-CJK/non-standard chars (OCR noise)
        result = re.sub(r'[^\u4e00-\u9fff0-9A-Za-z%％）)]+$', '', result)
        # Remove PUA characters (custom PDF font glyphs)
        result = re.sub(r'[\ue000-\uf8ff]+', '', result)
        # Remove trailing garbled fragments: space + digit(s) + ≤3 CJK chars (e.g. " 2中运喉")
        result = re.sub(r'\s+\d[\u4e00-\u9fff]{1,3}$', '', result)
        # Remove trailing single CJK char after space (truncated word)
        result = re.sub(r'\s+[\u4e00-\u9fff]$', '', result)
        # Remove trailing incomplete fragment: space + ≤4 chars that look like noise
        result = re.sub(r'\s+[^\u4e00-\u9fff\d]{0,2}[\u4e00-\u9fff]{1,2}$', '', result)
        result = result.rstrip('，,、 ')
        # Final coherence: if title ends mid-word (last CJK char is a common suffix-only char),
        # trim back to last meaningful boundary
        if result and result[-1] in '的了着过和与及或而在从到对':
            result = result[:-1].rstrip()
        return result
    # Only return raw title if it looks like a real figure/table title
    _raw = title.strip()
    _raw = re.sub(r'[\ue000-\uf8ff]+', '', _raw)
    if re.match(r'(?:图表?|表格?|图片)\s*\d+\s*[：:]', _raw):
        # Apply same trailing noise cleanup to raw fallback
        _raw = re.sub(r'\s+\d[\u4e00-\u9fff]{1,3}$', '', _raw)
        _raw = re.sub(r'\s+[\u4e00-\u9fff]$', '', _raw)
        _raw = _raw.rstrip('，,、 ')
        return _raw
    return ''


# ── main reference formatter ──────────────────────────────────────────────────

def _format_references(refs: list[dict[str, Any]], question: str = '') -> list[dict[str, Any]]:
    """Convert internal references to 表5 schema using anchor-based logic.

    For each document:
      1. Keep top-3 highest-scoring chunks
      2. Choose best figure title as paper_image (question/text relevance)
      3. Extract nearby explanation sentences from parent_text as text
      4. Fall back to child_text evidence if no good explanation found
    """
    from collections import OrderedDict

    # ── Step 1: group by document ─────────────────────────────────────────
    merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for ref in refs:
        raw_path = str(ref.get('paper_path') or ref.get('doc_path') or '').strip()
        if not raw_path:
            continue
        paper_path = _to_relative_path(raw_path)

        fig_titles = ref.get('figure_titles') or []
        if isinstance(fig_titles, str):
            try:
                fig_titles = json.loads(fig_titles)
            except Exception:
                fig_titles = []

        if paper_path not in merged:
            merged[paper_path] = {
                'paper_path': paper_path,
                'chunks': [],
                'all_fig_titles': [],
                'seen_figs': set(),
            }
        bucket = merged[paper_path]
        bucket['chunks'].append({
            'text': str(ref.get('text', '')).strip(),
            'parent_text': str(ref.get('parent_text', '')).strip(),
            'score': ref.get('score', 0),
            'fig_titles': [ft for ft in fig_titles if ft],
        })
        for ft in fig_titles:
            if ft and ft not in bucket['seen_figs']:
                bucket['seen_figs'].add(ft)
                bucket['all_fig_titles'].append(ft)

    # ── Step 2: prepare per-document data ─────────────────────────────────
    doc_list: list[dict[str, Any]] = []
    for bucket in merged.values():
        top_chunks = bucket['chunks'][:5]
        parent_texts = [c['parent_text'] for c in top_chunks if c.get('parent_text')]
        child_texts = [c['text'] for c in top_chunks if c.get('text')]
        fig_candidates: list[str] = []
        seen_fc: set[str] = set()
        for c in top_chunks:
            for ft in c.get('fig_titles', []):
                if ft and ft not in seen_fc:
                    seen_fc.add(ft)
                    fig_candidates.append(ft)
        if not fig_candidates:
            fig_candidates = list(bucket['all_fig_titles'])
        max_score = max((c.get('score', 0) for c in top_chunks), default=0)
        doc_list.append({
            'paper_path': bucket['paper_path'],
            'parent_texts': parent_texts,
            'child_texts': child_texts,
            'fig_candidates': fig_candidates,
            'max_score': max_score,
        })

    # ── Step 3: try LLM-based extraction (one call for all docs) ──────────
    llm_results: dict[str, dict[str, str]] = {}
    if llm_available() and doc_list:
        llm_results = _llm_extract_references(question, doc_list)

    # ── Step 4-5: build output with strict key ordering ───────────────────
    out: list[dict[str, Any]] = []
    for doc in doc_list:
        pp = doc['paper_path']
        llm_r = llm_results.get(pp, {})
        # Determine text: LLM extraction first (near-original), fallback to cleaned chunks
        text = _clean_ocr_text(llm_r.get('text', ''))
        if not text:
            raw_texts = doc.get('child_texts') or doc.get('parent_texts') or []
            if raw_texts:
                parts: list[str] = []
                seen_parts: set[str] = set()
                for rt in raw_texts[:3]:
                    rt = _clean_ocr_text(rt)
                    if not rt or rt in seen_parts:
                        continue
                    # Skip very short fragments (likely table header/noise)
                    if len(rt) < 15:
                        continue
                    seen_parts.add(rt)
                    parts.append(rt)
                    if len(parts) >= 2:
                        break
                text = '；'.join(parts)
        # Re-apply OCR cleanup after joining (catches cross-fragment noise patterns)
        if text:
            text = _clean_ocr_text(text)
        text = _truncate_at_sentence(text, 300)
        # Post-clean: strip leading stray symbols/numbers and trailing truncated fragments
        if text:
            text = re.sub(r'^[%％+\-\d.,，、\s]{1,10}(?=[^\d%])', '', text)  # leading stray like "%4%，"
            text = re.sub(r'[，,；]\s*$', '。', text)  # end with period instead of comma
            # Remove ；-separated fragments that are too short (<10 CJK chars — likely noise)
            # and deduplicate segments with >50% overlap
            if '；' in text:
                _segs = text.split('；')
                _good_segs: list[str] = []
                for seg in _segs:
                    seg = seg.strip()
                    if len(re.findall(r'[\u4e00-\u9fff]', seg)) < 10:
                        continue
                    # Check overlap with already-accepted segments
                    _is_dup = False
                    for existing in _good_segs:
                        _shorter = seg if len(seg) <= len(existing) else existing
                        if _shorter and _shorter in existing or _shorter in seg:
                            _overlap = len(set(seg) & set(existing)) / max(len(set(seg)), 1)
                            if _overlap > 0.6:
                                _is_dup = True
                                break
                    if not _is_dup:
                        _good_segs.append(seg)
                text = '；'.join(_good_segs) if _good_segs else text

        # Extract table title from text → paper_image (if paper_image is empty)
        _table_title_extracted = ''
        _tbl_m = re.search(
            r'((?:\d{4}[年、，]\s*)*\d{4}[年Q]\S{0,20}?'
            r'[\u4e00-\u9fff][\u4e00-\u9fff\d、，与和及()（）]{4,40}?'
            r'(?:情况|概览|统计|数据|对比|分析|趋势|变动|增速|走势|占比|结构|规模|排[名行]))'
            r'\s*(?:[\u4e00-\u9fff]{2,6}\s+)*[\u4e00-\u9fff]{2,6}\s*$', text)
        if _tbl_m:
            _table_title_extracted = _tbl_m.group(1).strip()
            text = text[:_tbl_m.start()].strip()
            text = _truncate_at_sentence(text, 300) if text else ''

        # Determine paper_image: LLM first, then table title from text, then regex
        paper_image = llm_r.get('paper_image', '').strip()
        if not paper_image and _table_title_extracted:
            paper_image = _table_title_extracted
        if not paper_image:
            paper_image = _choose_paper_image(
                doc['fig_candidates'], doc['parent_texts'], question)
        paper_image = _clean_paper_image(paper_image)

        # Build entry with strict key ordering: paper_path → text → paper_image
        entry: dict[str, Any] = {'paper_path': pp}
        entry['text'] = text or ''
        entry['paper_image'] = paper_image or ''
        entry['_max_score'] = doc.get('max_score', 0)
        out.append(entry)
    # Drop references with empty or low-quality text
    def _is_quality_text(t: str) -> bool:
        t = t.strip()
        if not t:
            return False
        # Boilerplate disclaimers / legal notices
        if re.search(r'(请务必参阅|信息披露和法律声明|免责声明|风险提示|投资评级说明|分析师声明)', t):
            return False
        # Too short and no sentence punctuation → table header garbage
        if len(t) < 30 and not re.search(r'[。；！？]', t):
            return False
        # Mostly space-separated short fragments (table headers like "利润总额 净利润 益归 属母公司")
        _words = t.split()
        if len(_words) >= 2 and all(len(w) <= 6 for w in _words) and not re.search(r'[，、]', t):
            return False
        # High English/ASCII ratio → likely English table data or OCR noise
        _en_chars = sum(1 for c in t if c.isascii() and c.isalpha())
        if len(t) > 10 and _en_chars / len(t) > 0.3:
            return False
        # Contains obvious table header keywords without sentence structure
        if re.search(r'(?:百万元|同比增减|利润表|资产负债)', t) and not re.search(r'[。]', t):
            return False
        # Table column headers: multiple % or metric acronyms (e.g. "增长率YoY% 毛利率% ROE% EPS")
        _pct_count = t.count('%') + t.count('％')
        if _pct_count >= 3:
            return False
        _acronym_count = len(re.findall(r'(?:YoY|EPS|ROE|P/E|P/B|PE|PB|CAGR|EBITDA|EBIT)', t))
        if _acronym_count >= 1 and len(t) < 60:
            return False
        # Short text with unit markers like (元)(万元) and no real sentence → table fragment
        if len(t) < 50 and re.search(r'\([元万亿百千]\w{0,2}\)', t) and not re.search(r'[，,].*[。]', t):
            return False
        # Financial table header terms (space-separated) with trailing period only
        _fin_terms = len(re.findall(r'(营业总?收入|净利润|毛利率|增长率|应付账款|主要财务指标|财务预测)', t))
        if _fin_terms >= 2 and len(t) < 80 and not re.search(r'[，,]', t):
            return False
        return True
    _all_entries = list(out)
    out = [e for e in out if _is_quality_text(e.get('text', ''))]
    # Fallback: if all filtered out but we had candidates, keep top-scoring with quality text
    if not out and _all_entries:
        _all_entries.sort(key=lambda e: e.get('_max_score', 0), reverse=True)
        for _fb in _all_entries:
            if _fb.get('text', '').strip() and _is_quality_text(_fb.get('text', '')):
                out = [_fb]
                break
        # If still empty (all text was garbage), keep top entry with paper_path only
        if not out:
            out = [{'paper_path': _all_entries[0]['paper_path'], 'text': '', 'paper_image': _all_entries[0].get('paper_image', '')}]
    # Keep at most 3 most relevant references
    out.sort(key=lambda e: e.get('_max_score', 0), reverse=True)
    out = out[:3]
    # ── Final sanitization pass ──
    _PUA_RE = re.compile(r'[\ue000-\uf8ff\u0000-\u001f\u007f-\u009f\ufffd]+')
    _TRAILING_NOISE_RE = re.compile(r'\s+[\d\u4e00-\u9fff]{1,4}$')
    for e in out:
        e.pop('_max_score', None)
        # Clean all string fields: PUA chars, control chars
        for key in ('text', 'paper_image', 'paper_path'):
            val = e.get(key, '')
            if val:
                val = _PUA_RE.sub('', val).strip()
                e[key] = val
        # ── Deduplicate repeated sentence sequences within text ──
        _txt = e.get('text', '')
        if _txt and len(_txt) > 60:
            _sentences = re.split(r'(?<=[。；！？])', _txt)
            _seen_sents: list[str] = []
            _deduped: list[str] = []
            for _s in _sentences:
                _s_norm = re.sub(r'\s+', '', _s).strip()
                if not _s_norm:
                    continue
                if _s_norm in _seen_sents:
                    continue  # skip duplicate sentence
                _seen_sents.append(_s_norm)
                _deduped.append(_s)
            _txt = ''.join(_deduped).strip()
            e['text'] = _txt
        # ── Remove isolated short table-cell fragments ──
        # Only remove truly short standalone fragments (≤8 chars, not a continuation)
        _txt = e.get('text', '')
        if _txt:
            # Remove trailing standalone fragment: period + ≤8 non-punct chars + period at end
            _txt = re.sub(r'。[^。；！？，,]{1,8}。$', '。', _txt)
            # Remove trailing comma + very short fragment (≤5 chars) — likely truncated noise
            _txt = re.sub(r'[，,][^。；！？]{1,5}。$', '。', _txt)
            _txt = re.sub(r'。{2,}', '。', _txt)
            e['text'] = _txt.strip()
        # Ensure text ends at sentence boundary or with period
        _txt = e.get('text', '')
        if _txt and not re.search(r'[。；！？]$', _txt):
            _txt = _txt.rstrip('，,、；; ') + '。'
            e['text'] = _txt
        # Ensure paper_image is clean: remove trailing noise
        _pi = e.get('paper_image', '')
        if _pi:
            _pi = _TRAILING_NOISE_RE.sub('', _pi).rstrip('，,、 ')
            e['paper_image'] = _pi
    # Drop entries where both text and paper_image are empty after final cleanup
    out = [e for e in out if e.get('text', '').strip() or e.get('paper_image', '').strip()]
    return out


# ── main export ───────────────────────────────────────────────────────────────

def _process_one_question(
    question: QuestionRecord,
    result_dir: Path,
    reply_builder: ReplyBuilder,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Process a single question (may have multiple turns). Thread-safe."""
    qid = str(question['id'])
    turns = list(question['questions'])
    history: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    sql_fragments: list[str] = []
    image_index = 1

    for turn in turns:
        prompt = str(turn.get('Q', '')).strip()
        user_msg = {
            'id': f'{qid}-user-{len(history) + 1}',
            'role': 'user',
            'content': prompt,
        }
        history_with_user = history + [user_msg]
        reply = reply_builder(prompt, history_with_user, render_mode='batch_export', fast_mode=True)

        images: list[str] = []
        # save primary chart
        if reply.get('chart'):
            img_path = result_dir / f'{qid}_{image_index}.jpg'
            images.append(save_chart_image(reply['chart'], img_path))
            image_index += 1
        # save additional charts from multi-intent replies
        for extra_chart in (reply.get('charts') or [])[1:]:
            img_path = result_dir / f'{qid}_{image_index}.jpg'
            images.append(save_chart_image(extra_chart, img_path))
            image_index += 1
        # per-subtask charts
        for task_result in (reply.get('taskResults') or []):
            if task_result.get('chart'):
                img_path = result_dir / f'{qid}_{image_index}.jpg'
                images.append(save_chart_image(task_result['chart'], img_path))
                image_index += 1

        # Build answer payload with strict key ordering: content → image → references
        raw_content = str(reply.get('content', ''))
        answer_payload: dict[str, Any] = {
            'content': _normalize_batch_text(raw_content),
            'image': images if images else [],
            'references': _format_references(reply.get('references') or [], prompt),
        }

        answers.append({'Q': prompt, 'A': answer_payload})

        if reply.get('sql'):
            sql_fragments.append(str(reply['sql']))

        history = history_with_user + [reply]

    sql_text = '\n'.join(_unique_keep_order(sql_fragments)) if sql_fragments else '无'
    answer_json = json.dumps(answers, ensure_ascii=False, indent=4)
    question_json = json.dumps(turns, ensure_ascii=False)

    wb_row = {
        '编号': qid,
        '问题': question_json,
        'SQL查询语法': sql_text,
        '回答': answer_json,
    }
    export_rec = {
        'id': qid,
        'questionType': question.get('questionType', ''),
        'question': turns,
        'sql': sql_text,
        'answer': answers,
    }
    return wb_row, export_rec


def export_task3_results(
    base_dir: Path,
    reply_builder: ReplyBuilder,
    question_file: Path | None = None,
) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from config import CONFIG

    questions = load_task3_questions(base_dir, question_file)
    result_dir = base_dir / 'result'
    result_dir.mkdir(parents=True, exist_ok=True)
    workbook_path = base_dir / 'result_3.xlsx'

    max_workers = CONFIG.get('parallel', {}).get('max_workers', 3)

    # Process questions in parallel (turns within a question stay sequential)
    futures_map: dict[Any, int] = {}
    results_by_idx: dict[int, tuple[dict, dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for idx, question in enumerate(questions):
            fut = pool.submit(_process_one_question, question, result_dir, reply_builder)
            futures_map[fut] = idx
        for fut in as_completed(futures_map):
            idx = futures_map[fut]
            try:
                results_by_idx[idx] = fut.result()
            except Exception as exc:
                qid = str(questions[idx].get('id', f'Q{idx}'))
                print(f'[Task3] ERROR processing {qid}: {exc}', file=__import__('sys').stderr)
                results_by_idx[idx] = (
                    {'编号': qid, '问题': '', 'SQL查询语法': '无', '回答': json.dumps([{'Q': '', 'A': {'content': f'处理失败: {exc}'}}], ensure_ascii=False)},
                    {'id': qid, 'questionType': '', 'question': [], 'sql': '无', 'answer': []},
                )

    # Reassemble in original order
    workbook_rows = [results_by_idx[i][0] for i in range(len(questions))]
    export_records = [results_by_idx[i][1] for i in range(len(questions))]

    _build_task3_workbook(workbook_rows, workbook_path)
    return {
        'questionFile': str(question_file or find_task3_question_file(base_dir)),
        'workbookPath': str(workbook_path),
        'workbookUrl': '/api/task3/result-file',
        'resultDir': str(result_dir),
        'questionCount': len(export_records),
        'records': export_records,
    }
