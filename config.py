from __future__ import annotations

import json
import os
from typing import Any

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

with open(_CONFIG_PATH, 'r', encoding='utf-8') as _f:
    CONFIG = json.load(_f)


def _clamp_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, lower), upper)


def _clamp_float(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, lower), upper)


def _mask_secret(value: str) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    if len(text) <= 8:
        return '*' * len(text)
    return f'{text[:4]}{"*" * max(len(text) - 8, 4)}{text[-4:]}'


def save_config() -> None:
    """Write CONFIG to disk."""
    with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        f.write('\n')


def get_system_settings() -> dict[str, Any]:
    # Re-read disk so hand-edits to config.json are visible in GET
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as _rf:
            _disk = json.load(_rf)
        for _k, _v in _disk.items():
            if isinstance(_v, dict) and isinstance(CONFIG.get(_k), dict):
                CONFIG[_k].update(_v)
            else:
                CONFIG[_k] = _v
    except Exception:
        pass
    database = CONFIG.setdefault('database', {})
    llm = CONFIG.setdefault('llm', {})
    extract_llm = CONFIG.setdefault('extract_llm', {})
    intent_llm = CONFIG.setdefault('intent_llm', {})
    embedding = CONFIG.setdefault('embedding', {})
    parallel = CONFIG.setdefault('parallel', {})
    kb = CONFIG.setdefault('knowledge_base', {})
    assistant = CONFIG.setdefault('assistant', {})
    return {
        'database': {
            'path': str(database.get('path', 'financial_reports.db')).strip() or 'financial_reports.db'
        },
        'reports_dir': str(CONFIG.get('reports_dir', '')).strip(),
        'research_reports_dir': str(CONFIG.get('research_reports_dir', '')).strip(),
        'parallel': {
            'max_workers': _clamp_int(parallel.get('max_workers', 4), 4, 1, 32)
        },
        'knowledge_base': {
            'index_concurrency': _clamp_int(kb.get('index_concurrency', 4), 4, 1, 16),
            'embed_batch_size': _clamp_int(kb.get('embed_batch_size', 64), 64, 1, 256),
            'embed_max_chars': _clamp_int(kb.get('embed_max_chars', 2000), 2000, 200, 8000),
            'child_chunk_min': _clamp_int(kb.get('child_chunk_min', 100), 100, 20, 500),
            'child_chunk_max': _clamp_int(kb.get('child_chunk_max', 300), 300, 100, 2000),
            'parent_chunk_max': _clamp_int(kb.get('parent_chunk_max', 900), 900, 300, 5000),
            'chunk_overlap': _clamp_int(kb.get('chunk_overlap', 50), 50, 0, 200),
            'ocr_dpi': _clamp_int(kb.get('ocr_dpi', 200), 200, 72, 600),
            'ocr_workers': _clamp_int(kb.get('ocr_workers', 4), 4, 1, 16),
            'bm25_weight': _clamp_float(kb.get('bm25_weight', 0.3), 0.3, 0.0, 1.0),
            'vector_weight': _clamp_float(kb.get('vector_weight', 0.7), 0.7, 0.0, 1.0),
            'rrf_k': _clamp_int(kb.get('rrf_k', 60), 60, 1, 200),
            'rerank_candidates': _clamp_int(kb.get('rerank_candidates', 30), 30, 5, 100),
            'top_k': _clamp_int(kb.get('top_k', 5), 5, 1, 50),
        },
        'assistant': {
            'enable_multi_intent': bool(assistant.get('enable_multi_intent', True)),
            'kb_top_k': _clamp_int(assistant.get('kb_top_k', 5), 5, 1, 20),
            'default_auto_chart': bool(assistant.get('default_auto_chart', True)),
            'default_enable_context': bool(assistant.get('default_enable_context', True))
        },
        'llm': {
            'base_url': str(llm.get('base_url', '')).strip(),
            'model': str(llm.get('model', '')).strip(),
            'has_api_key': bool(str(llm.get('api_key', '')).strip() or os.getenv('LLM_API_KEY')),
            'api_key_masked': _mask_secret(str(llm.get('api_key', '')).strip()),
            'request_timeout': _clamp_int(llm.get('request_timeout', 120), 120, 5, 600),
            'intent_temperature': _clamp_float(llm.get('intent_temperature', 0.1), 0.1, 0.0, 1.5),
            'query_plan_temperature': _clamp_float(llm.get('query_plan_temperature', 0.1), 0.1, 0.0, 1.5),
            'analysis_temperature': _clamp_float(llm.get('analysis_temperature', 0.3), 0.3, 0.0, 1.5),
            'general_reply_temperature': _clamp_float(llm.get('general_reply_temperature', 0.4), 0.4, 0.0, 1.5),
            'planner_decompose_temperature': _clamp_float(llm.get('planner_decompose_temperature', 0.1), 0.1, 0.0, 1.5),
            'synthesis_temperature': _clamp_float(llm.get('synthesis_temperature', 0.4), 0.4, 0.0, 1.5),
            'ref_extract_temperature': _clamp_float(llm.get('ref_extract_temperature', 0.1), 0.1, 0.0, 1.5)
        },
        'extract_llm': {
            'base_url': str(extract_llm.get('base_url', '')).strip(),
            'model': str(extract_llm.get('model', '')).strip(),
            'has_api_key': bool(str(extract_llm.get('api_key', '')).strip() or os.getenv('EXTRACT_LLM_API_KEY')),
            'api_key_masked': _mask_secret(str(extract_llm.get('api_key', '')).strip()),
            'request_timeout': _clamp_int(extract_llm.get('request_timeout', 120), 120, 5, 600),
        },
        'intent_llm': {
            'base_url': str(intent_llm.get('base_url', '')).strip(),
            'model': str(intent_llm.get('model', '')).strip(),
            'has_api_key': bool(str(intent_llm.get('api_key', '')).strip() or os.getenv('INTENT_LLM_API_KEY')),
            'api_key_masked': _mask_secret(str(intent_llm.get('api_key', '')).strip()),
            'request_timeout': _clamp_int(intent_llm.get('request_timeout', 30), 30, 5, 120),
        },
        'embedding': {
            'base_url': str(embedding.get('base_url', '')).strip(),
            'model': str(embedding.get('model', 'BAAI/bge-m3')).strip(),
            'reranker_model': str(embedding.get('reranker_model', 'BAAI/bge-reranker-v2-m3')).strip(),
            'has_api_key': bool(str(embedding.get('api_key', '')).strip() or os.getenv('EMBEDDING_API_KEY')),
            'api_key_masked': _mask_secret(str(embedding.get('api_key', '')).strip()),
        }
    }


