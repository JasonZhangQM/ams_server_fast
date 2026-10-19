# -*- coding: utf-8 -*-
"""HTTP 工具函数 + symbol 来源解析。

被两个入口共用：后端后台线程（app/bds/router.py）与 CLI（run2_batch_sync.py）。
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


def _allowed_symbols(trading_status, exclude_synced_since=None, only_stock=True):
    '''返回「允许同步」的 symbol 集合；条件都不生效时返回 None（调用方跳过求交）。

    仅 index 这类「symbol 不来自 bds_symbol_info」的 scope 才需要：
    成分股接口既不返回 delisted_date 也不返回 sync_at，只能取全表集合做交集。
    退市条件、已同步日期、仅个股（剔指数）在一次分页里拉完（约 6 次请求），避免各拉一轮。
    '''
    if trading_status == 'all' and not exclude_synced_since and not only_stock:
        return None
    params = {'only_stock': 1 if only_stock else 0}
    if exclude_synced_since:
        # date 型参数不能传 None（会 422），只在有值时拼入
        params['exclude_synced_since'] = exclude_synced_since.isoformat()
    if trading_status != 'all':
        # 端点 pattern 仅收 trading|delisted；'all' = 不加条件 = 不传该参数
        params['trading_status'] = trading_status
    return {it['symbol'] for it in _fetch_paged('/bds/symbol-infos', **params)}


def resolve_symbols(scope, explicit=None, industry='', index_code='', max_symbols=0,
                    exclude_synced_since=None, trading_status='trading'):
    '''按 scope 解析 symbol 列表，可选按退市口径过滤 + 剔除「指定日期及之后已同步」的标的。

    已同步判定由后端 /bds/symbol-infos?exclude_synced_since=YYYY-MM-DD 完成，
    依据是 bds_symbol_info.sync_at >= 该日期（sync_at 为空或早于该日期者保留）。
    scope: explicit | all | industry | index
    exclude_synced_since: datetime.date 或 None。None=不跳过；
      今天=只跑今日未同步；昨天=连昨天跑完的一并剔除（跨日续跑）
    trading_status: trading=仅在市（默认，含已公告未退市）| delisted=仅已退市 | all=全部
      生效范围（见 docs/批量同步退市股过滤方案_2026-10-16.md §3.3）：
        - all / industry → 条件直接透传给列表端点分页查询
        - index          → 成分接口无该字段，取全表集合求交
        - explicit       → **不参与过滤**（手写清单 = 用户显式意图，系统不二次过滤）
    仅 A 股个股：除 explicit 外一律只取 market 非空的标的，剔除 5 只指数
      （SHSE.000001/000300/000852/000905、SZSE.399006）与美股/黄金（market 恒 NULL）。
    '''

    if trading_status not in ('trading', 'delisted', 'all'):
        raise ValueError(f'非法 trading_status: {trading_status}（应为 trading/delisted/all）')

    scope = scope.lower()
    # date 型参数：None 不能传给端点（会 422），用条件字典按需拼入
    _es = {'exclude_synced_since': exclude_synced_since.isoformat()} if exclude_synced_since else {}
    # 'all' = 不加条件 ⇒ 不传该参数（端点 pattern 仅收 trading|delisted，传 all 会 422）
    _ts = {} if trading_status == 'all' else {'trading_status': trading_status}
    _os = {'only_stock': 1}   # 仅 A 股个股：财务/行情批量同步不带指数
    raw_total = None

    if scope == 'explicit':
        # 手写清单 = 用户显式意图：不做退市过滤、不做当日去重、也不剔指数（保持既有行为不变）
        raw = list(explicit or [])
        note = '手写清单（不做退市过滤）'

    elif scope == 'all':
        raw = [it['symbol'] for it in
               _fetch_paged('/bds/symbol-infos', **_es, **_ts, **_os)]
        note = f'全市场（bds_symbol_info，仅 A 股个股）| 退市口径={trading_status}'
        if exclude_synced_since:
            # ⚠️ 必须带同一退市/个股条件，否则 raw_total 含退市股与指数、drop 恒为「退市股+指数数」而非实际跳过数
            raw_total = _api_get('/bds/symbol-infos', limit=1, offset=0, **_ts, **_os).get('total', 0)

    elif scope == 'industry':
        if not industry:
            raise ValueError('scope=industry 需填 industry 参数')
        raw = [it['symbol'] for it in
               _fetch_paged('/bds/symbol-infos', industry=industry, **_es, **_ts, **_os)]
        note = f'行业={industry}（仅 A 股个股）| 退市口径={trading_status}'
        if exclude_synced_since:
            raw_total = _api_get('/bds/symbol-infos', industry=industry,
                                 limit=1, offset=0, **_ts, **_os).get('total', 0)

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
            note = f'指数={index_code} @ {td} | 退市口径={trading_status}'
        # 成分股接口不返回 delisted_date / sync_at ⇒ 取全表集合求交（退市 + 已同步日期 + 仅个股一次拉完）
        allowed = _allowed_symbols(trading_status, exclude_synced_since, only_stock=True)
        if allowed is not None:
            raw_total = len(raw)
            raw = [s for s in raw if s in allowed]

    else:
        raise ValueError(f'未知 scope: {scope}')

    syms = _filter_market(raw)
    if max_symbols and max_symbols > 0:
        syms = syms[:max_symbols]

    # tail 统计：raw_total 的含义随 scope 不同（见各分支注释），文案须区分
    #   all / industry → raw_total 是「同退市口径但不含已同步过滤」的 total ⇒ drop 只来自已同步剔除
    #   index          → raw_total 是「求交前」的成分数 ⇒ drop 含退市 + 已同步两类，无法细分
    tail = ''
    _since_label = f'{exclude_synced_since} 起已同步' if exclude_synced_since else ''
    if raw_total is not None:
        dropped = max(raw_total - len(raw), 0)
        if scope == 'index' and trading_status != 'all':
            extra = f' + {_since_label}' if exclude_synced_since else ''
            tail = f' | 成分求交剔除 {dropped} 只（退市口径={trading_status}{extra}）'
        else:
            tail = f' | {_since_label}剔除 {dropped} 只'
    print(f'symbol 来源: {note} | 原始 {len(raw)} -> 过滤后 {len(syms)}{tail}')
    return syms
