"""Structured session memory — maintains key facts across turns.

Unlike raw chat history which gets truncated and compressed, this memory
preserves the most important structured facts (current company, period,
discussed metrics, key conclusions) so that follow-up questions can be
answered accurately even when the original chat messages are long gone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_NUM_RE = re.compile(r'[\d,]+\.?\d*\s*(?:万元|亿元|亿|万|%|元)')


@dataclass
class SessionMemory:
    """Structured per-session memory that survives history compression."""

    current_company: str = ''
    current_period: str = ''
    discussed_metrics: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    topic_stack: list[str] = field(default_factory=list)
    turn_count: int = 0

    # ── update from a completed reply ────────────────────────────────────────

    def update_from_reply(self, reply: dict[str, Any]) -> None:
        """Extract and memorize key facts from a reply dict."""
        self.turn_count += 1
        meta = reply.get('meta') or {}

        # update current company / period
        company = str(meta.get('company', '')).strip()
        if company and company != 'None':
            if company != self.current_company:
                self.topic_stack.append(f'{self.current_company}' if self.current_company else '')
                self.current_company = company

        period = str(meta.get('report_period', '')).strip()
        if period:
            self.current_period = period

        # track discussed metrics (deduplicated, capped)
        metric = str(meta.get('metric', '')).strip()
        if metric and metric not in self.discussed_metrics:
            self.discussed_metrics.append(metric)
            if len(self.discussed_metrics) > 20:
                self.discussed_metrics = self.discussed_metrics[-20:]

        # extract key numeric facts from content
        content = str(reply.get('content', '')).strip()
        if content and company:
            facts = self._extract_facts(content, company)
            self.key_facts.extend(facts)
            if len(self.key_facts) > 15:
                self.key_facts = self.key_facts[-15:]

    def _extract_facts(self, content: str, company: str) -> list[str]:
        """Extract sentences containing key numeric data."""
        facts: list[str] = []
        sentences = re.split(r'[。！\n]', content)
        for sent in sentences:
            sent = sent.strip()
            if not sent or len(sent) < 10:
                continue
            if _NUM_RE.search(sent) and (company in sent or len(sent) < 80):
                clean = re.sub(r'\*\*', '', sent).strip()
                if clean and clean not in facts:
                    facts.append(clean[:120])
                if len(facts) >= 3:
                    break
        return facts

    # ── inject into prompt context ───────────────────────────────────────────

    def to_context_string(self) -> str:
        """Build a compact context string for injection into LLM prompts."""
        if not self.current_company and not self.key_facts:
            return ''
        parts: list[str] = []
        if self.current_company:
            parts.append(f'当前讨论公司：{self.current_company}')
        if self.current_period:
            parts.append(f'当前报告期：{self.current_period}')
        if self.discussed_metrics:
            recent = self.discussed_metrics[-5:]
            parts.append(f'已讨论指标：{"、".join(recent)}')
        if self.key_facts:
            recent_facts = self.key_facts[-5:]
            parts.append(f'关键结论：{"；".join(recent_facts)}')
        if self.topic_stack:
            prev = [t for t in self.topic_stack[-3:] if t]
            if prev:
                parts.append(f'之前讨论过：{"、".join(prev)}')
        return '\n'.join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            'current_company': self.current_company,
            'current_period': self.current_period,
            'discussed_metrics': list(self.discussed_metrics),
            'key_facts': list(self.key_facts),
            'topic_stack': list(self.topic_stack),
            'turn_count': self.turn_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SessionMemory':
        if not isinstance(data, dict):
            return cls()
        return cls(
            current_company=str(data.get('current_company', '')),
            current_period=str(data.get('current_period', '')),
            discussed_metrics=list(data.get('discussed_metrics', [])),
            key_facts=list(data.get('key_facts', [])),
            topic_stack=list(data.get('topic_stack', [])),
            turn_count=int(data.get('turn_count', 0)),
        )


# ── global session store (in-memory, keyed by conversation id) ───────────────

_sessions: dict[str, SessionMemory] = {}


def get_session_memory(session_id: str) -> SessionMemory:
    """Get or create a session memory for the given session."""
    if session_id not in _sessions:
        _sessions[session_id] = SessionMemory()
    return _sessions[session_id]


def clear_session_memory(session_id: str) -> None:
    _sessions.pop(session_id, None)
