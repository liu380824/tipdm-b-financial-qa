from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable

import httpx

_log = logging.getLogger(__name__)
_MAX_RETRIES = 3
_RETRY_BACKOFF = [2, 4, 8]
_THINK_RE = re.compile(r'<think>[\s\S]*?</think>', re.DOTALL)

from config import CONFIG


def _llm_settings() -> tuple[str, str, str]:
    config = CONFIG.get('llm', {})
    api_key = os.getenv('LLM_API_KEY') or config.get('api_key', '')
    base_url = (os.getenv('LLM_BASE_URL') or config.get('base_url', '')).rstrip('/')
    model = os.getenv('LLM_MODEL') or config.get('model', '')
    return api_key, base_url, model


def _clamp_float(value: Any, default: float, lower: float = 0.0, upper: float = 1.5) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, lower), upper)


def _clamp_int(value: Any, default: int, lower: int = 5, upper: int = 600) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, lower), upper)


def llm_temperature(setting_name: str, default: float) -> float:
    config = CONFIG.get('llm', {})
    return _clamp_float(config.get(setting_name, default), default)


def llm_request_timeout(default: int = 120) -> int:
    config = CONFIG.get('llm', {})
    return _clamp_int(config.get('request_timeout', default), default)


def llm_available() -> bool:
    api_key, base_url, model = _llm_settings()
    return bool(api_key and base_url and model)


def _fix_json_str(s: str) -> str:
    """Best-effort fix for common LLM JSON errors."""
    # Remove control characters that break JSON parsing
    s = re.sub(r'[\x00-\x1f]', lambda m: ' ' if m.group() in ('\n', '\r', '\t') else '', s)
    s = s.replace('\n', '\n')  # keep actual newlines
    # Remove trailing commas before } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)
    # Fix missing commas between "value"\n"key" patterns
    s = re.sub(r'"\s*\n\s*"', '",\n"', s)
    # Fix missing commas between number/bool/null and "key" patterns
    s = re.sub(r'(\d|true|false|null)\s*\n\s*"', r'\1,\n"', s)
    # Fix missing commas between }\n{ or ]\n[ patterns (arrays of objects)
    s = re.sub(r'}\s*\n\s*{', '},\n{', s)
    # Fix missing commas between ]\n"key" or }\n"key"
    s = re.sub(r'(\])\s*\n\s*"', r'\1,\n"', s)
    # Remove single-line comments (// ...)
    s = re.sub(r'//[^\n]*', '', s)
    # Fix unterminated strings: close open quotes before } or ]
    # Count quotes — if odd, append a closing quote before last } or ]
    _open = 0
    for ch in s:
        if ch == '"':
            _open += 1
    if _open % 2 != 0:
        # Find last } or ] and insert a closing quote before it
        for _i in range(len(s) - 1, -1, -1):
            if s[_i] in ('}', ']'):
                s = s[:_i] + '"' + s[_i:]
                break
    return s


