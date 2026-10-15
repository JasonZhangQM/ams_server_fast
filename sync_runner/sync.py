# -*- coding: utf-8 -*-
"""单只同步 + 当日标记。

从 run2-批量同步.ipynb Cell 3 原样抽取，单行逻辑未改。
"""

import time

from .config import API_BASE, REQUEST_TIMEOUT, MAX_RETRY, RETRY_BACKOFF, SESSION


# gm 终端连接断开 / 终端不可用 的致命错误关键词
# 命中任何一个就立即终止整个批量同步，不走 MAX_RETRY 重试
_FATAL_ERROR_HINTS = (
    'gm 终端', 'gm终端', '终端服务',
    'TimeoutError', 'ConnectionError', 'ConnectError',
    '连接超时', '无法连接', 'connect timeout',
    '请检查 gm 终端',
)


def _is_fatal_gm_error(message: str) -> bool:
    """判断错误信息是否指向 gm 终端不可用（致命错误，需立即中止）。"""
    if not message:
        return False
    msg_lower = message.lower()
    return any(h.lower() in msg_lower for h in _FATAL_ERROR_HINTS)


def sync_one(symbol, table):
    '''调用单只同步接口，返回 {status, message}。

    status: success | no_data | error | fatal
    fatal 表示 gm 终端连接断开等致命错误——调用方应立即中止整体同步。
    '''
    url = f'{API_BASE}/bds/sync/{table}'
    last_err = ''
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = SESSION.post(url, params={'symbol': symbol}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                body = r.json()
                # 后端明确标记 fatal=true → gm 终端断连，立即返回不重试
                if body.get('fatal'):
                    return {'status': 'fatal',
                            'message': body.get('message', 'gm 终端不可用')}
                st = body.get('status')
                msg = body.get('message', '')
                if st in ('success', 'no_data'):
                    return {'status': st, 'message': msg}
                last_err = msg or 'sync error'  # status == error
            else:
                # HTTPException：body 为 {'detail': ...}
                try:
                    last_err = f'HTTP {r.status_code}: ' + str(r.json().get('detail', r.text[:200]))
                except Exception:
                    last_err = f'HTTP {r.status_code}: {r.text[:200]}'
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'

        # 致命错误 gm 断连 → 不重试，立即返回 fatal
        # 优先用后端 response 里的 fatal 字段，其次用关键词兜底（HTTP 非 200 时）
        if _is_fatal_gm_error(last_err):
            return {'status': 'fatal', 'message': last_err}

        if attempt < MAX_RETRY:
            time.sleep(RETRY_BACKOFF * attempt)
    return {'status': 'error', 'message': last_err}


def mark_synced(symbol):
    '''标记该 symbol 当日已完成全部目标表同步（写 bds_symbol_info.sync_at）。

    返回 {status, message}；status: ok | error
    '''
    url = f'{API_BASE}/bds/sync/mark-synced'
    try:
        r = SESSION.post(url, params={'symbol': symbol}, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            body = r.json()
            return {'status': body.get('status', 'error'),
                    'message': body.get('message', f"sync_at={body.get('sync_at')}")}
        return {'status': 'error', 'message': f'HTTP {r.status_code}: {r.text[:200]}'}
    except Exception as e:
        return {'status': 'error', 'message': f'{type(e).__name__}: {e}'}
