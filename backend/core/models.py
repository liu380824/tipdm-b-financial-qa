from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from config import CONFIG
from .context import MAX_SQL_ROWS


def _assistant_default(key: str, fallback: bool = True) -> bool:
    cfg = CONFIG.get('assistant', {}) if isinstance(CONFIG.get('assistant'), dict) else {}
    return bool(cfg.get(key, fallback))


class AssistantQueryRequest(BaseModel):
    question: str = Field(min_length=1)
    history: list[dict[str, Any]] = Field(default_factory=list)
    auto_chart: bool = Field(default_factory=lambda: _assistant_default('default_auto_chart'))
    enable_context: bool = Field(default_factory=lambda: _assistant_default('default_enable_context'))
    deep_think: bool = True
    session_id: str = ''


class SqlQueryRequest(BaseModel):
    sql: str = Field(min_length=1)
    limit: int = Field(default=50, ge=1, le=MAX_SQL_ROWS)
