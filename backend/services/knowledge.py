"""Knowledge base: PDF ingestion, semantic chunking, hybrid BM25+vector retrieval."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import zlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import httpx
import jieba

try:
    import numpy as np
    _NUMPY_OK = True
except ImportError:
    np = None  # type: ignore[assignment]
    _NUMPY_OK = False

import jieba.analyse
import jieba.posseg as pseg
from rank_bm25 import BM25Okapi

try:
    import pdfplumber
    _PDFPLUMBER_OK = True
except ImportError:
    _PDFPLUMBER_OK = False

try:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCR
    _RAPIDOCR_OK = True
except ImportError:
    _RAPIDOCR_OK = False

_rapidocr_instance: Any | None = None
_rapidocr_lock = threading.Lock()


def _get_rapidocr() -> Any | None:
    global _rapidocr_instance
    if not _RAPIDOCR_OK:
        return None
    with _rapidocr_lock:
        if _rapidocr_instance is None:
            try:
                _rapidocr_instance = _RapidOCR()
            except Exception as e:
                print(f'[KB] RapidOCR init error: {e}', file=sys.stderr)
                return None
        return _rapidocr_instance

# Warm up jieba in the main thread
try:
    jieba.lcut('初始化')
except Exception:
    pass

from ..core.context import ATTACHMENT5_DIR, KB_DOCS_DIR
from ..core.kb_store import kb_conn

# ── tunables (loaded from config.json → knowledge_base) ─────────────────────
def _kb_cfg() -> dict[str, Any]:
    try:
        from config import CONFIG
        return CONFIG.get('knowledge_base', {})
    except Exception:
        return {}

_kbc = _kb_cfg()
CHILD_CHUNK_MIN  = int(_kbc.get('child_chunk_min', 100))
CHILD_CHUNK_MAX  = int(_kbc.get('child_chunk_max', 300))
PARENT_CHUNK_MAX = int(_kbc.get('parent_chunk_max', 900))
CHUNK_OVERLAP    = int(_kbc.get('chunk_overlap', 50))
TOP_K            = int(_kbc.get('top_k', 5))
_BM25_WEIGHT     = float(_kbc.get('bm25_weight', 0.3))
_VECTOR_WEIGHT   = float(_kbc.get('vector_weight', 0.7))
_RRF_K           = int(_kbc.get('rrf_k', 60))
_RERANK_CANDIDATES = int(_kbc.get('rerank_candidates', 30))
_EMBED_MAX_CHARS = int(_kbc.get('embed_max_chars', 2000))
_EMBED_BATCH_SIZE = int(_kbc.get('embed_batch_size', 64))
_OCR_DPI         = int(_kbc.get('ocr_dpi', 200))
_INDEX_CONCURRENCY = int(_kbc.get('index_concurrency', 4))
del _kbc
# Legacy aliases (used in some code paths)
CHUNK_MIN = CHILD_CHUNK_MIN
CHUNK_MAX = CHILD_CHUNK_MAX

# ── global BM25 index ─────────────────────────────────────────────────────────
_bm25_lock = threading.Lock()
_index_lock = threading.Semaphore(_INDEX_CONCURRENCY)
_bm25_index: BM25Okapi | None = None
_bm25_chunk_ids: list[int] = []

# ── in-memory vector store ────────────────────────────────────────────────────
_vec_lock = threading.Lock()
_vec_chunk_ids: list[int] = []
_vec_matrix: list[list[float]] = []   # parallel to _vec_chunk_ids


# ══════════════════════════════════════════════════════════════════════════════
# Tokenisation
# ══════════════════════════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """jieba cut, keep Han / alphanumeric tokens."""
    return [t for t in jieba.lcut(text) if re.search(r'[\u4e00-\u9fff\w]', t)]


# ══════════════════════════════════════════════════════════════════════════════
# PDF extraction  (pdfplumber word-bbox column-aware + fitz fallback)
# ══════════════════════════════════════════════════════════════════════════════

# Clean PDF form-field annotation tags like [Table_StockInfo], [Ta b事le_件Su：m公ma司ry]
_TAG_RE = re.compile(
    r'\[(?:T\s*a\s*b\s*(?:l\s*e\s*_|[^\]]{0,5}le_)[^\]]{0,60}|Table_[^\]]{1,40})\]',
    re.IGNORECASE,
)
# Garbled interleaved CJK+ASCII annotation tags like [利T润ab表l（e百_P万r元of）i tDet2a02i4lA]
_GARBLED_TAG_RE = re.compile(
    r'\[[^\]]*(?:[\u4e00-\u9fff][A-Za-z]|[A-Za-z][\u4e00-\u9fff])[^\]]{0,80}\]'
)
_NOISE_RE = re.compile(
    r'^\s*(\d{1,3}|第\s*\d+\s*页|page\s*\d+|[-─—]{3,}|·{3,}|\.{5,}'
    r'|请务必阅读.*|免责声明.*|分析师声明.*|证券研究报告.*|扫码获取.*'
    r'|敬请参阅.*|特别声明.*|用使司公限有理管.*|此告报仅供.*)\s*$',
    re.IGNORECASE | re.MULTILINE,
)
_BLANK_RE = re.compile(r'\n{3,}')
# Sidebar x-boundary: right column in research reports starts ~x=390
_SIDEBAR_X = 390
# Pure-number line: digits, spaces, %, ., +/-/× signs, brackets — no meaningful text
_PURE_NUM_RE = re.compile(r'^[\d\s%.,+\-×÷=<>()（）\[\]【】：:·/\\]{4,}$')

_HEADING_RE = re.compile(r'^(?:第[一二三四五六七八九十百\d]+[章节部分]|[一二三四五六七八九十百]+[、.．]|\d+(?:\.\d+){0,3}\s+)[^\n]{1,40}$')
_ABSTRACT_HINT_RE = re.compile(r'(年度报告摘要|半年度报告摘要|季度报告摘要|报告摘要)')
_ANALYTICAL_QUERY_RE = re.compile(r'(为什么|原因|分析|如何|怎么看|影响|驱动|归因|趋势|逻辑|展望|对比)')
_EXACT_QUERY_RE = re.compile(r'(多少|几|是什么|有无|有没有|谁|哪家|日期|时间|目标价|评级|EPS|PE|PB|ROE|营收|收入|净利润|现金流|股票代码|代码)')

_DOC_KIND_HINTS: dict[str, tuple[str, ...]] = {
    'financial_report': ('年度报告', '半年度报告', '季度报告', '财务报表', '资产负债表', '现金流量表', '利润表'),
    'industry_report': ('行业研究', '行业点评', '行业深度', '行业周报', '行业月报', '医保', '集采'),
    'equity_research': ('投资要点', '盈利预测', '目标价', '评级', '风险提示', '分析师'),
}
_DOC_ANCHORS: dict[str, tuple[str, ...]] = {
    'financial_report': ('管理层讨论与分析', '主要会计数据', '资产负债表', '现金流量表', '利润表'),
    'industry_report': ('行业研究', '投资要点', '风险提示', '核心观点'),
    'equity_research': ('投资要点', '盈利预测', '风险提示', '估值'),
}
_METRIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ('目标价', re.compile(r'目标价')),
    ('评级', re.compile(r'(评级|买入|增持|中性|减持|卖出|推荐|观望)')),
    ('营收', re.compile(r'(营业收入|营业总收入|营收)')),
    ('净利润', re.compile(r'(净利润|归母净利润|归属于母公司股东的净利润)')),
    ('EPS', re.compile(r'(?:\bEPS\b|每股收益)', re.IGNORECASE)),
    ('ROE', re.compile(r'(?:\bROE\b|净资产收益率)', re.IGNORECASE)),
    ('PE', re.compile(r'(?:\bP/?E\b|市盈率)', re.IGNORECASE)),
    ('PB', re.compile(r'(?:\bP/?B\b|市净率)', re.IGNORECASE)),
    ('毛利率', re.compile(r'毛利率')),
    ('现金流', re.compile(r'(现金流|经营活动现金流|现金流量表)')),
    ('医保', re.compile(r'(医保|医保目录|集采|带量采购)')),
    ('盈利预测', re.compile(r'(盈利预测|业绩预测|估值)')),
    ('风险提示', re.compile(r'风险提示')),
]


def _clean_tags(text: str) -> str:
    """Remove PDF form-field annotation tags from extracted text."""
    text = _TAG_RE.sub('', text)
    text = _GARBLED_TAG_RE.sub('', text)  # interleaved CJK+ASCII tags
    # Remove residual garbled tag fragments like [指Ta标ble/年
    text = re.sub(r'\[[^\]]{0,8}[Tt]a[^\]]{0,30}\]?', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text


def _filter_lines(text: str) -> str:
    """Remove lines that are purely numeric/symbolic with no meaningful Chinese context."""
    out = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            out.append('')
            continue
        chinese = sum(1 for c in stripped if '\u4e00' <= c <= '\u9fff')
        # Keep lines with sufficient Chinese text
        if chinese >= 3:
            out.append(line)
            continue
        # Keep very short tokens (single labels, units)
        if len(stripped) <= 6:
            out.append(line)
            continue
        # Filter pure number/symbol lines (chart ticks, axis values)
        if _PURE_NUM_RE.match(stripped):
            continue
        # Filter lines where >80% of chars are digits/spaces/symbols
        non_num = sum(1 for c in stripped if not (c.isdigit() or c in ' %.,+-\u00d7\u00f7'))
        if non_num / len(stripped) < 0.2:
            continue
        out.append(line)
    return '\n'.join(out)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _clean_body_text(text: str, *, keep_numbers: bool = False) -> str:
    cleaned = _clean_tags(text)
    if not keep_numbers:
        cleaned = _filter_lines(cleaned)
    cleaned = _NOISE_RE.sub('', cleaned)
    cleaned = _BLANK_RE.sub('\n\n', cleaned)
    return cleaned.strip()


def _text_signal_counts(text: str) -> tuple[int, int, int]:
    stripped = str(text or '').strip()
    chinese = sum(1 for c in stripped if '\u4e00' <= c <= '\u9fff')
    letters = sum(1 for c in stripped if c.isalpha() and not ('\u4e00' <= c <= '\u9fff'))
    digits = sum(1 for c in stripped if c.isdigit())
    return chinese, letters, digits


def _text_signal_score(text: str) -> int:
    chinese, letters, digits = _text_signal_counts(text)
    return chinese * 4 + letters * 2 + digits


def _is_low_signal_text(text: str, *, min_len: int = 80, min_score: int = 40) -> bool:
    stripped = str(text or '').strip()
    if not stripped:
        return True
    chinese, letters, digits = _text_signal_counts(stripped)
    score = _text_signal_score(stripped)
    if len(stripped) < min_len:
        return True
    if score < min_score:
        return True
    return chinese < 6 and letters < 12 and digits < 18


def _pick_better_text(*candidates: str) -> str:
    best = ''
    best_score = -1
    for candidate in candidates:
        stripped = str(candidate or '').strip()
        if not stripped:
            continue
        score = _text_signal_score(stripped) * 10 + len(stripped)
        if score > best_score:
            best = stripped
            best_score = score
    return best


def _is_heading_line(line: str) -> bool:
    stripped = str(line or '').strip(' ：:')
    if not stripped or len(stripped) < 2 or len(stripped) > 40:
        return False
    if stripped.startswith('[表格]') or stripped.endswith('。'):
        return False
    if _HEADING_RE.match(stripped):
        return True
    return any(term in stripped for term in (
        '投资要点', '核心观点', '盈利预测', '风险提示', '估值', '行业观点',
        '管理层讨论与分析', '财务报表', '资产负债表', '现金流量表', '利润表',
    ))


def _classify_block_kind(text: str, default: str = 'paragraph') -> str:
    stripped = str(text or '').strip()
    if not stripped:
        return default
    if stripped.startswith('[表格]'):
        return 'table'
    if _is_heading_line(stripped):
        return 'title'
    if len(stripped) <= 120 and _FIGURE_TITLE_RE.search(stripped):
        return 'figure'
    return default


def _split_page_blocks(text: str, page_no: int, source: str = 'pdf') -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for para in re.split(r'\n{2,}', text):
        para = para.strip()
        if not para:
            continue
        lines = [line.strip() for line in para.split('\n') if line.strip()]
        if not lines:
            continue
        if len(lines) == 1 and _is_heading_line(lines[0]):
            blocks.append({'page_no': page_no, 'kind': 'title', 'text': lines[0], 'source': source})
            continue
        if lines and _is_heading_line(lines[0]) and len(lines) > 1:
            blocks.append({'page_no': page_no, 'kind': 'title', 'text': lines[0], 'source': source})
            para = '\n'.join(lines[1:]).strip()
        else:
            para = '\n'.join(lines).strip()
        if para:
            blocks.append({'page_no': page_no, 'kind': _classify_block_kind(para, 'paragraph'), 'text': para, 'source': source})
    return blocks


def _words_to_text(words: list[dict[str, Any]]) -> str:
    """Reconstruct text from pdfplumber word dicts sorted by (y, x)."""
    if not words:
        return ''
    lines: dict[int, list[dict]] = {}
    for w in words:
        y_key = round(w['top'] / 4) * 4   # 4pt bucket → same line
        lines.setdefault(y_key, []).append(w)
    parts = []
    for y_key in sorted(lines):
        line_words = sorted(lines[y_key], key=lambda w: w['x0'])
        parts.append(' '.join(w['text'] for w in line_words))
    return '\n'.join(parts)


def _extract_text_fitz(pdf_path: Path) -> str:
    parts: list[str] = []
    doc = None
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            t = page.get_text('text')
            if t:
                parts.append(t.strip())
    except Exception as e:
        print(f'[KB] fitz error {pdf_path.name}: {e}', file=sys.stderr)
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return '\n\n'.join(parts)


def _extract_textpage_text(page: Any, textpage: Any) -> str:
    try:
        return str(page.get_text('text', textpage=textpage) or '')
    except Exception:
        try:
            return str(textpage.extractText() or '')
        except Exception:
            return ''


_tesseract_available: bool | None = None   # None = untested, True/False = cached
_ocr_semaphore = threading.Semaphore(1)  # serialize OCR: only 1 thread at a time uses all CPU cores


def _maybe_ocr_page(page: Any) -> str:
    global _tesseract_available
    with _ocr_semaphore:
        return _maybe_ocr_page_inner(page)


def _maybe_ocr_page_inner(page: Any) -> str:
    global _tesseract_available
    # ── attempt 1: PyMuPDF built-in OCR (tesseract-based) ──
    if _tesseract_available is not False:
        for lang in ('chi_sim+eng', 'eng'):
            try:
                textpage = page.get_textpage_ocr(language=lang, dpi=150, full=True)
                text = _extract_textpage_text(page, textpage)
                cleaned = _clean_body_text(text)
                if cleaned and _text_signal_score(cleaned) >= 40:
                    _tesseract_available = True
                    return cleaned
            except Exception:
                continue
        if _tesseract_available is None:
            _tesseract_available = False
            print('[KB] tesseract OCR unavailable, will use RapidOCR only', file=sys.stderr)
    # ── attempt 2: RapidOCR (ONNX-based, no system tesseract needed) ──
    ocr = _get_thread_ocr()
    if ocr is not None and _NUMPY_OK:
        pix = None
        try:
            mat = fitz.Matrix(_OCR_DPI / 72, _OCR_DPI / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
            result, _ = ocr(img)
            if result:
                lines: list[str] = []
                for item in result:
                    txt = str(item[1] if len(item) > 1 else item[0]).strip()
                    if txt:
                        lines.append(txt)
                joined = '\n'.join(lines)
                cleaned = _clean_body_text(joined)
                if cleaned:
                    return cleaned
        except Exception as e:
            print(f'[KB] RapidOCR page error: {e}', file=sys.stderr)
        finally:
            del pix
    return ''


_ocr_thread_local = threading.local()

def _get_thread_ocr() -> Any | None:
    """Get a per-thread RapidOCR instance (thread-local, avoids contention)."""
    if not _RAPIDOCR_OK:
        return None
    ocr = getattr(_ocr_thread_local, 'ocr', None)
    if ocr is None:
        try:
            _ocr_thread_local.ocr = _RapidOCR()
            ocr = _ocr_thread_local.ocr
        except Exception:
            return None
    return ocr




def _extract_forecast_table(page1_words: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract analyst earnings forecast table from page-1 words.

    Looks for the MainProfit section (营收, 净利润, EPS, ROE, PE, PB rows).
    Returns a dict mapping metric → {year: value} or None if not found.
    """
    full = _words_to_text(page1_words)
    full = _clean_tags(full)

    # Find block starting from year headers
    # Format A: "2024A 2025E 2026E 2027E" (西南证券 style)
    # Format B: "2023 2024 2025E 2026E 2027E" (国金证券 style, years without 'A')
    m = re.search(
        r'((?:\d{4}A|\d{4}\s+\d{4}E)[\s\S]{0,1500}?\bPB\b[^\n]*)',
        full, re.DOTALL
    )
    if not m:
        return None
    block = m.group(0)

    # Extract all year tokens: "2024A", "2025E", or bare "2024" near forecasts
    year_m = re.findall(r'(\d{4}[AE]|(?<=\s)\d{4}(?=\s))', block[:200])
    # Keep only 4-digit years, deduplicate, first 5
    seen: list[str] = []
    for y in year_m:
        if y not in seen:
            seen.append(y)
    years = seen[:5]  # may be 5 for 国金 format (2023-2027)
    if len(years) < 2:
        return None

    n = len(years)  # 4 or 5 depending on format
    val_pat = r'\s+'.join([r'([\d.,]+)'] * n)
    pct_pat = r'\s+'.join([r'([\d.,]+%?)'] * n)

    forecasts: dict[str, Any] = {'years': years}
    metrics = [
        ('revenue',        r'营业收入[（(]?百万元[）)]?\s*' + val_pat),
        ('revenue_growth', r'(?<!净利润)增长率\s*' + pct_pat),
        ('net_profit',     r'(?:归属母公司)?净利润[（(]?百万元[）)]?\s*' + val_pat),
        ('eps',            r'(?:每股收益)?EPS[（(]?元[）)]?\s*' + r'\s+'.join([r'([\d.]+)'] * n)),
        ('roe',            r'ROE\s*' + r'\s+'.join([r'([\d.%]+)'] * n)),
        ('pe',             r'\bP(?:/)?E\b\s*' + r'\s+'.join([r'([\d.]+)'] * n)),
        ('pb',             r'\bP(?:/)?B\b\s*' + r'\s+'.join([r'([\d.]+)'] * n)),
    ]
    for key, pat in metrics:
        mm = re.search(pat, block)
        if mm:
            forecasts[key] = {years[i]: mm.group(i + 1) for i in range(min(n, mm.lastindex or 0))}
    return forecasts if len(forecasts) > 2 else None