def _reload_config_from_disk() -> None:
    """Re-read config.json from disk and merge into CONFIG (preserves hand-edits)."""
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            disk = json.load(f)
        # Deep-merge: for dict values merge keys, for others overwrite
        for key, val in disk.items():
            if isinstance(val, dict) and isinstance(CONFIG.get(key), dict):
                CONFIG[key].update(val)
            else:
                CONFIG[key] = val
    except Exception:
        pass


def update_system_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError('设置参数格式不正确')

    current = get_system_settings()
    database = CONFIG.setdefault('database', {})
    llm = CONFIG.setdefault('llm', {})
    extract_llm = CONFIG.setdefault('extract_llm', {})
    intent_llm = CONFIG.setdefault('intent_llm', {})
    embedding = CONFIG.setdefault('embedding', {})
    parallel = CONFIG.setdefault('parallel', {})
    assistant = CONFIG.setdefault('assistant', {})

    database_payload = payload.get('database') if isinstance(payload.get('database'), dict) else {}
    llm_payload = payload.get('llm') if isinstance(payload.get('llm'), dict) else {}
    extract_llm_payload = payload.get('extract_llm') if isinstance(payload.get('extract_llm'), dict) else {}
    intent_llm_payload = payload.get('intent_llm') if isinstance(payload.get('intent_llm'), dict) else {}
    embedding_payload = payload.get('embedding') if isinstance(payload.get('embedding'), dict) else {}
    parallel_payload = payload.get('parallel') if isinstance(payload.get('parallel'), dict) else {}
    kb_payload = payload.get('knowledge_base') if isinstance(payload.get('knowledge_base'), dict) else {}
    assistant_payload = payload.get('assistant') if isinstance(payload.get('assistant'), dict) else {}

    if database_payload.get('path'):
        database['path'] = str(database_payload['path']).strip() or current['database']['path']
    if 'reports_dir' in payload:
        CONFIG['reports_dir'] = str(payload['reports_dir']).strip() or current['reports_dir']
    if 'research_reports_dir' in payload:
        CONFIG['research_reports_dir'] = str(payload['research_reports_dir']).strip() or current['research_reports_dir']
    if parallel_payload.get('max_workers') is not None:
        parallel['max_workers'] = _clamp_int(parallel_payload['max_workers'], current['parallel']['max_workers'], 1, 32)

    kb = CONFIG.setdefault('knowledge_base', {})
    cur_kb = current['knowledge_base']
    kb['index_concurrency'] = _clamp_int(kb_payload.get('index_concurrency', cur_kb['index_concurrency']), cur_kb['index_concurrency'], 1, 16)
    kb['embed_batch_size']  = _clamp_int(kb_payload.get('embed_batch_size', cur_kb['embed_batch_size']), cur_kb['embed_batch_size'], 1, 256)
    kb['embed_max_chars']   = _clamp_int(kb_payload.get('embed_max_chars', cur_kb['embed_max_chars']), cur_kb['embed_max_chars'], 200, 8000)
    kb['child_chunk_min']   = _clamp_int(kb_payload.get('child_chunk_min', cur_kb['child_chunk_min']), cur_kb['child_chunk_min'], 20, 500)
    kb['child_chunk_max']   = _clamp_int(kb_payload.get('child_chunk_max', cur_kb['child_chunk_max']), cur_kb['child_chunk_max'], 100, 2000)
    kb['parent_chunk_max']  = _clamp_int(kb_payload.get('parent_chunk_max', cur_kb['parent_chunk_max']), cur_kb['parent_chunk_max'], 300, 5000)
    kb['chunk_overlap']     = _clamp_int(kb_payload.get('chunk_overlap', cur_kb['chunk_overlap']), cur_kb['chunk_overlap'], 0, 200)
    kb['ocr_dpi']           = _clamp_int(kb_payload.get('ocr_dpi', cur_kb['ocr_dpi']), cur_kb['ocr_dpi'], 72, 600)
    kb['ocr_workers']        = _clamp_int(kb_payload.get('ocr_workers', cur_kb['ocr_workers']), cur_kb['ocr_workers'], 1, 16)
    kb['bm25_weight']       = _clamp_float(kb_payload.get('bm25_weight', cur_kb['bm25_weight']), cur_kb['bm25_weight'], 0.0, 1.0)
    kb['vector_weight']     = _clamp_float(kb_payload.get('vector_weight', cur_kb['vector_weight']), cur_kb['vector_weight'], 0.0, 1.0)
    kb['rrf_k']             = _clamp_int(kb_payload.get('rrf_k', cur_kb['rrf_k']), cur_kb['rrf_k'], 1, 200)
    kb['rerank_candidates'] = _clamp_int(kb_payload.get('rerank_candidates', cur_kb['rerank_candidates']), cur_kb['rerank_candidates'], 5, 100)
    kb['top_k']             = _clamp_int(kb_payload.get('top_k', cur_kb['top_k']), cur_kb['top_k'], 1, 50)

    assistant['enable_multi_intent'] = bool(assistant_payload.get('enable_multi_intent', current['assistant']['enable_multi_intent']))
    assistant['kb_top_k'] = _clamp_int(assistant_payload.get('kb_top_k', current['assistant']['kb_top_k']), current['assistant']['kb_top_k'], 1, 20)
    assistant['default_auto_chart'] = bool(assistant_payload.get('default_auto_chart', current['assistant']['default_auto_chart']))
    assistant['default_enable_context'] = bool(assistant_payload.get('default_enable_context', current['assistant']['default_enable_context']))

    if llm_payload:
        base_url = str(llm_payload.get('base_url', current['llm']['base_url'])).strip()
        model = str(llm_payload.get('model', current['llm']['model'])).strip()
        api_key = str(llm_payload.get('api_key', '')).strip()
        clear_api_key = bool(llm_payload.get('clear_api_key', False))
        llm['base_url'] = base_url.rstrip('/') if base_url else current['llm']['base_url']
        llm['model'] = model or current['llm']['model']
        if clear_api_key:
            llm['api_key'] = ''
        elif api_key:
            llm['api_key'] = api_key
        llm['request_timeout'] = _clamp_int(llm_payload.get('request_timeout', current['llm']['request_timeout']), current['llm']['request_timeout'], 5, 600)
        llm['intent_temperature'] = _clamp_float(llm_payload.get('intent_temperature', current['llm']['intent_temperature']), current['llm']['intent_temperature'], 0.0, 1.5)
        llm['query_plan_temperature'] = _clamp_float(llm_payload.get('query_plan_temperature', current['llm']['query_plan_temperature']), current['llm']['query_plan_temperature'], 0.0, 1.5)
        llm['analysis_temperature'] = _clamp_float(llm_payload.get('analysis_temperature', current['llm']['analysis_temperature']), current['llm']['analysis_temperature'], 0.0, 1.5)
        llm['general_reply_temperature'] = _clamp_float(llm_payload.get('general_reply_temperature', current['llm']['general_reply_temperature']), current['llm']['general_reply_temperature'], 0.0, 1.5)
        llm['planner_decompose_temperature'] = _clamp_float(llm_payload.get('planner_decompose_temperature', current['llm']['planner_decompose_temperature']), current['llm']['planner_decompose_temperature'], 0.0, 1.5)
        llm['synthesis_temperature'] = _clamp_float(llm_payload.get('synthesis_temperature', current['llm']['synthesis_temperature']), current['llm']['synthesis_temperature'], 0.0, 1.5)
        llm['ref_extract_temperature'] = _clamp_float(llm_payload.get('ref_extract_temperature', current['llm']['ref_extract_temperature']), current['llm']['ref_extract_temperature'], 0.0, 1.5)

    if extract_llm_payload:
        ext_base_url = str(extract_llm_payload.get('base_url', current['extract_llm']['base_url'])).strip()
        ext_model = str(extract_llm_payload.get('model', current['extract_llm']['model'])).strip()
        ext_api_key = str(extract_llm_payload.get('api_key', '')).strip()
        ext_clear_api_key = bool(extract_llm_payload.get('clear_api_key', False))
        extract_llm['base_url'] = ext_base_url.rstrip('/') if ext_base_url else current['extract_llm']['base_url']
        extract_llm['model'] = ext_model or current['extract_llm']['model']
        if ext_clear_api_key:
            extract_llm['api_key'] = ''
        elif ext_api_key:
            extract_llm['api_key'] = ext_api_key
        extract_llm['request_timeout'] = _clamp_int(extract_llm_payload.get('request_timeout', current['extract_llm']['request_timeout']), current['extract_llm']['request_timeout'], 5, 600)

    if intent_llm_payload:
        int_base_url = str(intent_llm_payload.get('base_url', current['intent_llm']['base_url'])).strip()
        int_model = str(intent_llm_payload.get('model', current['intent_llm']['model'])).strip()
        int_api_key = str(intent_llm_payload.get('api_key', '')).strip()
        int_clear_api_key = bool(intent_llm_payload.get('clear_api_key', False))
        intent_llm['base_url'] = int_base_url.rstrip('/') if int_base_url else current['intent_llm']['base_url']
        intent_llm['model'] = int_model or current['intent_llm']['model']
        if int_clear_api_key:
            intent_llm['api_key'] = ''
        elif int_api_key:
            intent_llm['api_key'] = int_api_key
        intent_llm['request_timeout'] = _clamp_int(intent_llm_payload.get('request_timeout', current['intent_llm']['request_timeout']), current['intent_llm']['request_timeout'], 5, 120)

    if embedding_payload:
        emb_base_url = str(embedding_payload.get('base_url', current['embedding']['base_url'])).strip()
        emb_model = str(embedding_payload.get('model', current['embedding']['model'])).strip()
        emb_reranker = str(embedding_payload.get('reranker_model', current['embedding']['reranker_model'])).strip()
        emb_api_key = str(embedding_payload.get('api_key', '')).strip()
        emb_clear_api_key = bool(embedding_payload.get('clear_api_key', False))
        embedding['base_url'] = emb_base_url.rstrip('/') if emb_base_url else current['embedding']['base_url']
        embedding['model'] = emb_model or current['embedding']['model']
        embedding['reranker_model'] = emb_reranker or current['embedding']['reranker_model']
        if emb_clear_api_key:
            embedding['api_key'] = ''
        elif emb_api_key:
            embedding['api_key'] = emb_api_key

    save_config()
    return get_system_settings()
