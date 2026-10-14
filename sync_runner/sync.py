# -*- coding: utf-8 -*-
"""单只同步 + 当日标记。

从 run2-批量同步.ipynb Cell 3 原样抽取，单行逻辑未改。
"""

import time

from .config import API_BASE, REQUEST_TIMEOUT, MAX_RETRY, RETRY_BACKOFF, SESSION


def sync_one(symbol, table):
    '''调用单只同步接口，返回 {status, message}。

    status: success | no_data | error
    '''
    url = f'{API_BASE}/bds/sync/{table}'
    last_err = ''
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = SESSION.post(url, params={'symbol': symbol}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                body = r.json()
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