def _extract_rich_metadata(pdf_path: Path) -> dict[str, str]:
    """Extract rich structured metadata from page 1 of a research report.

    Fields: stock_code, company, current_price, target_price, rating,
            analyst, research_firm, sector, report_date, forecast_json.
    """
    meta: dict[str, str] = {}
    if not _PDFPLUMBER_OK:
        return _extract_metadata_regex('', pdf_path.stem)
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return meta
            page1 = pdf.pages[0]
            words = page1.extract_words(x_tolerance=3, y_tolerance=3)

            # Split into main (left) and sidebar (right)
            main_words = [w for w in words if w['x0'] < _SIDEBAR_X]
            side_words = [w for w in words if w['x0'] >= _SIDEBAR_X]
            main_text = _clean_tags(_words_to_text(main_words))
            side_text = _clean_tags(_words_to_text(side_words))
            combined = main_text + '\n' + side_text

            # ── report date ─────────────────────────────────────────────────
            dm = re.search(r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', combined)
            if dm:
                meta['report_date'] = f"{dm.group(1)}-{dm.group(2).zfill(2)}-{dm.group(3).zfill(2)}"
            else:
                dm2 = re.search(r'(20\d{2})[年\-/](\d{1,2})', combined)
                if dm2:
                    meta['report_date'] = f"{dm2.group(1)}-{dm2.group(2).zfill(2)}"

            # ── company + stock code ────────────────────────────────────────
            sc_m = re.search(r'([\u4e00-\u9fff]{2,8})[（(](\d{6})[）)]', combined)
            if sc_m:
                meta['company'] = sc_m.group(1)
                meta['stock_code'] = sc_m.group(2)
            else:
                # For image-header reports: scan all pages for company（code） pattern
                for pg in pdf.pages:
                    pg_words = pg.extract_words(x_tolerance=3, y_tolerance=3)
                    pg_text = _clean_tags(_words_to_text(pg_words))
                    sc2 = re.search(r'([\u4e00-\u9fff]{2,8})[（(](\d{6})[）)]', pg_text)
                    if sc2:
                        meta['company'] = sc2.group(1)
                        meta['stock_code'] = sc2.group(2)
                        break

            # ── rating ──────────────────────────────────────────────────────
            rt_m = re.search(r'(买入|增持|中性|减持|卖出|强烈推荐|推荐|观望)', combined[:300])
            if rt_m:
                meta['rating'] = rt_m.group(1)

            # ── current price ───────────────────────────────────────────────
            cp_m = re.search(r'当前价[：:]\s*([\d.]+)\s*元', combined)
            if cp_m:
                meta['current_price'] = cp_m.group(1)

            # ── target price ────────────────────────────────────────────────
            tp_m = re.search(r'目标价[：:]\s*([\d—–-]+(?:\.[\d]+)?)\s*元', combined)
            if tp_m:
                meta['target_price'] = tp_m.group(1)

            # ── sector ──────────────────────────────────────────────────────
            sec_m = re.search(
                r'(医药生物|计算机|电子|通信|银行|房地产|机械|化工|食品饮料|汽车|军工|传媒|消费)',
                combined[:500]
            )
            if sec_m:
                meta['sector'] = sec_m.group(1)

            # ── research firm (sidebar first, then main text) ──────────────
            firm_pat = r'([\u4e00-\u9fff]{2,8}(?:证券|投资|基金|资管|资本)[\u4e00-\u9fff研院所部]{0,6})'
            firm_m = re.search(firm_pat, side_text[:400])
            if not firm_m:
                firm_m = re.search(firm_pat, combined)
            if firm_m:
                meta['research_firm'] = firm_m.group(1)

            # ── analyst name: extract from RAW (uncleaned) garbled PDF tag ───
            raw_side = _words_to_text(side_words)  # NOT cleaned yet
            # Pattern: [分Ta析bl师e_：Au杜th向or阳] → extract CJK → 分析师：杜向阳
            tag_m = re.search(
                r'\[([^\]]{0,80}\u5206[^\]]{0,80}\u6790[^\]]{0,80}\u5e08[^\]]{0,80})\]',
                raw_side
            )
            if tag_m:
                cjk_only = re.sub(r'[^\u4e00-\u9fff\uff1a：]', '', tag_m.group(1))
                an_name_m = re.search(r'\u5e08[\uff1a：]([\u4e00-\u9fff]{2,4})', cjk_only)
                if an_name_m:
                    meta['analyst'] = an_name_m.group(1)
            if 'analyst' not in meta:
                # fallback: Chinese name before 执业证号 in cleaned sidebar
                an_m2 = re.search(r'([\u4e00-\u9fff]{2,4})\s*\n\s*执业证号', side_text)
                if an_m2:
                    meta['analyst'] = an_m2.group(1)

            # ── financial forecast table ─────────────────────────────────────
            forecast = _extract_forecast_table(words)
            if forecast:
                meta['forecast_json'] = json.dumps(forecast, ensure_ascii=False)

    except Exception as e:
        print(f'[KB] metadata extraction error {pdf_path.name}: {e}', file=sys.stderr)

    # fallback: company from filename or full-text body scan
    if 'company' not in meta:
        try:
            from config import CONFIG
            known = list(CONFIG.get('stock_abbr_to_code', {}).keys())
        except Exception:
            known = []
        # 1) filename
        for kw in known:
            if kw in pdf_path.stem:
                meta['company'] = kw
                break
        # 2) full-text body scan via pdfplumber
        if 'company' not in meta and _PDFPLUMBER_OK:
            try:
                with pdfplumber.open(str(pdf_path)) as pdf2:
                    for pg in pdf2.pages:
                        pg_text = _clean_tags(_words_to_text(
                            pg.extract_words(x_tolerance=3, y_tolerance=3)
                        ))
                        for kw in known:
                            if kw in pg_text:
                                meta['company'] = kw
                                break
                        if 'company' in meta:
                            break
            except Exception:
                pass

    # resolve stock_code from company name via config mapping
    if 'company' in meta and 'stock_code' not in meta:
        try:
            from config import CONFIG
            mapping = CONFIG.get('stock_abbr_to_code', {})
            code = mapping.get(meta['company'])
            if code:
                meta['stock_code'] = str(code)
        except Exception:
            pass

    # fallback: research_firm from last pages if still missing
    if 'research_firm' not in meta and _PDFPLUMBER_OK:
        firm_pat = r'([\u4e00-\u9fff]{2,8}(?:证券|投资|基金|资管|资本)[\u4e00-\u9fff研院所部]{0,6})'
        try:
            with pdfplumber.open(str(pdf_path)) as pdf3:
                for pg in reversed(pdf3.pages):
                    pg_text = _clean_tags(_words_to_text(
                        pg.extract_words(x_tolerance=3, y_tolerance=3)
                    ))
                    fm = re.search(firm_pat, pg_text)
                    if fm:
                        meta['research_firm'] = fm.group(1)
                        break
        except Exception:
            pass

    return meta


def _extract_metadata_regex(text: str, stem: str) -> dict[str, str]:
    """Fallback metadata extraction when pdfplumber is unavailable."""
    head = text[:1200]
    meta: dict[str, str] = {}
    m = re.search(r'(20\d{2})[年\-/](\d{1,2})[月\-/]?(\d{0,2})', head)
    if m:
        meta['report_date'] = f"{m.group(1)}-{m.group(2).zfill(2)}"
    try:
        from config import CONFIG
        known_companies = list(CONFIG.get('stock_abbr_to_code', {}).keys())
    except Exception:
        known_companies = []
    for keyword in known_companies:
        if keyword in stem or keyword in head[:300]:
            meta['company'] = keyword
            break
    m2 = re.search(r'(买入|增持|中性|减持|卖出|强烈推荐|推荐|观望)', head)
    if m2:
        meta['rating'] = m2.group(1)
    return meta


def _word_in_bbox(w: dict[str, Any], bbox: tuple[float, float, float, float], margin: float = 3.0) -> bool:
    """Return True if word center falls within bbox (x0, top, x1, bottom)."""
    cx = (w['x0'] + w['x1']) / 2
    cy = (w['top'] + w['bottom']) / 2
    return (bbox[0] - margin) <= cx <= (bbox[2] + margin) and (bbox[1] - margin) <= cy <= (bbox[3] + margin)


def _table_rows_to_text(rows: list[list[str]]) -> str:
    """Convert table rows to readable text, keeping only rows with at least one Chinese cell."""
    keep = []
    for row in rows:
        # Normalize cells: collapse internal newlines/whitespace to single space
        cells = [re.sub(r'\s+', ' ', str(c or '')).strip() for c in row]
        if not any(cells):
            continue
        if any(sum(1 for ch in cell if '\u4e00' <= ch <= '\u9fff') >= 1 for cell in cells):
            keep.append(' | '.join(c for c in cells if c))
    return '\n'.join(keep)


def _extract_text_fitz_structured(pdf_path: Path) -> tuple[str, list[tuple[int, str]], list[dict[str, Any]], dict[str, Any]]:
    parts: list[str] = []
    page_texts: list[tuple[int, str]] = []
    page_blocks: list[dict[str, Any]] = []
    page_count = 0
    ocr_pages = 0
    doc = None
    try:
        doc = fitz.open(str(pdf_path))
        page_count = len(doc)
        for i, page in enumerate(doc):
            page_no = i + 1
            block_texts: list[str] = []
            try:
                for block in page.get_text('blocks'):
                    raw = str(block[4] or '').strip()
                    strict_text = _clean_body_text(raw)
                    relaxed_text = _clean_body_text(raw, keep_numbers=True) if _is_low_signal_text(strict_text, min_len=60, min_score=24) else ''
                    cleaned = _pick_better_text(strict_text, relaxed_text)
                    if cleaned:
                        block_texts.append(cleaned)
            except Exception:
                pass
            page_text = '\n\n'.join(block_texts).strip()
            page_block_items: list[dict[str, Any]] = []
            for block_text in block_texts:
                page_block_items.extend(_split_page_blocks(block_text, page_no, source='fitz'))
            if _is_low_signal_text(page_text, min_len=60, min_score=24):
                ocr_text = _maybe_ocr_page(page)
                if _text_signal_score(ocr_text) > _text_signal_score(page_text):
                    page_text = ocr_text
                    page_block_items = _split_page_blocks(ocr_text, page_no, source='ocr')
                    ocr_pages += 1
            if page_text:
                parts.append(page_text)
                page_texts.append((page_no, page_text))
                page_blocks.extend(page_block_items)
    except Exception as e:
        print(f'[KB] fitz error {pdf_path.name}: {e}', file=sys.stderr)
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    text_density = round(sum(len(t) for _, t in page_texts) / max(page_count, 1), 2) if page_count else 0.0
    return '\n\n'.join(parts), page_texts, page_blocks, {
        'page_count': page_count,
        'ocr_pages': ocr_pages,
        'has_ocr': bool(ocr_pages),
        'text_density': text_density,
    }


def _get_extract_cache(pdf_path: Path) -> tuple[str, dict[str, str], list[tuple[int, str]], list[dict[str, Any]], dict[str, Any]] | None:
    """Try to load cached extraction results for an unchanged file."""
    try:
        stat = pdf_path.stat()
        with kb_conn() as conn:
            row = conn.execute(
                'SELECT file_size, file_mtime, ocr_dpi, full_text, meta_json, '
                'page_texts_json, page_blocks_json, extract_info_json '
                'FROM kb_extract_cache WHERE doc_path=?',
                (str(pdf_path),)
            ).fetchone()
        if row is None:
            return None
        if row['file_size'] != stat.st_size or abs(row['file_mtime'] - stat.st_mtime) > 0.01 or row['ocr_dpi'] != _OCR_DPI:
            return None
        full_text = row['full_text'] or ''
        meta = json.loads(row['meta_json']) if row['meta_json'] else {}
        page_texts = [tuple(x) for x in json.loads(row['page_texts_json'])] if row['page_texts_json'] else []
        page_blocks = json.loads(row['page_blocks_json']) if row['page_blocks_json'] else []
        extract_info = json.loads(row['extract_info_json']) if row['extract_info_json'] else {}
        return full_text, meta, page_texts, page_blocks, extract_info
    except Exception:
        return None


def _set_extract_cache(pdf_path: Path, full_text: str, meta: dict, page_texts: list, page_blocks: list, extract_info: dict) -> None:
    """Store extraction results in cache for future rebuilds."""
    try:
        stat = pdf_path.stat()
        with kb_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO kb_extract_cache'
                '(doc_path, file_size, file_mtime, ocr_dpi, full_text, meta_json, '
                'page_texts_json, page_blocks_json, extract_info_json) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    str(pdf_path), stat.st_size, stat.st_mtime, _OCR_DPI,
                    full_text, json.dumps(meta, ensure_ascii=False),
                    json.dumps(page_texts, ensure_ascii=False),
                    json.dumps(page_blocks, ensure_ascii=False),
                    json.dumps(extract_info, ensure_ascii=False),
                )
            )
    except Exception as e:
        print(f'[KB] extract cache write error: {e}', file=sys.stderr)


