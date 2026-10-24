"""Unified dialogue state for the intelligent assistant pipeline.

DialogueState is a structured dataclass that flows through all layers:
  Layer 0 (preprocess) → Layer 1 (intent) → Layer 2 (route) → Layer 3 (execute) → Layer 4 (response)

Each slot records its origin (current / history / llm / rule / default) so that
topic-switch detection and slot TTL can make informed decisions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SlotSource:
    """A single slot value with provenance tracking."""
    value: Any = None
    origin: str = ''       # 'current' | 'history' | 'llm' | 'rule' | 'default'
    confidence: float = 1.0
    turn_age: int = 0      # how many turns ago this value was set (0 = current)

    def __bool__(self) -> bool:
        if self.value is None:
            return False
        if isinstance(self.value, str) and not self.value.strip():
            return False
        if isinstance(self.value, dict) and not self.value:
            return False
        return True


@dataclass
class DialogueState:
    """Unified dialogue state that flows through the entire request lifecycle."""

    # ── raw input ──
    raw_question: str = ''
    rewritten_question: str = ''
    history: list[dict[str, Any]] = field(default_factory=list)

    # ── slots ──
    company: SlotSource = field(default_factory=SlotSource)
    period: SlotSource = field(default_factory=SlotSource)
    metric: SlotSource = field(default_factory=SlotSource)

    # ── intent ──
    query_kind: str = 'general'       # metric | cash_flow | ranking | general
    analysis_mode: str = 'general'    # single | trend | ranking | general
    is_multi_intent: bool = False
    is_follow_up: bool = False
    is_topic_switch: bool = False
    is_small_talk: bool = False

    # ── missing fields ──
    required: list[str] = field(default_factory=list)

    # ── execution trace (for dynamic attribution) ──
    execution_steps: list[dict[str, str]] = field(default_factory=list)

    def record_step(self, label: str, status: str, detail: str = ''):
        self.execution_steps.append({
            'label': label, 'status': status, 'detail': detail
        })

    @property
    def company_value(self) -> str:
        return str(self.company.value or '')

    @property
    def period_value(self) -> str:
        return str(self.period.value or '')

    @property
    def metric_value(self) -> dict:
        return dict(self.metric.value) if isinstance(self.metric.value, dict) else {}

    @property
    def effective_question(self) -> str:
        return self.rewritten_question or self.raw_question


# ── follow-up detection ──────────────────────────────────────────────────────

_FOLLOW_UP_PATTERN = re.compile(
    r'^(为什么|然后呢|继续|详细说说|具体呢|怎么回事|还有吗|说说看|'
    r'展开讲讲|多说点|为啥|咋回事|啥原因|什么原因|接着说|'
    r'再详细点|能展开吗|深入分析|解释一下|说明一下|分析一下原因)[？?。！!，,\s]*$'
)

_PRONOUN_FOLLOW_UP = re.compile(
    r'(^那.{1,10}呢|^它的|^这家公司|^该公司|^上面|^这些|^那个|^那呢|'
    r'^跟上次比|^和之前比|^同比|^环比如何|^再看看|^其他指标|^还有什么)'
)


def is_follow_up(question: str) -> bool:
    q = question.strip()
    if _FOLLOW_UP_PATTERN.match(q):
        return True
    if _PRONOUN_FOLLOW_UP.match(q):
        return True
    if len(q) <= 8 and re.search(r'[呢吗？?]$', q):
        return True
    return False


# ── topic switch detection ───────────────────────────────────────────────────

_TOPIC_SWITCH_SIGNALS = re.compile(
    r'(换一个|换个|另一个|不说这个|不聊这个|问个别的|说说别的|'
    r'另外一个公司|其他公司|除了.+还有)'
)


def detect_topic_switch(
    question: str,
    history: list[dict[str, Any]],
    company_names: list[str],
) -> bool:
    if _TOPIC_SWITCH_SIGNALS.search(question):
        return True
    # current question mentions a different company than the last turn
    curr_company = _extract_company(question, company_names)
    prev_company = _last_company_from_history(history, company_names)
    if curr_company and prev_company and curr_company != prev_company:
        return True
    return False


def _extract_company(text: str, company_names: list[str]) -> str:
    for name in company_names:
        if name in text:
            return name
    return ''


def _last_company_from_history(history: list[dict[str, Any]], company_names: list[str]) -> str:
    for item in reversed(history):
        meta = item.get('meta')
        if isinstance(meta, dict):
            c = str(meta.get('company', '')).strip()
            if c and c in company_names:
                return c
        # only check user messages for company mentions (not AI content)
        if str(item.get('role', '')).strip() == 'user':
            content = str(item.get('content', ''))
            for name in company_names:
                if name in content:
                    return name
    return ''


# ── slot merging ─────────────────────────────────────────────────────────────

def merge_slot(
    current: Any,
    rule: Any,
    llm: Any,
    history_val: Any,
    allow_history: bool = True,
) -> SlotSource:
    """Merge slot values by priority: current > LLM > rule > history."""
    if _slot_truthy(current):
        return SlotSource(value=current, origin='current', confidence=1.0)
    if _slot_truthy(llm):
        return SlotSource(value=llm, origin='llm', confidence=0.8)
    if _slot_truthy(rule):
        return SlotSource(value=rule, origin='rule', confidence=0.9)
    if allow_history and _slot_truthy(history_val):
        return SlotSource(value=history_val, origin='history', confidence=0.6)
    return SlotSource()


def _slot_truthy(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str) and not val.strip():
        return False
    if isinstance(val, dict) and not val:
        return False
    return True


# ── dynamic attribution builder ──────────────────────────────────────────────

def build_dynamic_attribution(
    state: DialogueState,
    refs: list[dict[str, Any]] | None = None,
    sql: str = '',
    thinking_map: dict[str, Any] | None = None,
    data_summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build attribution dict from actual execution steps, not a fixed template."""
    if not state.execution_steps:
        return None

    steps = [
        {'label': s['label'], 'status': s['status'], 'detail': s['detail']}
        for s in state.execution_steps
    ]

    attribution: dict[str, Any] = {'steps': steps, 'sql': sql, 'kb_sources': refs or []}

    if data_summary is not None:
        attribution['data_summary'] = data_summary

    if thinking_map is not None:
        attribution['thinking_map'] = thinking_map

    return attribution


