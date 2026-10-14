# -*- coding: utf-8 -*-
"""HTTP 工具函数 + symbol 来源解析。

从 run2-批量同步.ipynb Cell 3 原样抽取，单行逻辑未改。
"""

from .config import API_BASE, REQUEST_TIMEOUT, MARKET_PREFIXES, SESSION


def _api_get(path, **params):
    '''GET 后端接口并返回 JSON。'''
    r = SESSION.get(f'{API_BASE}{path}', params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _fetch_paged(path, page_size=1000, **params):
    '''分页拉取列表接口，返回全部 items（自动翻页直到取完）。'''
    items, offset = [], 0
    while True:
        j = _api_get(path, limit=page_size, offset=offset, **params)
        batch = j.get('items', [])
        items.extend(batch)
        total = j.get('total', 0)
        offset += page_size
        if not batch or offset >= total:
            break
    return items


def _filter_market(symbols):
    '''仅保留白名单前缀的股票标的，去重并保持顺序。'''
    seen, out = set(), []
    for s in symbols:
        if s and s.startswith(MARKET_PREFIXES) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def resolve_symbols(scope, explicit=None, industry='', index_code='', max_symbols=0,
                    exclude_synced_today=False):
    '''按 scope 解析 symbol 列表，可选剔除「当日已同步」的标的。

    「当日」由后端 /bds/symbol-infos?exclude_synced_today=1 判定，
    依据是 bds_symbol_info.sync_at == 服务器本地当日。
    scope: explicit | all | industry | index
    '''
    scope = scope.lower()
    _only_today = 1 if exclude_synced_today else 0
    raw_total = None

    if scope == 'explicit':
        raw = list(explicit or [])
        note = '手写清单'

    elif scope == 'all':
        raw = [it['symbol'] for it in
               _fetch_paged('/bds/symbol-infos', exclude_synced_today=_only_today)]
        note = '全市场（bds_symbol_info）'
        if _only_today:
            raw_total = _api_get('/bds/symbol-infos', limit=1, offset=0).get('total', 0)

    elif scope == 'industry':
        if not industry:
            raise ValueError('scope=industry 需填 industry 参数')
        raw = [it['symbol'] for it in
               _fetch_paged('/bds/symbol-infos', industry=industry,
                            exclude_synced_today=_only_today)]
        note = f'行业={industry}'
        if _only_today:
            raw_total = _api_get('/bds/symbol-infos', industry=industry,
                                 limit=1, offset=0).get('total', 0)

    elif scope == 'index':
        if not index_code:
            raise ValueError('scope=index 需填 index_code 参数')
        head = _api_get('/bds/index-constituents', index_code=index_code, limit=1, offset=0)
        items = head.get('items', [])
        if not items:
            raw, note = [], f'指数={index_code}（无成分数据）'
        else:
            td = items[0]['trade_date']
            rows = _fetch_paged('/bds/index-constituents', index_code=index_code, trade_date=td)
            raw = [it['symbol'] for it in rows]
            note = f'指数={index_code} @ {td}'
        if _only_today:
            # 成分股接口不返回 sync_at，改取「当日未同步集合」求交
            allowed = {it['symbol'] for it in
                       _fetch_paged('/bds/symbol-infos', exclude_synced_today=1)}
            raw_total = len(raw)
            raw = [s for s in raw if s in allowed]

    else:
        raise ValueError(f'未知 scope: {scope}')

    syms = _filter_market(raw)
    if max_symbols and max_symbols > 0:
        syms = syms[:max_symbols]

    tail = ''
    if exclude_synced_today:
        drop = (raw_total - len(raw)) if raw_total is not None else 0
        tail = f' | 当日已同步剔除 {max(drop, 0)} 只'
    print(f'symbol 来源: {note} | 原始 {len(raw)} -> 过滤后 {len(syms)}{tail}')
    return syms