def _extract_text(pdf_path: Path) -> tuple[str, dict[str, str], list[tuple[int, str]], list[dict[str, Any]], dict[str, Any]]:
    """Extract full body text + rich metadata from a research report PDF.

    Strategy per page:
    1. Detect tables via find_tables() to get precise bounding boxes.
    2. Prose text: extract words that are NOT inside any table bbox and x0 < _SIDEBAR_X.
    3. Table text: extract each table with structured row/cell format.
    This prevents cross-column number mixing in two-column financial tables.
    Returns (body_text, metadata_dict, page_texts) where page_texts is
    a list of (1-based page_number, cleaned_page_text) tuples.
    """
    cached = _get_extract_cache(pdf_path)
    if cached is not None:
        print(f'[KB] extract cache HIT: {pdf_path.name}', file=sys.stderr)
        return cached

    meta = _extract_rich_metadata(pdf_path)
    full_text = ''
    page_texts: list[tuple[int, str]] = []
    page_blocks: list[dict[str, Any]] = []
    extract_info: dict[str, Any] = {'page_count': 0, 'ocr_pages': 0, 'has_ocr': False, 'text_density': 0.0}

    if _PDFPLUMBER_OK:
        fitz_doc: Any | None = None
        try:
            parts: list[str] = []
            try:
                fitz_doc = fitz.open(str(pdf_path))
            except Exception:
                fitz_doc = None
            with pdfplumber.open(str(pdf_path)) as pdf:
                extract_info['page_count'] = len(pdf.pages)
                for page in pdf.pages:
                    page_no = page.page_number  # 1-based
                    words = page.extract_words(x_tolerance=3, y_tolerance=3)
                    page_parts: list[str] = []
                    page_block_items: list[dict[str, Any]] = []

                    # ── Step 1: find tables → precise bounding boxes ───────────
                    try:
                        found_tables = page.find_tables()
                    except Exception:
                        found_tables = []
                    table_bboxes: list[tuple[float, float, float, float]] = [
                        t.bbox for t in found_tables
                    ]

                    # ── Step 2: find image areas → chart-label filtering ────────
                    try:
                        image_bboxes: list[tuple[float, float, float, float]] = [
                            (img['x0'], img['top'], img['x1'], img['bottom'])
                            for img in page.images
                            if (img['x1'] - img['x0']) * (img['bottom'] - img['top']) > 4000
                        ]
                    except Exception:
                        image_bboxes = []

                    # ── Step 3: dynamic column split (sidebar vs prose) ────────
                    non_tbl = [
                        w for w in words
                        if not any(_word_in_bbox(w, bb) for bb in table_bboxes)
                    ]
                    right_words = [w for w in non_tbl if w['x0'] >= _SIDEBAR_X]
                    if not right_words:
                        col_limit: float = float('inf')  # no sidebar, full-width page
                    else:
                        right_text = ' '.join(w['text'] for w in right_words)
                        zh_ratio = sum(
                            1 for c in right_text if '\u4e00' <= c <= '\u9fff'
                        ) / max(len(right_text), 1)
                        col_limit = float('inf') if zh_ratio > 0.30 else _SIDEBAR_X

                    # ── Step 4: prose words (in column, not in table/image) ────────
                    prose_words = [
                        w for w in words
                        if w['x0'] < col_limit
                        and not any(_word_in_bbox(w, bb) for bb in table_bboxes)
                        and not any(_word_in_bbox(w, bb, margin=2.0) for bb in image_bboxes)
                    ]
                    raw_prose_text = _words_to_text(prose_words)
                    strict_prose_text = _clean_body_text(raw_prose_text)
                    relaxed_prose_text = _clean_body_text(raw_prose_text, keep_numbers=True) if raw_prose_text and _is_low_signal_text(strict_prose_text, min_len=80, min_score=32) else ''
                    fitz_prose_text = ''
                    if fitz_doc is not None and 0 <= page_no - 1 < len(fitz_doc) and _is_low_signal_text(strict_prose_text, min_len=80, min_score=32):
                        fitz_prose_text = _clean_body_text(fitz_doc[page_no - 1].get_text('text'), keep_numbers=True)
                    prose_text = _pick_better_text(strict_prose_text, relaxed_prose_text, fitz_prose_text)
                    if prose_text.strip():
                        page_parts.append(prose_text.strip())
                        page_block_items.extend(_split_page_blocks(prose_text.strip(), page_no, source='pdf'))

                    # ── Step 5: extract each table as structured text ─────────
                    for t in found_tables:
                        try:
                            rows = t.extract()
                        except Exception:
                            continue
                        tbl_text = _table_rows_to_text(rows)
                        tbl_text = _clean_body_text(tbl_text, keep_numbers=True)
                        if tbl_text.strip():
                            table_text = '[表格]\n' + tbl_text
                            page_parts.append(table_text)
                            page_block_items.append({'page_no': page_no, 'kind': 'table', 'text': table_text, 'source': 'pdf'})

                    page_text = '\n\n'.join(page_parts).strip()
                    if _is_low_signal_text(page_text, min_len=60, min_score=24) and fitz_doc is not None and 0 <= page_no - 1 < len(fitz_doc):
                        ocr_text = _maybe_ocr_page(fitz_doc[page_no - 1])
                        if _text_signal_score(ocr_text) > _text_signal_score(page_text):
                            page_text = ocr_text
                            page_block_items = _split_page_blocks(ocr_text, page_no, source='ocr')
                            extract_info['ocr_pages'] = int(extract_info.get('ocr_pages', 0)) + 1
                            extract_info['has_ocr'] = True

                    if page_text:
                        parts.append(page_text)
                        page_texts.append((page_no, page_text))
                        page_blocks.extend(page_block_items)

            full_text = '\n\n'.join(parts)
        except Exception as e:
            print(f'[KB] pdfplumber error {pdf_path.name}: {e}', file=sys.stderr)
        finally:
            try:
                if fitz_doc is not None:
                    fitz_doc.close()
            except Exception:
                pass

    if not full_text.strip():
        full_text, page_texts, page_blocks, extract_info = _extract_text_fitz_structured(pdf_path)

    full_text = _clean_tags(full_text)
    full_text = _NOISE_RE.sub('', full_text)
    full_text = _BLANK_RE.sub('\n\n', full_text).strip()
    if page_texts:
        extract_info['text_density'] = round(sum(len(t) for _, t in page_texts) / max(int(extract_info.get('page_count') or 1), 1), 2)
    if not page_blocks and page_texts:
        for page_no, page_text in page_texts:
            page_blocks.extend(_split_page_blocks(page_text, page_no, source='fallback'))

    _set_extract_cache(pdf_path, full_text, meta, page_texts, page_blocks, extract_info)
    return full_text, meta, page_texts, page_blocks, extract_info


# ══════════════════════════════════════════════════════════════════════════════
# Parent-Child Chunking (small child for retrieval, large parent for context)
# ══════════════════════════════════════════════════════════════════════════════