def _parse_json(text: str) -> dict[str, Any] | list[Any]:
    """Parse JSON object or array from LLM output, with auto-repair."""
    # Step 1: try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Step 1.5: strip trailing non-JSON text (LLM often appends explanations)
    stripped = text.strip()
    for _end_char, _start_char in [(']', '['), ('}', '{')]:
        _last = stripped.rfind(_end_char)
        if _last >= 0:
            _first = stripped.find(_start_char)
            if _first >= 0 and _first < _last:
                candidate = stripped[_first:_last + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
    # Step 2: extract JSON block and try
    matched = re.search(r'[\[{].*[\]}]', text, re.S)
    if not matched:
        raise json.JSONDecodeError('No JSON found', text, 0)
    raw = matched.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Step 3: auto-repair common errors and retry
    fixed = _fix_json_str(raw)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    # Step 4: attempt to repair truncated JSON by closing open brackets
    _repair = fixed.rstrip()
    # Remove trailing incomplete key-value (e.g., truncated string)
    _repair = re.sub(r',\s*"[^"]*$', '', _repair)
    _repair = re.sub(r',\s*$', '', _repair)
    # Count and close open brackets
    _stack: list[str] = []
    _in_str = False
    _esc = False
    for _ch in _repair:
        if _esc:
            _esc = False
            continue
        if _ch == '\\':
            _esc = True
            continue
        if _ch == '"':
            _in_str = not _in_str
            continue
        if _in_str:
            continue
        if _ch in ('{', '['):
            _stack.append('}' if _ch == '{' else ']')
        elif _ch in ('}', ']') and _stack:
            _stack.pop()
    _repair += ''.join(reversed(_stack))
    return json.loads(_repair)


def _parse_json_object(text: str) -> dict[str, Any]:
    parsed = _parse_json(text)
    if isinstance(parsed, list):
        return {'items': parsed}
    if not isinstance(parsed, dict):
        raise ValueError('LLM 未返回 JSON 对象')
    return parsed


def _llm_call(system_prompt: str, user_prompt: str, *, temperature: float = 0.2, json_mode: bool = True, max_tokens: int | None = None) -> str:
    """Shared low-level LLM call with retry on 429/5xx. Returns raw content string."""
    api_key, base_url, model = _llm_settings()
    if not api_key or not base_url or not model:
        raise RuntimeError('LLM 配置不完整，无法调用模型服务')
    body: dict[str, Any] = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': temperature,
    }
    if max_tokens is not None:
        body['max_tokens'] = max_tokens
    if json_mode:
        body['response_format'] = {'type': 'json_object'}
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = httpx.post(
                f'{base_url}/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'},
                json=body,
                timeout=llm_request_timeout(),
            )
            response.raise_for_status()
            payload = response.json()
            content = payload['choices'][0]['message']['content']
            if isinstance(content, list):
                content = ''.join(
                    item.get('text', '') if isinstance(item, dict) else str(item)
                    for item in content
                )
            result = str(content)
            return _THINK_RE.sub('', result).strip() if '<think>' in result else result
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF[attempt]
                _log.warning('[LLM] %s on attempt %d, retrying in %ds...', exc.response.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF[attempt]
                _log.warning('[LLM] %s on attempt %d, retrying in %ds...', type(exc).__name__, attempt + 1, wait)
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def request_llm_json(system_prompt: str, user_prompt: str, *, temperature: float = 0.2, max_tokens: int | None = None) -> dict[str, Any]:
    """Call LLM and parse response as JSON object."""
    raw = _llm_call(system_prompt, user_prompt, temperature=temperature, json_mode=True, max_tokens=max_tokens)
    return _parse_json_object(raw)


def request_llm_array(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> list[Any]:
    """Call LLM and parse response as JSON array."""
    raw = _llm_call(system_prompt, user_prompt, temperature=temperature, json_mode=True)
    parsed = _parse_json(raw)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ('items', 'subtasks', 'tasks', 'plan', 'result', 'data'):
            if isinstance(parsed.get(key), list):
                return parsed[key]
    return []


def request_llm_text(system_prompt: str, user_prompt: str, *, temperature: float = 0.2) -> str:
    """Call LLM and return plain text (no JSON mode)."""
    return _llm_call(system_prompt, user_prompt, temperature=temperature, json_mode=False).strip()


# ── intent LLM (supports function calling / tool_calls) ─────────────────────

def _intent_llm_settings() -> tuple[str, str, str]:
    """Read intent_llm config; falls back to main llm config."""
    intent_cfg = CONFIG.get('intent_llm', {})
    main_cfg = CONFIG.get('llm', {})
    api_key = intent_cfg.get('api_key') or main_cfg.get('api_key', '')
    base_url = (intent_cfg.get('base_url') or main_cfg.get('base_url', '')).rstrip('/')
    model = intent_cfg.get('model') or main_cfg.get('model', '')
    return api_key, base_url, model


def intent_llm_available() -> bool:
    api_key, base_url, model = _intent_llm_settings()
    return bool(api_key and base_url and model)


def request_llm_with_tools(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
) -> dict[str, Any]:
    """Call LLM with function-calling tools. Returns the full message dict (may contain tool_calls)."""
    api_key, base_url, model = _intent_llm_settings()
    if not api_key or not base_url or not model:
        raise RuntimeError('intent_llm 配置不完整，无法调用 function calling')
    body: dict[str, Any] = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'tools': tools,
        'temperature': temperature,
    }
    timeout = _clamp_int(
        CONFIG.get('intent_llm', {}).get('request_timeout')
        or CONFIG.get('llm', {}).get('request_timeout', 90),
        90, 5, 300,
    )
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = httpx.post(
                f'{base_url}/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'},
                json=body,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            message = payload['choices'][0]['message']
            return message
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF[attempt]
                _log.warning('[Intent LLM] %s on attempt %d, retrying in %ds...', exc.response.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF[attempt]
                _log.warning('[Intent LLM] %s on attempt %d, retrying in %ds...', type(exc).__name__, attempt + 1, wait)
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def _llm_call_stream(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Streaming LLM call with retry on 429/5xx. Sends text chunks via *on_chunk*, returns full text."""
    api_key, base_url, model = _llm_settings()
    if not api_key or not base_url or not model:
        raise RuntimeError('LLM 配置不完整，无法调用模型服务')
    body: dict[str, Any] = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': temperature,
        'stream': True,
    }
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            parts: list[str] = []
            in_think = False
            with httpx.stream(
                'POST',
                f'{base_url}/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'},
                json=body,
                timeout=httpx.Timeout(timeout=llm_request_timeout(), connect=10.0),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[len('data:'):].strip()
                    if data == '[DONE]':
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk['choices'][0].get('delta', {})
                        text = delta.get('content', '')
                        if not text:
                            continue
                        parts.append(text)
                        if '<think>' in text:
                            in_think = True
                        if in_think:
                            if '</think>' in text:
                                in_think = False
                            continue
                        if on_chunk:
                            on_chunk(text)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
            full = ''.join(parts)
            return _THINK_RE.sub('', full).strip() if '<think>' in full else full
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF[attempt]
                _log.warning('[LLM] stream %s on attempt %d, retrying in %ds...', exc.response.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF[attempt]
                _log.warning('[LLM] stream %s on attempt %d, retrying in %ds...', type(exc).__name__, attempt + 1, wait)
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def request_llm_text_stream(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Stream LLM plain-text response. Calls *on_chunk* for every token, returns full text."""
    return _llm_call_stream(
        system_prompt, user_prompt, temperature=temperature, on_chunk=on_chunk,
    ).strip()