# ── slot TTL ─────────────────────────────────────────────────────────────────

_DEFAULT_SLOT_TTL = 3  # history-inherited slots expire after this many turns


def compute_turn_age(history: list[dict[str, Any]], slot_origin: str) -> int:
    """Estimate how many user turns ago the slot was set.

    For 'history' origin slots, count user messages from end of history.
    For 'current'/'llm'/'rule', age is always 0.
    """
    if slot_origin != 'history':
        return 0
    user_turns = sum(1 for h in history if str(h.get('role', '')).strip() == 'user')
    return user_turns


def apply_slot_ttl(
    slot: SlotSource,
    history: list[dict[str, Any]],
    ttl: int = _DEFAULT_SLOT_TTL,
) -> SlotSource:
    """Expire a history-inherited slot if it's older than TTL turns."""
    if slot.origin != 'history':
        return slot
    age = compute_turn_age(history, slot.origin)
    slot.turn_age = age
    if age > ttl:
        return SlotSource()  # expired
    # decay confidence with age
    slot.confidence = max(0.2, slot.confidence - age * 0.08)
    return slot


# ── intent cache ─────────────────────────────────────────────────────────────

import hashlib
from collections import OrderedDict

_INTENT_CACHE_MAX = 32
_intent_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _intent_cache_key(question: str, history_len: int) -> str:
    """Create a cache key from question + history length."""
    raw = f'{question}|{history_len}'
    return hashlib.md5(raw.encode()).hexdigest()


def get_cached_intent(question: str, history_len: int) -> dict[str, Any] | None:
    """Look up a cached intent result. Returns None on miss."""
    key = _intent_cache_key(question, history_len)
    if key in _intent_cache:
        _intent_cache.move_to_end(key)
        return _intent_cache[key]
    return None


def set_cached_intent(question: str, history_len: int, intent: dict[str, Any]) -> None:
    """Store an intent result in cache."""
    key = _intent_cache_key(question, history_len)
    _intent_cache[key] = intent
    _intent_cache.move_to_end(key)
    while len(_intent_cache) > _INTENT_CACHE_MAX:
        _intent_cache.popitem(last=False)


# ── history summarization ────────────────────────────────────────────────────

_SUMMARY_THRESHOLD = 10  # summarize when history exceeds this many messages


def should_summarize_history(history: list[dict[str, Any]]) -> bool:
    """Return True if history is long enough to benefit from summarization."""
    return len(history) > _SUMMARY_THRESHOLD


def build_summary_prompt(history: list[dict[str, Any]]) -> str:
    """Build a prompt for the LLM to summarize older history messages."""
    # keep the last 6 messages verbatim; summarize everything before
    keep_count = 6
    to_summarize = history[:-keep_count] if len(history) > keep_count else []
    if not to_summarize:
        return ''
    lines = []
    for msg in to_summarize:
        role = str(msg.get('role', '')).strip()
        content = str(msg.get('content', '')).strip()[:200]
        if content:
            lines.append(f'{role}: {content}')
    return (
        '请将以下对话历史压缩为一段简洁的摘要（不超过150字），保留关键的公司名、指标名、报告期和分析结论：\n\n'
        + '\n'.join(lines)
    )