def _hard_split(text: str, max_len: int = CHILD_CHUNK_MAX, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text longer than max_len into overlapping sub-chunks."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += max_len - overlap
    return chunks


def _build_parent_chunks(text: str) -> list[str]:
    """Build large parent chunks (for context) from paragraph boundaries."""
    paragraphs = re.split(r'\n{2,}', text)
    parents: list[str] = []
    buffer = ''
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(buffer) + len(para) + 1 > PARENT_CHUNK_MAX:
            if buffer.strip():
                parents.append(buffer.strip())
            buffer = para
        else:
            buffer = (buffer + '\n' + para).strip() if buffer else para
    if buffer.strip():
        parents.append(buffer.strip())
    return parents


def _split_parent_to_children(parent: str) -> list[str]:
    """Split a parent chunk into smaller child chunks for retrieval precision."""
    if len(parent) <= CHILD_CHUNK_MAX:
        return [parent]
    # Try splitting on sentence boundaries first
    sentences = re.split(r'(?<=[。！？；\n])', parent)
    children: list[str] = []
    buffer = ''
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(buffer) + len(sent) + 1 > CHILD_CHUNK_MAX:
            if len(buffer) >= CHILD_CHUNK_MIN:
                children.append(buffer.strip())
            buffer = sent
        else:
            buffer = sent if not buffer else (buffer + sent)
    if len(buffer) >= CHILD_CHUNK_MIN:
        children.append(buffer.strip())
    elif buffer.strip() and children:
        # Append short remainder to last child
        children[-1] = children[-1] + buffer.strip()
    elif buffer.strip():
        children.append(buffer.strip())
    # If sentence split didn't work, fall back to hard split
    if not children:
        children = _hard_split(parent, CHILD_CHUNK_MAX, CHUNK_OVERLAP)
    return children


def _chunk_text_parent_child(text: str) -> list[tuple[str, str]]:
    """Parent-child chunking. Returns list of (child_text, parent_text) tuples.

    Child chunks are small (100-300 chars) for precise retrieval.
    Parent chunks are large (up to 900 chars) providing full context to the LLM.
    """
    parents = _build_parent_chunks(text)
    pairs: list[tuple[str, str]] = []
    for parent in parents:
        children = _split_parent_to_children(parent)
        for child in children:
            pairs.append((child, parent))
    return pairs


def _chunk_text(text: str) -> list[str]:
    """Legacy: returns child chunks only (for backward compatibility)."""
    return [child for child, _ in _chunk_text_parent_child(text)]


def _assign_page_numbers(
    pc_pairs: list[tuple[str, str]],
    page_texts: list[tuple[int, str]],
) -> list[int | None]:
    """Determine which PDF page each child chunk belongs to.

    Matches each child chunk's text (first 60 chars) against page texts.
    Returns a list of 1-based page numbers (or None if unresolvable).
    """
    result: list[int | None] = []
    for child, _parent in pc_pairs:
        snippet = child[:60]
        best_page: int | None = None
        for page_no, page_text in page_texts:
            if snippet in page_text:
                best_page = page_no
                break
        if best_page is None and len(child) > 20:
            # fallback: try shorter prefix
            short = child[:30]
            for page_no, page_text in page_texts:
                if short in page_text:
                    best_page = page_no
                    break
        result.append(best_page)
    return result


# ── Figure / chart / table title extraction ──────────────────────────────────
_FIGURE_TITLE_RE = re.compile(
    r'((?:图表?|表格?)\s*\d+[：:]\s*[^\n]{2,80})'
)


def _extract_page_figure_titles(
    page_texts: list[tuple[int, str]],
) -> dict[int, list[str]]:
    """Extract figure/chart/table captions from each page.

    Returns {page_no: ["图表8：2025年国家医保目录新增7个中药产品", ...]}
    """
    result: dict[int, list[str]] = {}
    for page_no, text in page_texts:
        titles = _FIGURE_TITLE_RE.findall(text)
        if titles:
            result[page_no] = [t.strip() for t in titles]
    return result


def _assign_figure_titles(
    page_numbers: list[int | None],
    page_figure_titles: dict[int, list[str]],
) -> list[str | None]:
    """For each chunk, return JSON-encoded figure titles from its page (or None)."""
    result: list[str | None] = []
    for pg in page_numbers:
        if pg and pg in page_figure_titles:
            result.append(json.dumps(page_figure_titles[pg], ensure_ascii=False))
        else:
            result.append(None)
    return result


def _detect_doc_kind(text: str, pdf_path: Path) -> str:
    head = text[:4000]
    for doc_kind, hints in _DOC_KIND_HINTS.items():
        if any(hint in head for hint in hints):
            return doc_kind
    if '行业' in pdf_path.stem:
        return 'industry_report'
    return 'research_report'


def _assess_document_quality(
    pdf_path: Path,
    text: str,
    meta: dict[str, Any],
    page_texts: list[tuple[int, str]],
    extract_info: dict[str, Any],
) -> dict[str, Any]:
    page_count = int(extract_info.get('page_count') or len(page_texts) or 0)
    text_density = float(extract_info.get('text_density') or 0.0)
    doc_kind = _detect_doc_kind(text, pdf_path)
    head = text[:6000]
    chinese_chars = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    char_count = max(len(text), 1)
    chinese_ratio = chinese_chars / char_count
    anchor_hits = sum(1 for anchor in _DOC_ANCHORS.get(doc_kind, ()) if anchor in head)
    abstract_hits = len(_ABSTRACT_HINT_RE.findall(head))
    is_abstract = bool(abstract_hits and anchor_hits < 2)
    if doc_kind == 'financial_report' and page_count and page_count < 10 and anchor_hits < 2:
        is_abstract = True
    quality = 1.0
    notes: list[str] = []
    if is_abstract:
        quality -= 0.65
        notes.append('疑似摘要')
    if len(text) < 1200:
        quality -= 0.2
        notes.append('正文较短')
    if chinese_ratio < 0.2:
        quality -= 0.2
        notes.append('中文密度偏低')
    if page_count and text_density < 120:
        quality -= 0.15
        notes.append('页均正文密度偏低')
    if doc_kind == 'financial_report' and anchor_hits < 2:
        quality -= 0.15
        notes.append('关键章节缺失')
    if extract_info.get('ocr_pages'):
        notes.append(f"OCR页数:{extract_info.get('ocr_pages')}")
    if meta.get('company'):
        notes.append(f"公司:{meta.get('company')}")
    quality = round(max(0.0, min(1.0, quality)), 3)
    should_skip = False
    if not notes:
        notes.append('正文完整')
    return {
        'doc_kind': doc_kind,
        'quality_score': quality,
        'quality_notes': '；'.join(notes),
        'is_abstract': is_abstract,
        'has_ocr': bool(extract_info.get('has_ocr')),
        'page_count': page_count,
        'text_density': round(text_density, 2),
        'should_skip': should_skip,
    }


def _extract_metric_tags(text: str) -> list[str]:
    tags = [name for name, pattern in _METRIC_PATTERNS if pattern.search(text)]
    return _dedupe_keep_order(tags)


def _extract_entities(
    text: str,
    meta: dict[str, Any],
    section_title: str,
    page_start: int,
    page_end: int,
) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    company = str(meta.get('company') or '').strip()
    stock_code = str(meta.get('stock_code') or '').strip()
    years = _dedupe_keep_order(re.findall(r'20\d{2}(?:FY|Q[1-4]|HY|H1|E|A)?', text))[:8]
    if company:
        entities['company'] = company
    if stock_code:
        entities['stock_code'] = stock_code
    if section_title:
        entities['section_title'] = section_title
    if years:
        entities['years'] = years
    entities['page_start'] = page_start
    entities['page_end'] = page_end
    return entities


def _build_retrieval_text(
    child_text: str,
    parent_text: str,
    doc_meta: dict[str, Any],
    section_title: str,
    chunk_type: str,
    page_start: int,
    page_end: int,
    metric_tags: list[str],
) -> str:
    lines: list[str] = []
    if doc_meta.get('doc_name'):
        lines.append(f"文档：{doc_meta['doc_name']}")
    if doc_meta.get('company'):
        lines.append(f"公司：{doc_meta['company']}")
    if doc_meta.get('stock_code'):
        lines.append(f"股票代码：{doc_meta['stock_code']}")
    if doc_meta.get('report_date'):
        lines.append(f"日期：{doc_meta['report_date']}")
    if doc_meta.get('research_firm'):
        lines.append(f"机构：{doc_meta['research_firm']}")
    if doc_meta.get('analyst'):
        lines.append(f"分析师：{doc_meta['analyst']}")
    if doc_meta.get('sector'):
        lines.append(f"行业：{doc_meta['sector']}")
    if doc_meta.get('rating'):
        lines.append(f"评级：{doc_meta['rating']}")
    if doc_meta.get('target_price'):
        lines.append(f"目标价：{doc_meta['target_price']}")
    if section_title:
        lines.append(f'章节：{section_title}')
    if chunk_type:
        lines.append(f'片段类型：{chunk_type}')
    if page_start:
        page_label = f'{page_start}' if page_start == page_end else f'{page_start}-{page_end}'
        lines.append(f'页码：{page_label}')
    if metric_tags:
        lines.append(f"指标：{'、'.join(metric_tags)}")
    lines.append(f'内容：{child_text.strip()}')
    if parent_text and parent_text != child_text:
        lines.append(f'上下文：{parent_text[:240].strip()}')
    return '\n'.join(lines).strip()


def _build_low_signal_summary_text(
    pdf_path: Path,
    doc_meta: dict[str, Any],
    quality_info: dict[str, Any],
    extract_info: dict[str, Any],
    body_text: str,
) -> str:
    text_density = float(extract_info.get('text_density') or 0.0)
    if not (_is_low_signal_text(body_text, min_len=220, min_score=120) or text_density < 25):
        return ''
    title = str(doc_meta.get('doc_name') or pdf_path.stem).strip()
    lines: list[str] = []
    if title:
        lines.append(f'文档标题：{title}')
    if quality_info.get('doc_kind'):
        lines.append(f"文档类型：{quality_info['doc_kind']}")
    if doc_meta.get('company'):
        lines.append(f"公司：{doc_meta['company']}")
    if doc_meta.get('stock_code'):
        lines.append(f"股票代码：{doc_meta['stock_code']}")
    if doc_meta.get('report_date'):
        lines.append(f"日期：{doc_meta['report_date']}")
    if doc_meta.get('research_firm'):
        lines.append(f"机构：{doc_meta['research_firm']}")
    if doc_meta.get('analyst'):
        lines.append(f"分析师：{doc_meta['analyst']}")
    if doc_meta.get('sector'):
        lines.append(f"行业：{doc_meta['sector']}")
    if doc_meta.get('rating'):
        lines.append(f"评级：{doc_meta['rating']}")
    if doc_meta.get('target_price'):
        lines.append(f"目标价：{doc_meta['target_price']}")
    if doc_meta.get('current_price'):
        lines.append(f"当前价：{doc_meta['current_price']}")
    if extract_info.get('page_count'):
        lines.append(f"页数：{extract_info['page_count']}")
    if quality_info.get('quality_notes'):
        lines.append(f"抽取说明：{quality_info['quality_notes']}")
    snippet = re.sub(r'\s+', ' ', str(body_text or '')).strip()
    if snippet and snippet != title:
        lines.append(f'可提取正文片段：{snippet[:240]}')
    return '\n'.join(_dedupe_keep_order(lines)).strip()


def _build_structured_chunks(
    page_blocks: list[dict[str, Any]],
    doc_meta: dict[str, Any],
    page_figure_titles: dict[int, list[str]],
    fallback_text: str = '',
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if not page_blocks and fallback_text.strip():
        page_blocks = [{'page_no': 1, 'kind': 'paragraph', 'text': fallback_text.strip(), 'source': 'fallback'}]

    current_section = ''
    buffer_blocks: list[dict[str, Any]] = []
    buffer_kind = ''
    buffer_section = ''

    def flush() -> None:
        nonlocal buffer_blocks, buffer_kind, buffer_section
        if not buffer_blocks:
            return
        texts = [str(block.get('text') or '').strip() for block in buffer_blocks if str(block.get('text') or '').strip()]
        if not texts:
            buffer_blocks = []
            buffer_kind = ''
            buffer_section = ''
            return
        parent_text = '\n\n'.join(texts).strip()
        page_start = min(int(block.get('page_no') or 1) for block in buffer_blocks)
        page_end = max(int(block.get('page_no') or page_start) for block in buffer_blocks)
        figure_titles: list[str] = []
        for page_no in range(page_start, page_end + 1):
            for title in page_figure_titles.get(page_no, []):
                if title not in figure_titles:
                    figure_titles.append(title)
        section_title = buffer_section or ''
        metric_tags = _extract_metric_tags('\n'.join([section_title, parent_text]))
        entities = _extract_entities(parent_text, doc_meta, section_title, page_start, page_end)
        child_texts = _split_parent_to_children(parent_text) if buffer_kind not in ('table', 'figure') else _hard_split(parent_text, CHILD_CHUNK_MAX, CHUNK_OVERLAP)
        if not child_texts:
            child_texts = [parent_text]
        for child_text in child_texts:
            if len(child_text.strip()) < 20:
                continue
            chunks.append({
                'text': child_text,
                'parent_text': parent_text,
                'page_no': page_start,
                'page_start': page_start,
                'page_end': page_end,
                'figure_titles': json.dumps(figure_titles, ensure_ascii=False) if figure_titles else None,
                'chunk_type': buffer_kind or 'paragraph',
                'section_title': section_title or None,
                'section_path': section_title or None,
                'retrieval_text': _build_retrieval_text(child_text, parent_text, doc_meta, section_title, buffer_kind or 'paragraph', page_start, page_end, metric_tags),
                'entities_json': json.dumps(entities, ensure_ascii=False) if entities else None,
                'metric_tags': '|'.join(metric_tags) if metric_tags else None,
            })
        buffer_blocks = []
        buffer_kind = ''
        buffer_section = ''

    for block in page_blocks:
        text = str(block.get('text') or '').strip()
        if not text:
            continue
        kind = str(block.get('kind') or 'paragraph')
        if kind == 'title':
            flush()
            current_section = text[:120]
            continue
        target_kind = 'table' if kind == 'table' else ('figure' if kind == 'figure' else 'paragraph')
        next_section = current_section or ''
        current_len = sum(len(str(item.get('text') or '')) for item in buffer_blocks)
        if buffer_blocks and (target_kind != buffer_kind or next_section != buffer_section or current_len + len(text) > PARENT_CHUNK_MAX):
            flush()
        if not buffer_blocks:
            buffer_kind = target_kind
            buffer_section = next_section
        buffer_blocks.append({'page_no': int(block.get('page_no') or 1), 'text': text, 'kind': target_kind})
    flush()
    return chunks


def _update_document_record(
    conn: Any,
    doc_id: int,
    status: str,
    total_chunks: int,
    build_seconds: float,
    merged_meta: dict[str, Any],
    quality_info: dict[str, Any],
) -> None:
    update_sql = (
        "UPDATE kb_documents SET status=?, total_chunks=?, indexed_at=?, build_seconds=?, "
        "doc_kind=?, quality_score=?, quality_notes=?, is_abstract=?, has_ocr=?, page_count=?, text_density=?"
    )
    params: list[Any] = [
        status,
        total_chunks,
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        build_seconds,
        quality_info.get('doc_kind'),
        quality_info.get('quality_score'),
        quality_info.get('quality_notes'),
        1 if quality_info.get('is_abstract') else 0,
        1 if quality_info.get('has_ocr') else 0,
        quality_info.get('page_count'),
        quality_info.get('text_density'),
    ]
    meta_col_map = {
        'report_date': 'report_date',
        'company': 'stock_name',
        'stock_code': 'stock_code',
        'current_price': 'current_price',
        'target_price': 'target_price',
        'rating': 'rating',
        'analyst': 'analyst',
        'research_firm': 'research_firm',
        'sector': 'sector',
        'forecast_json': 'forecast_json',
        'excel_metadata': 'excel_metadata',
    }
    for meta_key, col in meta_col_map.items():
        val = merged_meta.get(meta_key)
        if val:
            update_sql += f', {col}=?'
            params.append(val)
    update_sql += ' WHERE id=?'
    params.append(doc_id)
    conn.execute(update_sql, params)


# ══════════════════════════════════════════════════════════════════════════════
# Embedding  (BAAI/bge-m3, 1024-dim by default)
# ══════════════════════════════════════════════════════════════════════════════

_EMBEDDING_BLOB_V1 = b'KBEMB1:'   # legacy: zlib-compressed JSON
_EMBEDDING_BLOB_V2 = b'KBEMB2:'   # current: raw float32 binary (lossless)

def _serialize_embedding(vec: list[float] | None) -> bytes | None:
    if vec is None:
        return None
    if _NUMPY_OK:
        arr = np.array(vec, dtype=np.float32)
        return _EMBEDDING_BLOB_V2 + arr.tobytes()
    # fallback: struct pack float32
    import struct
    return _EMBEDDING_BLOB_V2 + struct.pack(f'{len(vec)}f', *vec)

def _deserialize_embedding(emb_raw: Any) -> list[float] | None:
    if emb_raw is None:
        return None
    try:
        if isinstance(emb_raw, memoryview):
            emb_raw = emb_raw.tobytes()
        if isinstance(emb_raw, bytearray):
            emb_raw = bytes(emb_raw)
        if isinstance(emb_raw, bytes):
            if emb_raw.startswith(_EMBEDDING_BLOB_V2):
                payload = emb_raw[len(_EMBEDDING_BLOB_V2):]
                if _NUMPY_OK:
                    return np.frombuffer(payload, dtype=np.float32).tolist()
                import struct
                n = len(payload) // 4
                return list(struct.unpack(f'{n}f', payload))
            if emb_raw.startswith(_EMBEDDING_BLOB_V1):
                payload = emb_raw[len(_EMBEDDING_BLOB_V1):]
                return json.loads(zlib.decompress(payload).decode('utf-8'))
            return json.loads(emb_raw.decode('utf-8'))
        if isinstance(emb_raw, str):
            return json.loads(emb_raw)
    except Exception:
        return None
    return None

def _embedding_settings() -> tuple[str, str, str]:
    """Return (api_key, base_url, model) from config.embedding, fallback to config.llm."""
    try:
        from config import CONFIG
        emb = CONFIG.get('embedding', {})
        llm = CONFIG.get('llm', {})
        key = os.getenv('EMBEDDING_API_KEY') or emb.get('api_key', '') or os.getenv('LLM_API_KEY') or llm.get('api_key', '')
        url = (os.getenv('EMBEDDING_BASE_URL') or emb.get('base_url', '') or os.getenv('LLM_BASE_URL') or llm.get('base_url', '')).rstrip('/')
        model = emb.get('model', '') or 'BAAI/bge-m3'
        return key, url, model
    except Exception:
        return '', '', 'BAAI/bge-m3'

_embed_client: httpx.Client | None = None
_embed_client_lock = threading.Lock()
_embed_semaphore = threading.Semaphore(1)


def _get_embed_client() -> httpx.Client:
    """Return a reusable httpx Client with connection pooling for embedding API."""
    global _embed_client
    with _embed_client_lock:
        if _embed_client is None or _embed_client.is_closed:
            _embed_client = httpx.Client(
                timeout=120,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return _embed_client


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Call embedding API (BAAI/bge-m3 by default). Returns list of vectors, or None on failure."""
    api_key, base_url, model = _embedding_settings()
    if not api_key or not base_url:
        return None
    if not texts:
        return []
    results: list[list[float]] = []
    batch_size = _EMBED_BATCH_SIZE
    client = _get_embed_client()
    max_retries = 5
    try:
        for i in range(0, len(texts), batch_size):
            batch = [t[:_EMBED_MAX_CHARS] for t in texts[i: i + batch_size]]
            with _embed_semaphore:
                for attempt in range(max_retries):
                    resp = client.post(
                        f'{base_url}/embeddings',
                        headers={'Authorization': f'Bearer {api_key}'},
                        json={'model': model, 'input': batch},
                    )
                    if resp.status_code == 429:
                        wait = min(2 ** attempt, 30)
                        print(f'[KB] embedding 429 rate-limited, retry {attempt+1}/{max_retries} in {wait}s', file=sys.stderr)
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json().get('data', [])
                    data_sorted = sorted(data, key=lambda x: x.get('index', 0))
                    for item in data_sorted:
                        results.append(item['embedding'])
                    break
                else:
                    print(f'[KB] embedding API failed after {max_retries} retries (429)', file=sys.stderr)
                    return None
    except Exception as e:
        print(f'[KB] embedding API error: {e}', file=sys.stderr)
        return None
    return results

def _embed_query(query: str) -> list[float] | None:
    vecs = _embed_texts([query])
    if vecs:
        return vecs[0]
    return None

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

# ══════════════════════════════════════════════════════════════════════════════
# Reranker  (BAAI/bge-reranker-v2-m3, cross-encoder precision reranking)
# ══════════════════════════════════════════════════════════════════════════════

def _rerank(query: str, texts: list[str], top_n: int | None = None) -> list[tuple[int, float]]:
    """Rerank texts against query using cross-encoder reranker API.

    Returns list of (original_index, relevance_score) sorted by score descending.
    Falls back to identity ranking on failure.
    """
    if not texts:
        return []
    api_key, base_url, _ = _embedding_settings()
    if not api_key or not base_url:
        return [(i, 1.0 / (i + 1)) for i in range(len(texts))]

    try:
        from config import CONFIG
        emb = CONFIG.get('embedding', {})
        reranker_model = emb.get('reranker_model', 'BAAI/bge-reranker-v2-m3')
    except Exception:
        reranker_model = 'BAAI/bge-reranker-v2-m3'

    try:
        resp = httpx.post(
            f'{base_url}/rerank',
            headers={'Authorization': f'Bearer {api_key}'},
            json={
                'model': reranker_model,
                'query': query,
                'documents': texts,
                'top_n': top_n or len(texts),
                'return_documents': False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])
        ranked = sorted(results, key=lambda x: x.get('relevance_score', 0), reverse=True)
        return [(r['index'], r['relevance_score']) for r in ranked]
    except Exception as e:
        print(f'[KB] reranker API error: {e}', file=sys.stderr)
        return [(i, 1.0 / (i + 1)) for i in range(len(texts))]

# ══════════════════════════════════════════════════════════════════════════════
# HyDE  (Hypothetical Document Embedding)
# ══════════════════════════════════════════════════════════════════════════════

def _hyde_expand(query: str) -> str:
    """Generate a hypothetical answer to the query, then embed that for retrieval.

    The intuition: a hypothetical answer is semantically closer to the real answer
    in embedding space than the original question is.
    Returns the hypothetical document text to be embedded instead of the query.
    """
    try:
        from .llm import request_llm_text, llm_available
        if not llm_available():
            return query
        system = (
            '你是一位金融分析师。请根据以下问题，写一段简短的假设性回答（100-200字），'
            '包含可能出现在研报中的专业术语和关键数据描述。'
            '只输出假设性回答，不要解释或添加前缀。'
        )
        hypo = request_llm_text(system, query, temperature=0.3)
        hypo = hypo.strip()
        if hypo and 30 < len(hypo) < 500:
            return hypo
    except Exception as e:
        print(f'[KB] HyDE error: {e}', file=sys.stderr)
    return query

# ══════════════════════════════════════════════════════════════════════════════
# Lost-in-the-Middle reordering
# ══════════════════════════════════════════════════════════════════════════════

def _lost_in_middle_reorder(items: list) -> list:
    """Reorder items so the most relevant ones are at the START and END.

    LLMs attend more to the beginning and end of long contexts.
    Place odd-ranked items at the start, even-ranked at the end (reversed).
    """
    if len(items) <= 2:
        return items
    start = items[::2]   # indices 0, 2, 4, ... (most relevant first)
    end = items[1::2]    # indices 1, 3, 5, ...
    end.reverse()         # least relevant in the middle
    return start + end

# ══════════════════════════════════════════════════════════════════════════════
# Index rebuild (BM25 + vector matrix)
# ══════════════════════════════════════════════════════════════════════════════

def _rebuild_index() -> None:
    global _bm25_index, _bm25_chunk_ids, _vec_chunk_ids, _vec_matrix
    with kb_conn() as conn:
        rows = conn.execute(
            'SELECT id, tokens, embedding FROM kb_chunks ORDER BY id'
        ).fetchall()

    chunk_ids = [r['id'] for r in rows]
    corpus = [r['tokens'].split() for r in rows]

    vec_ids: list[int] = []
    vec_matrix: list[list[float]] = []
    for r in rows:
        emb_raw = r['embedding']
        if emb_raw:
            vec = _deserialize_embedding(emb_raw)
            if vec is not None:
                vec_ids.append(r['id'])
                vec_matrix.append(vec)

    with _bm25_lock:
        _bm25_index = BM25Okapi(corpus) if corpus else None
        _bm25_chunk_ids = chunk_ids

    with _vec_lock:
        _vec_chunk_ids = vec_ids
        _vec_matrix = vec_matrix

# ══════════════════════════════════════════════════════════════════════════════
# Document indexing
# ══════════════════════════════════════════════════════════════════════════════

def index_document(
    doc_id: int,
    pdf_path: Path,
    excel_meta: dict[str, Any] | None = None,
    rebuild_index: bool = True,
) -> None:
    """Parse PDF → semantic chunk → embed → store, then rebuild index."""
    with _index_lock:
        _index_document_inner(doc_id, pdf_path, excel_meta=excel_meta or {}, rebuild_index=rebuild_index)

def _index_document_inner(
    doc_id: int,
    pdf_path: Path,
    excel_meta: dict[str, Any] | None = None,
    rebuild_index: bool = True,
) -> None:
    start_dt = datetime.now()
    start_text = start_dt.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with kb_conn() as conn:
            conn.execute(
                "UPDATE kb_documents SET status='indexing', index_started_at=?, indexed_at=NULL, build_seconds=NULL WHERE id=?",
                (start_text, doc_id)
            )

        text, meta, page_texts, page_blocks, extract_info = _extract_text(pdf_path)
        merged_meta: dict[str, Any] = dict(meta)
        if excel_meta:
            for k, v in excel_meta.items():
                if v:
                    merged_meta[k] = v
        merged_meta.setdefault('doc_name', pdf_path.stem)
        quality_info = _assess_document_quality(pdf_path, text, merged_meta, page_texts, extract_info)
        low_signal_summary = _build_low_signal_summary_text(pdf_path, merged_meta, quality_info, extract_info, text)
        if low_signal_summary:
            page_blocks = [{'page_no': 1, 'kind': 'paragraph', 'text': low_signal_summary, 'source': 'fallback'}] + page_blocks
            if low_signal_summary not in text:
                text = low_signal_summary if not text.strip() else f'{low_signal_summary}\n\n{text}'
        print(f'[KB] doc {doc_id} extracted {len(text)} chars, {len(page_texts)} pages, meta={merged_meta}, quality={quality_info}', file=sys.stderr)

        page_fig_titles = _extract_page_figure_titles(page_texts)
        chunk_docs = _build_structured_chunks(page_blocks, merged_meta, page_fig_titles, fallback_text=text)
        print(f'[KB] doc {doc_id} → {len(chunk_docs)} child chunks (structured), {sum(len(v) for v in page_fig_titles.values())} figure titles', file=sys.stderr)

        # batch embed child chunks (small chunks = precise retrieval)
        retrieval_texts = [str(chunk.get('retrieval_text') or chunk.get('text') or '') for chunk in chunk_docs]
        embeddings = _embed_texts(retrieval_texts)
        if embeddings:
            print(f'[KB] doc {doc_id} embeddings OK ({len(embeddings)} vectors)', file=sys.stderr)
        else:
            print(f'[KB] doc {doc_id} embeddings unavailable, using BM25 only', file=sys.stderr)

        with kb_conn() as conn:
            conn.execute('DELETE FROM kb_chunks WHERE doc_id=?', (doc_id,))
            # Check if dedup table exists
            _has_parent_table = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_parent_texts'"
            ).fetchone())
            parent_text_cache: dict[str, int] = {}  # hash → parent_text_id

            def _get_parent_text_id(conn_: Any, text: str) -> int | None:
                if not _has_parent_table:
                    return None
                import hashlib
                h = hashlib.md5(text.encode('utf-8')).hexdigest()
                if h in parent_text_cache:
                    return parent_text_cache[h]
                conn_.execute("INSERT OR IGNORE INTO kb_parent_texts(hash, text) VALUES(?, ?)", (h, text))
                row = conn_.execute("SELECT id FROM kb_parent_texts WHERE hash=?", (h,)).fetchone()
                pt_id = row[0] if row else None
                if pt_id is not None:
                    parent_text_cache[h] = pt_id
                return pt_id

            insert_rows: list[tuple[Any, ...]] = []
            for i, chunk in enumerate(chunk_docs):
                child = str(chunk.get('text') or '')
                parent = str(chunk.get('parent_text') or child)
                retrieval_text = str(chunk.get('retrieval_text') or child)
                token_list = _tokenize(retrieval_text)
                if not token_list:
                    token_list = _tokenize(child)
                if not token_list:
                    token_list = [w for w in retrieval_text.split() if w.strip()]
                tokens = ' '.join(token_list)
                emb_blob = _serialize_embedding(embeddings[i]) if embeddings and i < len(embeddings) else None
                pt_id = _get_parent_text_id(conn, parent)
                insert_rows.append(
                    (
                        doc_id,
                        i + 1,
                        child,
                        None if pt_id else parent,
                        tokens,
                        emb_blob,
                        chunk.get('page_no'),
                        chunk.get('page_start'),
                        chunk.get('page_end'),
                        chunk.get('figure_titles'),
                        chunk.get('chunk_type'),
                        chunk.get('section_title'),
                        chunk.get('section_path'),
                        retrieval_text,
                        chunk.get('entities_json'),
                        chunk.get('metric_tags'),
                        pt_id,
                    )
                )
            conn.executemany(
                'INSERT INTO kb_chunks('
                'doc_id, chunk_idx, text, parent_text, tokens, embedding, page_no, page_start, page_end, '
                'figure_titles, chunk_type, section_title, section_path, retrieval_text, entities_json, metric_tags,'
                'parent_text_id'
                ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                insert_rows,
            )
            elapsed_seconds = round((datetime.now() - start_dt).total_seconds(), 2)
            _update_document_record(conn, doc_id, 'done', len(chunk_docs), elapsed_seconds, merged_meta, quality_info)
        print(f'[KB] doc {doc_id} build done in {elapsed_seconds:.2f}s, chunks={len(chunk_docs)}', file=sys.stderr)

    except Exception as e:
        elapsed_seconds = round((datetime.now() - start_dt).total_seconds(), 2)
        print(f'[KB] ERROR indexing doc {doc_id} after {elapsed_seconds:.2f}s: {e}', file=sys.stderr)
        with kb_conn() as conn:
            conn.execute(
                "UPDATE kb_documents SET status='error', build_seconds=? WHERE id=?",
                (elapsed_seconds, doc_id)
            )
        raise
    finally:
        if rebuild_index:
            _rebuild_index()


# ══════════════════════════════════════════════════════════════════════════════
# Attachment-5 ingestion  (集成 Excel 元数据为主要数据源)
# ══════════════════════════════════════════════════════════════════════════════

def _load_excel_metadata() -> dict[str, dict[str, Any]]:
    """读取附件5中两个Excel，返回 {title: metadata_dict}，以标题为键匹配PDF。"""
    import openpyxl
    result: dict[str, dict[str, Any]] = {}

    def _read_sheet(xlsx_path: Path, is_stock: bool) -> None:
        if not xlsx_path.exists():
            return
        try:
            wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return
            headers = [str(h).strip() if h else '' for h in rows[0]]
            for data_row in rows[1:]:
                if not any(c is not None for c in data_row):
                    continue
                row = dict(zip(headers, data_row))
                title = str(row.get('title', '') or '').strip()
                if not title:
                    continue
                meta: dict[str, Any] = {}
                if is_stock:
                    if row.get('stockName'):
                        meta['company'] = str(row['stockName']).strip()
                    if row.get('stockCode'):
                        meta['stock_code'] = str(row['stockCode']).strip()
                    if row.get('emRatingName'):
                        meta['rating'] = str(row['emRatingName']).strip()
                    if row.get('researcher'):
                        meta['analyst'] = str(row['researcher']).strip()
                    if row.get('orgSName') or row.get('orgName'):
                        meta['research_firm'] = str(row.get('orgSName') or row.get('orgName') or '').strip()
                    if row.get('indvInduName'):
                        meta['sector'] = str(row['indvInduName']).strip()
                    tp_t = row.get('indvAimPriceT')
                    tp_l = row.get('indvAimPriceL')
                    if tp_t or tp_l:
                        meta['target_price'] = str(tp_t or tp_l).strip()
                    if row.get('publishDate'):
                        pd_str = str(row['publishDate']).strip()
                        dm = re.search(r'(20\d{2})-(\d{2})-(\d{2})', pd_str)
                        if dm:
                            meta['report_date'] = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
                        else:
                            dm2 = re.search(r'(20\d{2})-(\d{2})', pd_str)
                            if dm2:
                                meta['report_date'] = f"{dm2.group(1)}-{dm2.group(2)}"
                    # store full predictions as excel_metadata
                    excel_data: dict[str, Any] = {}
                    for field in ('predictLastYearEps', 'predictLastYearPe',
                                  'predictThisYearEps', 'predictThisYearPe',
                                  'predictNextYearEps', 'predictNextYearPe',
                                  'predictNextTwoYearEps', 'predictNextTwoYearPe',
                                  'indvAimPriceT', 'indvAimPriceL',
                                  'emRatingName', 'lastEmRatingName',
                                  'researcher', 'orgName', 'orgSName', 'market',
                                  'stockCode', 'stockName', 'publishDate'):
                        v = row.get(field)
                        if v is not None:
                            excel_data[field] = str(v).strip()
                    if excel_data:
                        meta['excel_metadata'] = json.dumps(excel_data, ensure_ascii=False)
                else:
                    # industry report
                    if row.get('emRatingName'):
                        meta['rating'] = str(row['emRatingName']).strip()
                    if row.get('researcher'):
                        meta['analyst'] = str(row['researcher']).strip()
                    if row.get('orgSName') or row.get('orgName'):
                        meta['research_firm'] = str(row.get('orgSName') or row.get('orgName') or '').strip()
                    if row.get('industryName'):
                        meta['sector'] = str(row['industryName']).strip()
                    if row.get('publishDate'):
                        pd_str = str(row['publishDate']).strip()
                        dm = re.search(r'(20\d{2})-(\d{2})-(\d{2})', pd_str)
                        if dm:
                            meta['report_date'] = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
                    excel_data = {k: str(v).strip() for k, v in row.items()
                                  if v is not None and k != 'title'}
                    if excel_data:
                        meta['excel_metadata'] = json.dumps(excel_data, ensure_ascii=False)
                result[title] = meta
        except Exception as e:
            print(f'[KB] Excel read error {xlsx_path.name}: {e}', file=sys.stderr)

    _read_sheet(ATTACHMENT5_DIR / '个股_研报信息.xlsx', is_stock=True)
    _read_sheet(ATTACHMENT5_DIR / '行业_研报信息.xlsx', is_stock=False)
    return result


# cache Excel metadata (loaded once per process)
_excel_meta_cache: dict[str, dict[str, Any]] | None = None
_excel_meta_lock = threading.Lock()


def _get_excel_meta() -> dict[str, dict[str, Any]]:
    global _excel_meta_cache
    with _excel_meta_lock:
        if _excel_meta_cache is None:
            _excel_meta_cache = _load_excel_metadata()
            print(f'[KB] loaded Excel metadata for {len(_excel_meta_cache)} reports', file=sys.stderr)
    return _excel_meta_cache


def _scan_attachment5() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    if not ATTACHMENT5_DIR.exists():
        return docs
    excel_meta = _get_excel_meta()
    for sub in ('个股研报', '行业研报'):
        folder = ATTACHMENT5_DIR / sub
        if not folder.exists():
            continue
        for pdf in folder.glob('*.pdf'):
            em = excel_meta.get(pdf.stem, {})
            docs.append({
                'doc_type': sub,
                'doc_name': pdf.stem,
                'doc_path': str(pdf),
                'stock_code': em.get('stock_code'),
                'stock_name': em.get('company'),
                'report_date': em.get('report_date'),
                '_excel_meta': em,
            })
    return docs


def ingest_attachment5(background: bool = False) -> list[dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    docs = _scan_attachment5()
    results: list[dict[str, Any]] = []
    pending_tasks: list[tuple[int, Path, dict[str, Any]]] = []
    for d in docs:
        em = d.pop('_excel_meta', {})
        with kb_conn() as conn:
            existing = conn.execute(
                'SELECT id, status FROM kb_documents WHERE doc_path=?', (d['doc_path'],)
            ).fetchone()
            if existing:
                doc_id = existing['id']
                status = existing['status']
                if status == 'skipped':
                    conn.execute("UPDATE kb_documents SET status='pending' WHERE id=?", (doc_id,))
                    status = 'pending'
                # update Excel-sourced fields on existing record
                if em:
                    _apply_excel_meta_update(conn, doc_id, em)
            else:
                cur = conn.execute(
                    'INSERT INTO kb_documents(doc_type,doc_name,doc_path,stock_code,stock_name,report_date)'
                    ' VALUES(?,?,?,?,?,?)',
                    (d['doc_type'], d['doc_name'], d['doc_path'],
                     d['stock_code'], d['stock_name'], d['report_date'])
                )
                doc_id = cur.lastrowid
                status = 'pending'
                if em:
                    _apply_excel_meta_update(conn, doc_id, em)
        results.append({'id': doc_id, 'doc_name': d['doc_name'], 'status': status})
        if status in ('pending', 'error'):
            pending_tasks.append((doc_id, Path(d['doc_path']), em))

    if not pending_tasks:
        return results

    if background:
        for doc_id, pdf_path, em in pending_tasks:
            threading.Thread(
                target=index_document,
                args=(doc_id, pdf_path),
                daemon=True,
                kwargs={'excel_meta': em, 'rebuild_index': True},
            ).start()
    else:
        indexed_count = 0
        total = len(pending_tasks)
        print(f'[KB] parallel ingest: {total} docs with {_INDEX_CONCURRENCY} workers', file=sys.stderr)

        def _worker(task: tuple[int, Path, dict[str, Any]]) -> int:
            doc_id, pdf_path, em = task
            index_document(doc_id, pdf_path, excel_meta=em, rebuild_index=False)
            return doc_id

        with ThreadPoolExecutor(max_workers=_INDEX_CONCURRENCY) as pool:
            futures = {pool.submit(_worker, t): t for t in pending_tasks}
            for future in as_completed(futures):
                doc_id = futures[future][0]
                try:
                    future.result()
                    indexed_count += 1
                    if indexed_count % 20 == 0 or indexed_count == total:
                        print(f'[KB] progress: {indexed_count}/{total} docs indexed', file=sys.stderr)
                except Exception as e:
                    print(f'[KB] worker error doc {doc_id}: {e}', file=sys.stderr)

        if indexed_count > 0:
            _rebuild_index()
            print(f'[KB] batch rebuild index once after ingest ({indexed_count}/{total} docs)', file=sys.stderr)
    return results


def _apply_excel_meta_update(conn: Any, doc_id: int, em: dict[str, Any]) -> None:
    """Write Excel-sourced metadata fields directly to kb_documents."""
    _EXCEL_COL_MAP = {
        'report_date': 'report_date',
        'company':     'stock_name',
        'stock_code':  'stock_code',
        'rating':        'rating',
        'analyst':       'analyst',
        'research_firm': 'research_firm',
        'sector':        'sector',
        'target_price':  'target_price',
        'excel_metadata': 'excel_metadata',
    }
    sets = []
    params: list[Any] = []
    for meta_key, col in _EXCEL_COL_MAP.items():
        val = em.get(meta_key)
        if val:
            sets.append(f'{col}=?')
            params.append(val)
    if sets:
        params.append(doc_id)
        conn.execute(f"UPDATE kb_documents SET {', '.join(sets)} WHERE id=?", params)


# ── public: add user-uploaded PDF ─────────────────────────────────────────────

def add_uploaded_pdf(
    filename: str,
    data: bytes,
    doc_type: str = '用户上传',
    stock_code: str | None = None,
    stock_name: str | None = None,
) -> dict[str, Any]:
    dest = KB_DOCS_DIR / filename
    dest.write_bytes(data)
    with kb_conn() as conn:
        existing = conn.execute('SELECT id FROM kb_documents WHERE doc_path=?', (str(dest),)).fetchone()
        if existing:
            doc_id = existing['id']
            conn.execute(
                "UPDATE kb_documents SET status='pending', index_started_at=NULL, build_seconds=NULL WHERE id=?",
                (doc_id,)
            )
        else:
            cur = conn.execute(
                'INSERT INTO kb_documents(doc_type,doc_name,doc_path,stock_code,stock_name)'
                ' VALUES(?,?,?,?,?)',
                (doc_type, Path(filename).stem, str(dest), stock_code, stock_name)
            )
            doc_id = cur.lastrowid
    threading.Thread(target=index_document, args=(doc_id, dest), daemon=True).start()
    return {'id': doc_id, 'doc_name': Path(filename).stem, 'status': 'indexing'}


# ── public: delete a document ─────────────────────────────────────────────────

def delete_document(doc_id: int) -> None:
    with kb_conn() as conn:
        row = conn.execute('SELECT doc_path FROM kb_documents WHERE id=?', (doc_id,)).fetchone()
        if not row:
            raise KeyError(f'文档 {doc_id} 不存在')
        conn.execute('DELETE FROM kb_documents WHERE id=?', (doc_id,))
    path = Path(row['doc_path'])
    if path.parent == KB_DOCS_DIR and path.exists():
        path.unlink(missing_ok=True)
    _rebuild_index()


# ── public: list documents ────────────────────────────────────────────────────

def list_documents() -> list[dict[str, Any]]:
    with kb_conn() as conn:
        rows = conn.execute(
            'SELECT id,doc_type,doc_name,doc_path,stock_code,stock_name,'
            'report_date,current_price,target_price,rating,analyst,research_firm,'
            'sector,forecast_json,excel_metadata,doc_kind,quality_score,quality_notes,is_abstract,has_ocr,page_count,text_density,'
            'index_started_at,total_chunks,build_seconds,indexed_at,status FROM kb_documents ORDER BY id'
        ).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# LLM query rewriting
# ══════════════════════════════════════════════════════════════════════════════

def _rewrite_query(query: str) -> str:
    """Use LLM to expand/rewrite the query for better retrieval. Falls back to original."""
    try:
        from .llm import request_llm_text, llm_available
        if not llm_available():
            return query
        system = (
            '你是一个金融研报检索专家。请将用户的问题改写为更适合检索的关键词查询语句，'
            '保留核心实体（公司名、指标名、时间）并扩展相关专业术语，'
            '输出一行简洁的检索语句，不要解释。'
        )
        rewritten = request_llm_text(system, query, temperature=0.1)
        rewritten = rewritten.strip().split('\n')[0].strip()
        if rewritten and len(rewritten) <= 200:
            return rewritten
    except Exception as e:
        print(f'[KB] query rewrite error: {e}', file=sys.stderr)
    return query


# ══════════════════════════════════════════════════════════════════════════════
# Hybrid retrieval: BM25 + vector cosine + RRF fusion
# ══════════════════════════════════════════════════════════════════════════════

def _rrf_score(rank: int, k: int = _RRF_K) -> float:
    return 1.0 / (k + rank + 1)


def _bm25_rank(query_tokens: list[str], chunk_ids: list[int], index: BM25Okapi) -> dict[int, int]:
    """Return {chunk_id: 0-based rank} for BM25."""
    scores = index.get_scores(query_tokens)
    ranked = sorted(zip(scores, chunk_ids), key=lambda x: -x[0])
    return {cid: i for i, (_, cid) in enumerate(ranked)}


def _vector_rank(query_vec: list[float]) -> dict[int, int]:
    """Return {chunk_id: 0-based rank} for vector cosine similarity."""
    with _vec_lock:
        vids = list(_vec_chunk_ids)
        vmat = list(_vec_matrix)
    if not vids or not query_vec:
        return {}
    sims = [(i, _cosine(query_vec, v)) for i, v in enumerate(vmat)]
    sims.sort(key=lambda x: -x[1])
    return {vids[i]: rank for rank, (i, _) in enumerate(sims)}


def _fuse_rrf(
    bm25_ranks: dict[int, int],
    vec_ranks: dict[int, int],
    all_ids: list[int],
    bm25_w: float = _BM25_WEIGHT,
    vec_w: float = _VECTOR_WEIGHT,
) -> list[tuple[int, float]]:
    """Combine BM25 and vector rankings via weighted RRF."""
    scores: dict[int, float] = {}
    for cid in all_ids:
        s = 0.0
        if cid in bm25_ranks:
            s += bm25_w * _rrf_score(bm25_ranks[cid])
        if cid in vec_ranks:
            s += vec_w * _rrf_score(vec_ranks[cid])
        if s > 0:
            scores[cid] = s
    return sorted(scores.items(), key=lambda x: -x[1])


def _load_company_maps() -> tuple[dict[str, str], dict[str, str]]:
    try:
        from config import CONFIG
        name_to_code = {
            str(name).strip(): str(code).strip()
            for name, code in (CONFIG.get('stock_abbr_to_code', {}) or {}).items()
            if str(name).strip() and str(code).strip()
        }
    except Exception:
        name_to_code = {}
    code_to_name = {code: name for name, code in name_to_code.items()}
    return name_to_code, code_to_name


def _infer_query_company(query: str) -> tuple[str, str]:
    name_to_code, code_to_name = _load_company_maps()
    best_name = ''
    for name in name_to_code:
        if name and name in query and len(name) > len(best_name):
            best_name = name
    stock_code = ''
    code_match = re.search(r'(?<!\d)(\d{6})(?!\d)', query)
    if code_match:
        stock_code = code_match.group(1)
    if not stock_code and best_name:
        stock_code = name_to_code.get(best_name, '')
    if not best_name and stock_code:
        best_name = code_to_name.get(stock_code, '')
    return best_name, stock_code


def _parse_query_filters(query: str, company_filter: str | None = None, doc_type_preference: str = '') -> dict[str, Any]:
    parsed_company, parsed_code = _infer_query_company(query)
    # When query asks for research reports (industry or general), don't hard-filter by company
    _wants_broad_kb = bool(re.search(r'(?:行业研报|行业报告|行业.*研报|结合.*行业|结合.*研报|结合.*报告|请.*研报)', query))
    if company_filter is not None:
        company = company_filter.strip()
    else:
        company = '' if _wants_broad_kb else parsed_company
    report_years = _dedupe_keep_order(re.findall(r'20\d{2}', query))[:4]
    metric_tags = _extract_metric_tags(query)
    section_terms = []
    for term in ('投资要点', '核心观点', '盈利预测', '风险提示', '估值', '现金流量表', '资产负债表', '利润表', '管理层讨论与分析'):
        if term in query:
            section_terms.append(term)
    if '现金流' in query and '现金流量表' not in section_terms:
        section_terms.append('现金流量表')
    if ('风险' in query or '风险点' in query) and '风险提示' not in section_terms:
        section_terms.append('风险提示')
    if ('预测' in query or '业绩' in query) and '盈利预测' not in section_terms:
        section_terms.append('盈利预测')
    if ('估值' in query or 'PE' in query.upper() or 'PB' in query.upper()) and '估值' not in section_terms:
        section_terms.append('估值')
    doc_terms: list[str] = []
    explicit_doc_pref = doc_type_preference.strip()
    if explicit_doc_pref:
        doc_terms.append(explicit_doc_pref)
    if '行业' in query:
        doc_terms.append('行业')
    if '个股' in query or '公司研报' in query or '点评' in query:
        doc_terms.append('个股')
    if '年报' in query or '半年报' in query or '季报' in query:
        doc_terms.append('financial_report')
    is_exact = bool(_EXACT_QUERY_RE.search(query)) and not bool(_ANALYTICAL_QUERY_RE.search(query))
    return {
        'company': company,
        'stock_code': parsed_code,
        'report_years': report_years,
        'metric_tags': _dedupe_keep_order(metric_tags),
        'section_terms': _dedupe_keep_order(section_terms),
        'doc_terms': _dedupe_keep_order(doc_terms),
        'is_exact': is_exact,
    }


def _restrict_ranks(rank_map: dict[int, int], allowed_ids: set[int]) -> dict[int, int]:
    if not allowed_ids:
        return rank_map
    ranked = sorted(((rank, cid) for cid, rank in rank_map.items() if cid in allowed_ids), key=lambda x: x[0])
    return {cid: idx for idx, (_rank, cid) in enumerate(ranked)}


def _resolve_prefilter_chunk_ids(filters: dict[str, Any]) -> list[int]:
    clauses = ["d.status='done'"]
    params: list[Any] = []

    company = str(filters.get('company') or '').strip()
    stock_code = str(filters.get('stock_code') or '').strip()
    report_years = [str(y).strip() for y in (filters.get('report_years') or []) if str(y).strip()]
    metric_tags = [str(t).strip() for t in (filters.get('metric_tags') or []) if str(t).strip()]
    section_terms = [str(t).strip() for t in (filters.get('section_terms') or []) if str(t).strip()]
    doc_terms = [str(t).strip() for t in (filters.get('doc_terms') or []) if str(t).strip()]

    if company:
        clauses.append('(IFNULL(d.stock_name, "") LIKE ? OR IFNULL(c.retrieval_text, "") LIKE ?)')
        params.extend([f'%{company}%', f'%{company}%'])
    if stock_code:
        clauses.append('(IFNULL(d.stock_code, "")=? OR IFNULL(c.retrieval_text, "") LIKE ?)')
        params.extend([stock_code, f'%{stock_code}%'])
    if report_years:
        clauses.append('(' + ' OR '.join(['IFNULL(d.report_date, "") LIKE ? OR IFNULL(c.retrieval_text, "") LIKE ?'] * len(report_years)) + ')')
        for year in report_years:
            params.extend([f'{year}%', f'%{year}%'])
    if doc_terms:
        clauses.append('(' + ' OR '.join(['IFNULL(d.doc_type, "") LIKE ? OR IFNULL(d.doc_kind, "") LIKE ?'] * len(doc_terms)) + ')')
        for term in doc_terms:
            params.extend([f'%{term}%', f'%{term}%'])
    if section_terms:
        clauses.append('(' + ' OR '.join(['IFNULL(c.section_title, "") LIKE ? OR IFNULL(c.section_path, "") LIKE ? OR IFNULL(c.retrieval_text, "") LIKE ?'] * len(section_terms)) + ')')
        for term in section_terms:
            params.extend([f'%{term}%', f'%{term}%', f'%{term}%'])
    if metric_tags:
        clauses.append('(' + ' OR '.join(['IFNULL(c.metric_tags, "") LIKE ? OR IFNULL(c.retrieval_text, "") LIKE ?'] * len(metric_tags)) + ')')
        for tag in metric_tags:
            params.extend([f'%{tag}%', f'%{tag}%'])

    if len(clauses) == 1:
        return []

    where_sql = ' AND '.join(clauses)
    sql = (
        'SELECT c.id FROM kb_chunks c '
        'JOIN kb_documents d ON c.doc_id=d.id '
        f'WHERE {where_sql} '
        'ORDER BY c.id'
    )
    with kb_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [int(r['id']) for r in rows]


def _row_matches_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    company = str(filters.get('company') or '').strip()
    stock_code = str(filters.get('stock_code') or '').strip()
    report_years = [str(y).strip() for y in (filters.get('report_years') or []) if str(y).strip()]
    metric_tags = [str(t).strip() for t in (filters.get('metric_tags') or []) if str(t).strip()]
    section_terms = [str(t).strip() for t in (filters.get('section_terms') or []) if str(t).strip()]

    # When no company filter (industry queries), skip strict year/section filters
    # and rely on BM25/vector/reranker for relevance
    _relaxed = not company

    text_blob = '\n'.join([
        str(row.get('retrieval_text') or ''),
        str(row.get('section_title') or ''),
        str(row.get('metric_tags') or ''),
        str(row.get('stock_name') or ''),
        str(row.get('stock_code') or ''),
    ])
    if company and company not in str(row.get('stock_name') or '') and company not in text_blob:
        return False
    if not _relaxed:
        if stock_code and stock_code not in str(row.get('stock_code') or '') and stock_code not in text_blob:
            return False
        if report_years and not any(str(row.get('report_date') or '').startswith(year) or year in text_blob for year in report_years):
            return False
        if section_terms and not any(term in text_blob for term in section_terms):
            return False
        if metric_tags and not any(tag in text_blob for tag in metric_tags):
            return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Adaptive Retrieval  (LLM judges whether/how to retrieve)
# ══════════════════════════════════════════════════════════════════════════════

def adaptive_retrieval_judge(question: str) -> dict[str, Any]:
    """Use LLM to decide the optimal retrieval strategy for a question.

    Returns dict with:
      - need_kb: bool – whether knowledge base retrieval is needed
      - retrieval_rounds: int – how many retrieval rounds (1 or 2)
      - suggested_top_k: int – recommended top_k for this question type
      - reason: str – brief explanation
    """
    default = {'need_kb': True, 'retrieval_rounds': 1, 'suggested_top_k': 5, 'reason': '默认策略'}
    try:
        from .llm import request_llm_text, llm_available
        if not llm_available():
            return default
        system = (
            '你是一个检索策略分析器。根据用户的问题，判断最优的知识库检索策略。\n'
            '输出JSON格式（不要额外文字）：\n'
            '{"need_kb": true/false, "retrieval_rounds": 1或2, "suggested_top_k": 3-15, "reason": "简短说明"}\n\n'
            '判断规则：\n'
            '- 闲聊/打招呼/帮助类问题 → need_kb=false\n'
            '- 简单事实查询（某公司评级、目标价）→ retrieval_rounds=1, top_k=3-5\n'
            '- 对比分析（多个公司、多个指标对比）→ retrieval_rounds=1, top_k=8-12\n'
            '- 复杂分析（原因分析、趋势预测、多维度归因）→ retrieval_rounds=2, top_k=8-15\n'
            '- 列举类问题（有哪些、列出）→ retrieval_rounds=1, top_k=10-15'
        )
        raw = request_llm_text(system, question, temperature=0.1)
        # Extract JSON from response
        raw = raw.strip()
        m = re.search(r'\{[^}]+\}', raw)
        if m:
            result = json.loads(m.group(0))
            result['need_kb'] = bool(result.get('need_kb', True))
            result['retrieval_rounds'] = min(max(int(result.get('retrieval_rounds', 1)), 1), 2)
            result['suggested_top_k'] = min(max(int(result.get('suggested_top_k', 5)), 3), 20)
            return result
    except Exception as e:
        print(f'[KB] adaptive retrieval judge error: {e}', file=sys.stderr)
    return default


# ══════════════════════════════════════════════════════════════════════════════
# Self-RAG  (LLM filters irrelevant chunks after retrieval)
# ══════════════════════════════════════════════════════════════════════════════

def self_rag_filter(question: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use LLM to judge relevance of each retrieved chunk, filtering out irrelevant ones.

    Each chunk is scored as 'relevant', 'partial', or 'irrelevant'.
    Only 'relevant' and 'partial' chunks are kept.
    """
    if not chunks or len(chunks) <= 1:
        return chunks
    try:
        from .llm import request_llm_text, llm_available
        if not llm_available():
            return chunks

        # Build compact chunk descriptions for LLM
        chunk_descs = []
        for i, c in enumerate(chunks):
            text_preview = c.get('text', '')[:200].replace('\n', ' ')
            source = c.get('doc_name', '')
            chunk_descs.append(f'[{i}] 来源:{source} 内容:{text_preview}')

        system = (
            '你是一个检索结果质量评估器。判断每条检索结果与用户问题的相关性。\n'
            '输出JSON数组，每个元素格式：{"idx": 编号, "relevance": "relevant"/"partial"/"irrelevant"}\n'
            '只输出JSON数组，不要其他文字。\n'
            '判断标准：\n'
            '- relevant：直接回答问题或包含关键信息\n'
            '- partial：包含部分相关信息，可能有用\n'
            '- irrelevant：与问题无关，不应使用'
        )
        user = f'问题：{question}\n\n检索结果：\n' + '\n'.join(chunk_descs)
        raw = request_llm_text(system, user, temperature=0.1)

        # Parse JSON array
        raw = raw.strip()
        m = re.search(r'\[[\s\S]*\]', raw)
        if m:
            judgments = json.loads(m.group(0))
            irrelevant_ids = set()
            for j in judgments:
                if j.get('relevance') == 'irrelevant':
                    irrelevant_ids.add(int(j.get('idx', -1)))
            filtered = [c for i, c in enumerate(chunks) if i not in irrelevant_ids]
            removed = len(chunks) - len(filtered)
            if removed > 0:
                print(f'[KB] Self-RAG filtered out {removed}/{len(chunks)} irrelevant chunks', file=sys.stderr)
            return filtered if filtered else chunks[:1]  # keep at least 1
    except Exception as e:
        print(f'[KB] Self-RAG filter error: {e}', file=sys.stderr)
    return chunks


# ══════════════════════════════════════════════════════════════════════════════
# Multi-step Retrieval  (retrieve → gap analysis → second retrieval)
# ══════════════════════════════════════════════════════════════════════════════

def multi_step_retrieve(
    question: str,
    first_results: list[dict[str, Any]],
    top_k: int = TOP_K,
    company_filter: str = '',
    doc_type_preference: str = '',
    max_per_doc: int = 2,
) -> list[dict[str, Any]]:
    """Analyze gaps in first retrieval results and perform a targeted second retrieval.

    Steps:
      1. LLM analyzes what information is missing from first results
      2. LLM generates a refined follow-up query targeting the gaps
      3. Second retrieval with the refined query
      4. Merge and deduplicate results from both rounds
    """
    if not first_results:
        return first_results
    try:
        from .llm import request_llm_text, llm_available
        if not llm_available():
            return first_results

        # Summarize first-round results for gap analysis
        summaries = []
        for i, r in enumerate(first_results[:5]):
            text_preview = r.get('text', '')[:200].replace('\n', ' ')
            summaries.append(f'[{i+1}] {r.get("doc_name","")}: {text_preview}')

        system = (
            '你是一个检索策略优化器。分析当前检索结果是否完整回答了问题。\n'
            '输出JSON格式：{"has_gap": true/false, "missing": "缺失的信息描述", "refined_query": "针对缺失信息的新检索语句"}\n'
            '只输出JSON，不要其他文字。\n'
            '如果当前结果已经足够回答问题，设置has_gap=false。'
        )
        user = f'问题：{question}\n\n当前检索结果：\n' + '\n'.join(summaries)
        raw = request_llm_text(system, user, temperature=0.1)

        m = re.search(r'\{[^}]+\}', raw)
        if not m:
            return first_results
        gap_info = json.loads(m.group(0))

        if not gap_info.get('has_gap', False):
            print(f'[KB] Multi-step: no gap detected, skipping second retrieval', file=sys.stderr)
            return first_results

        refined_query = gap_info.get('refined_query', '')
        if not refined_query or len(refined_query) < 5:
            return first_results

        print(f'[KB] Multi-step: gap="{gap_info.get("missing","")}", query2="{refined_query}"', file=sys.stderr)

        # Second retrieval with the refined query (no HyDE/rewrite to avoid recursion)
        second_results = search_kb(
            refined_query,
            top_k=top_k,
            company_filter=company_filter,
            doc_type_preference=doc_type_preference,
            max_per_doc=max_per_doc,
            rewrite_query=False,  # already refined by LLM
        )

        # Merge: first results take priority, add non-duplicate second results
        existing_ids = {r['chunk_id'] for r in first_results}
        merged = list(first_results)
        for r in second_results:
            if r['chunk_id'] not in existing_ids:
                existing_ids.add(r['chunk_id'])
                merged.append(r)

        print(f'[KB] Multi-step: {len(first_results)} + {len(second_results) - len([r for r in second_results if r["chunk_id"] in {x["chunk_id"] for x in first_results}])} new = {len(merged)} total', file=sys.stderr)
        return merged

    except Exception as e:
        print(f'[KB] Multi-step retrieval error: {e}', file=sys.stderr)
    return first_results


# ══════════════════════════════════════════════════════════════════════════════
# Public: search_kb  (hybrid retrieval + LLM re-ranking optional)
# ══════════════════════════════════════════════════════════════════════════════

def search_kb(
    query: str,
    top_k: int = TOP_K,
    min_score: float = 0.0,
    company_filter: str | None = None,
    doc_type_preference: str = '',
    max_per_doc: int = 2,
    rewrite_query: bool = True,
) -> list[dict[str, Any]]:
    """Advanced retrieval pipeline: Query Rewrite → HyDE → BM25+Vector RRF → Reranker → Parent-text → LitM.

    Pipeline:
      1. LLM query rewrite (keyword expansion)
      2. HyDE: generate hypothetical answer, embed that for vector search
      3. BM25 ranking on rewritten query tokens
      4. Vector ranking on HyDE-expanded embedding
      5. RRF fusion of BM25 + vector ranks
      6. Fetch top candidates from DB
      7. Cross-encoder reranker for precision re-scoring
      8. Return parent_text (larger context) instead of child text
      9. Lost-in-the-Middle reordering for optimal LLM attention
    """
    global _bm25_index, _bm25_chunk_ids

    # lazy rebuild
    if _bm25_index is None:
        _rebuild_index()

    with _bm25_lock:
        index = _bm25_index
        all_ids = list(_bm25_chunk_ids)

    if not all_ids:
        return []

    query_filters = _parse_query_filters(query, company_filter=company_filter, doc_type_preference=doc_type_preference)
    effective_company_filter = str(query_filters.get('company') or (company_filter if company_filter else '') or '').strip()
    boost_terms = _dedupe_keep_order([
        *(query_filters.get('doc_terms') or []),
        doc_type_preference.strip(),
    ])
    prefilter_ids = _resolve_prefilter_chunk_ids(query_filters)
    allowed_id_set = set(prefilter_ids)
    search_space_ids = [cid for cid in all_ids if cid in allowed_id_set] if allowed_id_set else list(all_ids)
    if query_filters.get('is_exact') and allowed_id_set:
        print(f'[KB] metadata prefilter matched {len(search_space_ids)} chunks for query="{query}"', file=sys.stderr)
    if not search_space_ids:
        search_space_ids = list(all_ids)

    # ── Step 1+2: query rewriting + HyDE in parallel ──────────────────────────
    from concurrent.futures import ThreadPoolExecutor as _KbStepTPE
    _do_rewrite = rewrite_query
    with _KbStepTPE(max_workers=2) as _step_pool:
        _rewrite_fut = _step_pool.submit(_rewrite_query, query) if _do_rewrite else None
        _hyde_fut = _step_pool.submit(_hyde_expand, query)
        effective_query = _rewrite_fut.result() if _rewrite_fut else query
        hyde_text = _hyde_fut.result()
    if effective_query != query:
        print(f'[KB] query rewrite: "{query}" → "{effective_query}"', file=sys.stderr)
    if hyde_text != query:
        print(f'[KB] HyDE generated ({len(hyde_text)} chars)', file=sys.stderr)

    # ── Step 3: BM25 ranking (on rewritten query) ────────────────────────────
    bm25_ranks: dict[int, int] = {}
    if index:
        tokens = _tokenize(effective_query)
        if tokens:
            bm25_ranks = _bm25_rank(tokens, all_ids, index)
            if allowed_id_set:
                bm25_ranks = _restrict_ranks(bm25_ranks, allowed_id_set)

    # ── Step 4: Vector ranking (on HyDE-expanded text) ───────────────────────
    vec_ranks: dict[int, int] = {}
    query_vec = _embed_query(hyde_text)
    if query_vec:
        vec_ranks = _vector_rank(query_vec)
        if allowed_id_set:
            vec_ranks = _restrict_ranks(vec_ranks, allowed_id_set)

    # ── Step 5: RRF fusion ───────────────────────────────────────────────────
    if vec_ranks:
        fused = _fuse_rrf(bm25_ranks, vec_ranks, search_space_ids)
    else:
        if index and bm25_ranks:
            fused = sorted(bm25_ranks.items(), key=lambda x: x[1])
            fused = [(cid, _rrf_score(rank)) for cid, rank in fused]
            if allowed_id_set:
                fused = [(cid, score) for cid, score in fused if cid in allowed_id_set]
        else:
            return []

    # ── Step 6: Fetch top candidates from DB ─────────────────────────────────
    fetch_limit = max(top_k * 6, _RERANK_CANDIDATES)
    top_candidates = [(cid, score) for cid, score in fused[:fetch_limit] if score >= min_score]
    if not top_candidates:
        return []

    top_ids = [cid for cid, _ in top_candidates]
    id_to_score = {cid: score for cid, score in top_candidates}

    placeholders = ','.join('?' * len(top_ids))
    with kb_conn() as conn:
        rows = conn.execute(
            f'SELECT c.id, c.text, COALESCE(pt.text, c.parent_text) AS parent_text,'
            f' c.chunk_idx, c.page_no, c.page_start, c.page_end, c.figure_titles,'
            f' c.chunk_type, c.section_title, c.section_path, c.retrieval_text, c.entities_json, c.metric_tags,'
            f' d.id AS doc_id, d.doc_name, d.doc_path, d.doc_type, d.doc_kind, d.stock_code, d.stock_name,'
            f' d.report_date, d.rating, d.analyst, d.research_firm,'
            f' d.current_price, d.target_price, d.forecast_json, d.excel_metadata'
            f' FROM kb_chunks c JOIN kb_documents d ON c.doc_id=d.id'
            f' LEFT JOIN kb_parent_texts pt ON c.parent_text_id=pt.id'
            f' WHERE c.id IN ({placeholders})',
            top_ids,
        ).fetchall()

    # Build candidate list with company filter and doc_type boost
    candidates = []
    for r in rows:
        raw_score = float(id_to_score.get(r['id'], 0))
        if effective_company_filter and r['stock_name'] and effective_company_filter not in (r['stock_name'] or ''):
            continue
        eff_score = raw_score
        if boost_terms and any(term and (term in (r['doc_type'] or '') or term in (r['doc_kind'] or '')) for term in boost_terms):
            eff_score *= 1.35
        candidate = {
            'chunk_id': r['id'],
            'chunk_idx': r['chunk_idx'],
            'source_title': r['doc_name'],
            'doc_id': r['doc_id'],
            'page_no': r['page_no'],
            'page_start': r['page_start'],
            'page_end': r['page_end'],
            'rrf_score': round(raw_score, 6),
            'effective_score': eff_score,
            'child_text': r['text'],
            'text': r['parent_text'] or r['text'],  # Step 8: return parent context
            'retrieval_text': r['retrieval_text'] or r['text'],
            'doc_name': r['doc_name'],
            'doc_path': r['doc_path'],
            'doc_type': r['doc_type'],
            'doc_kind': r['doc_kind'] or '',
            'stock_code': r['stock_code'] or '',
            'stock_name': r['stock_name'] or '',
            'report_date': r['report_date'] or '',
            'rating': r['rating'] or '',
            'analyst': r['analyst'] or '',
            'research_firm': r['research_firm'] or '',
            'current_price': r['current_price'] or '',
            'target_price': r['target_price'] or '',
            'forecast_json': r['forecast_json'] or '',
            'excel_metadata': r['excel_metadata'] or '',
            'figure_titles': r['figure_titles'] or '',
            'chunk_type': r['chunk_type'] or '',
            'section_title': r['section_title'] or '',
            'section_path': r['section_path'] or '',
            'entities_json': r['entities_json'] or '',
            'metric_tags': r['metric_tags'] or '',
        }
        if not _row_matches_filters(candidate, query_filters):
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda x: -x['effective_score'])

    # ── Step 7: Reranker (cross-encoder precision re-scoring) ────────────────
    rerank_pool = candidates[:_RERANK_CANDIDATES]
    if len(rerank_pool) > 1:
        rerank_texts = [c['retrieval_text'] for c in rerank_pool]
        reranked = _rerank(query, rerank_texts, top_n=len(rerank_pool))
        reranked_candidates = []
        for orig_idx, rel_score in reranked:
            if 0 <= orig_idx < len(rerank_pool):
                c = rerank_pool[orig_idx]
                c['score'] = round(rel_score, 6)
                reranked_candidates.append(c)
        if reranked_candidates:
            candidates = reranked_candidates
            print(f'[KB] reranker re-scored {len(reranked_candidates)} candidates', file=sys.stderr)
    else:
        for c in candidates:
            c['score'] = c['rrf_score']

    # ── per-doc deduplication ─────────────────────────────────────────────────
    doc_count: dict[int, int] = {}
    results = []
    seen_parents: set[str] = set()
    for c in candidates:
        did = c['doc_id']
        if doc_count.get(did, 0) >= max_per_doc:
            continue
        # Deduplicate by parent_text to avoid returning overlapping context
        parent_key = f"{did}:{c['text'][:80]}"
        if parent_key in seen_parents:
            continue
        seen_parents.add(parent_key)
        doc_count[did] = doc_count.get(did, 0) + 1
        # Clean up internal scoring fields (keep child_text for evidence-level refs)
        for key in ('effective_score', 'rrf_score'):
            c.pop(key, None)
        results.append(c)
        if len(results) >= top_k:
            break

    # ── Step 9: Lost-in-the-Middle reordering ────────────────────────────────
    results = _lost_in_middle_reorder(results)
    return results
