"""实时市值业务函数（从 bills/service.py 拆分）。"""
import logging
import pandas as pd
import akshare as ak
from gm.api import *
from server_fast.config import settings
from server_fast.common.utils import filter_dtypes, map_value, df_init_model, upsert_df_to_db, get_sql_to_df, call_with_timeout
from server_fast.app.bds.config import Config as DbsCfg
from server_fast.app.bills.models import Group

logger = logging.getLogger("uvicorn.error")


# ---------------- 期权本地行情快照（方案 §2.1/§2.2，2026-10-27） ----------------
def fetch_option_quotes():
    """期权持仓的本地行情快照，供 value_float 计算期权市值/浮动盈亏。

    join 链（方案 docs/Group市值与浮动盈亏重算方案_2026-10-27.md §2.1）：
        bills_group.symbol(=券商码) → bds_option_config.broker_symbol
        → contract_id → bds_option_market（每合约取 MAX(trade_date)，
        当日未同步自动退最近交易日）→ bds_option_info.multiplier。

    - 乘数取 info 逐合约值（A 类调整合约 10180 自动正确），
      **不走 MAP_MULTIPLIER**（其期权段永久注释，见 bds/config.py）。
    - 估值取价降级 price→settle→pre_settle（0 与 NULL 同视为无价）。
      ⚠️ 与风险口径的 COALESCE(settle, pre_settle, price) 用途不同：
      那条降级链只允许存在于 option_market.py 一处，这里是「报表市值」口径，
      优先要最新成交价，其次交易所盯市价。
    - value_time = quote_time（缺则快照日），修「成交日当行情时间」。
    """
    sql = '''
        SELECT c.broker_symbol,
               COALESCE(NULLIF(m.price, 0), NULLIF(m.settle, 0), NULLIF(m.pre_settle, 0)) AS px,
               i.multiplier,
               COALESCE(m.quote_time, CAST(m.trade_date AS DATETIME)) AS value_time
        FROM bds_option_config c
        JOIN bds_option_info i ON i.contract_id = c.contract_id
        JOIN bds_option_market m ON m.contract_id = c.contract_id
        JOIN (
            SELECT contract_id, MAX(trade_date) AS max_date
            FROM bds_option_market
            GROUP BY contract_id
        ) t ON t.contract_id = m.contract_id AND t.max_date = m.trade_date
        WHERE c.broker_symbol IS NOT NULL AND c.broker_symbol <> '';
    '''
    df = get_sql_to_df(sql, settings.DB_ENGINE)
    if df.empty:
        return df
    # DECIMAL 列 pymysql 返回 Decimal，统一转数值（NaN 化 NULL），避免后续赋值 dtype 冲突
    df['px'] = pd.to_numeric(df['px'], errors='coerce')
    df['multiplier'] = pd.to_numeric(df['multiplier'], errors='coerce')
    # broker_symbol 无唯一约束（已知缺陷）：同码多行会静默覆盖，这里显式告警 + 取最后一行
    dup = df[df.duplicated('broker_symbol', keep=False)]
    if not dup.empty:
        logger.warning(f'*****期权配置 broker_symbol 重复，取最后一行:{sorted(set(dup["broker_symbol"]))}')
        df = df.drop_duplicates('broker_symbol', keep='last')
    return df


# 实时市值
# 根据实时数据，计算持有市值与浮动盈亏
def value_float(df, current_data, multiplier, df_opt_quotes=None):
    """计算持有市值与浮动盈亏。

    2026-10-27 起（docs/Group市值与浮动盈亏重算方案_2026-10-27.md）：
    - 期权行（category='期权交易'）的价格/乘数/行情时点来自本地 bds_option_market
      （df_opt_quotes，见 fetch_option_quotes），不再依赖 gm（期权恒 0 条）。
    - 负成本持仓（义务仓，卖出开仓收到权利金 → cost_t<0）的市值取**负数**（负债口径）：
      pf = value − cost = 已收权利金 − 当前义务市值；正成本 pf = cost − value 不变。
      ⚠️ 与启用期权乘数原子上线，验收基准 IO2611-P-4000（真实 −307 量级）。
      🔒 cost_t_* 一律不改（历史落库，不可逆）。
    """
    symbol_dict = {item['symbol']: item['price'] for item in current_data}
    df['price'] = df['symbol'].map(symbol_dict)
    df['multiplier'] = df['symbol'].apply(map_value, multiplier=multiplier)

    is_opt = df['category'] == '期权交易'
    has_q = df_opt_quotes is not None and not df_opt_quotes.empty
    if has_q:
        q = df_opt_quotes.set_index('broker_symbol')

    # 期权行：本地快照覆盖价格/乘数（未认领或快照无价的行 price 保持 NaN → 走成本降级）
    if has_q and is_opt.any():
        opt_syms = df.loc[is_opt, 'symbol']
        df.loc[is_opt, 'price'] = opt_syms.map(q['px'])
        mult_map = opt_syms.map(q['multiplier'])
        df.loc[is_opt, 'multiplier'] = mult_map.fillna(df.loc[is_opt, 'multiplier'])
        # 防御性告警（方案 §2.4，禁静默）
        no_quote = sorted(set(opt_syms) - set(q.index))
        if no_quote:
            logger.warning(f'*****期权行认领不到行情快照，保持市值=成本:{no_quote}')
        no_px = sorted(set(opt_syms) & {s for s in q.index if pd.isna(q.loc[s, 'px'])})
        if no_px:
            logger.warning(f'*****期权快照价格三列全空/全0，保持市值=成本:{no_px}')

    cdt = ((df['p_long'] > 0) & df['price'].notna())  # 多头持仓市值=多头持仓数量*最新价格*乘数
    df.loc[cdt, 'value_long'] = (
        df.loc[cdt, 'price'] * df.loc[cdt, 'p_long'] * df.loc[cdt, 'multiplier']
    ).round(2)
    cdt = ((df['p_short'] > 0) & df['price'].notna())  # 空头持仓市值=空头持仓数量*最新价格*乘数
    # ⚠️ 原式 `df['p_short'] > 0 & df['price'].notna()` 缺括号：& 优先级高于 >，
    # notna 保护失效，靠下方 isna 分支兜底才没出事。2026-10-27 修（方案 P1）。
    df.loc[cdt, 'value_short'] = (
        df.loc[cdt, 'price'] * df.loc[cdt, 'p_short'] * df.loc[cdt, 'multiplier']
    ).round(2)
    cdt = df['price'].isna()  # 没有价格信息，则市值=成本
    df.loc[cdt, 'value_long'] = df.loc[cdt, 'cost_t_long']
    df.loc[cdt, 'value_short'] = df.loc[cdt, 'cost_t_short']
    # 如果没有持仓，则市值=成本
    cdt = ((df['p_long'] <= 0) & (df['p_short'] <= 0))
    df.loc[cdt, 'value_long'] = df.loc[cdt, 'cost_t_long']
    df.loc[cdt, 'value_short'] = df.loc[cdt, 'cost_t_short']

    # ===== 期权义务仓符号修正（负债口径，方案 §2.3，2026-10-27）=====
    # 只翻「有价格且有持仓」的行：无价/无持仓行 value 已等于负成本，口径天然一致，不能再翻。
    flip_l = is_opt & (df['p_long'] > 0) & df['price'].notna() & (df['cost_t_long'] < 0)
    flip_s = is_opt & (df['p_short'] > 0) & df['price'].notna() & (df['cost_t_short'] < 0)
    df.loc[flip_l, 'value_long'] = -df.loc[flip_l, 'value_long']
    df.loc[flip_s, 'value_short'] = -df.loc[flip_s, 'value_short']

    # 计算浮动盈亏（公式方向与历史一致：多头 pf=value−cost、空头 pf=cost−value）
    df = df.fillna(0)
    df['value_total'] = (
        df['value_long'] + df['value_short']).round(2)
    df['pf_long'] = (
        df['value_long'] - df['cost_t_long']).round(2)
    df['pf_short'] = (
        df['cost_t_short'] - df['value_short']).round(2)
    # 负成本空头（认购义务仓）翻转：pf = value − cost = 已收权利金 − 当前义务市值。
    # 负成本多头（认沽义务仓）无需翻：value 取负后 value−cost = 已收 − 现值，原公式已自洽。
    # 例：IO2611-P-4000 收 8692.75、现值 22740 → value=−22740, pf=−22740−(−8692.75)=−14047.25。
    flip_s = is_opt & (df['cost_t_short'] < 0)
    df.loc[flip_s, 'pf_short'] = (
        df.loc[flip_s, 'value_short'] - df.loc[flip_s, 'cost_t_short']).round(2)
    df['pf_total'] = (
        df['pf_long'] + df['pf_short']).round(2)
    # 市值更新时间
    df['value_time'] = df['end_time']
    # 期权行改写为行情时点（quote_time，缺则快照日）——修「成交日当行情时间」（方案 §2.1/P1）
    if has_q and is_opt.any():
        vt_map = df.loc[is_opt, 'symbol'].map(q['value_time'])
        m = vt_map.notna()
        df.loc[vt_map.index[m], 'value_time'] = vt_map[m]
    return df

# 获取期货实时行情（通过 AkShare）
# 解析数据库 symbol（如 SHFE.au2608），逐个调用 AkShare 接口获取实时价格
# 返回格式：[{symbol: 数据库原始symbol, price: current_price}]
def fetch_futures_realtime_prices(df_futures):
    # 传入 DataFrame 为空时直接返回空列表
    if df_futures.empty:
        return []
    prices = []
    for db_symbol in df_futures['symbol']:
        # 解析数据库 symbol：取 . 前为交易所代码，取 . 后并大写为合约代码
        if '.' not in db_symbol:
            logger.warning(f'*****期货symbol格式异常，缺少交易所分隔符:{db_symbol}，已跳过')
            continue
        exchange_code, contract_lower = db_symbol.split('.', 1)
        contract_code = contract_lower.upper()
        # 通过映射字典确定 AkShare market 参数（CFFEX->FF，其他->CF）
        market = DbsCfg.MAP_AKSHARE_MARKET.get(exchange_code)
        if market is None:
            logger.warning(f'*****未找到交易所[{exchange_code}]对应的AkShare market映射，symbol:{db_symbol}，已跳过')
            continue
        try:
            df_spot = ak.futures_zh_spot(symbol=contract_code, market=market)
            # 从返回的 DataFrame 中取 current_price 字段作为价格
            price = df_spot['current_price'].iloc[0]
            prices.append({'symbol': db_symbol, 'price': price})
        except Exception as e:
            # 单个品种获取失败时记录警告日志并跳过，不中断整体流程
            logger.warning(f'*****AkShare获取期货行情失败，symbol:{db_symbol}，已跳过:{e}')
    return prices

# 更新市值和浮动盈亏
def value_float_em_sql():
    _engine = settings.DB_ENGINE
    _multiplier = DbsCfg.MAP_MULTIPLIER
    _mdl = Group
    sql = f'''
        SELECT {','.join(_mdl.fields_f)}
        FROM {_mdl.__table__.name}
        WHERE end_time<>value_time
            OR cost_total>0
            OR p_total>0
            OR value_time IS NULL;
        '''
    df = get_sql_to_df(sql, _engine)
    df = df.fillna(0)
    df = df.astype(filter_dtypes(list(df.columns), _mdl.to_dtype()))
    # 按 category 分组获取行情：期货走 AkShare、期权走本地 bds_option_market、其余走 gm API
    df_futures = df[df['category'] == '期货交易']
    # 期权不再喂给 gm（恒 0 条），价格来自 fetch_option_quotes（方案 §2.1）
    df_others = df[(df['category'] != '期货交易') & (df['category'] != '期权交易')]
    current_data = []
    # 期货组：通过 AkShare 获取实时行情
    if not df_futures.empty:
        current_data.extend(fetch_futures_realtime_prices(df_futures))
    # 其他组：保持现有 gm API 方式（带超时保护，防止 gm 终端未启动时无限阻塞）
    # 降级策略：实时行情获取失败时，使用空行情继续计算，
    # value_float 会将无价格的标的市值降级为成本，已平仓标的市值将为0
    if not df_others.empty:
        try:
            current_data.extend(call_with_timeout(current, timeout=10)(
                list(df_others['symbol']), fields=['symbol', 'price']))
        except Exception as e:
            logger.warning(f'*****获取实时数据失败，启用降级策略:{e}')
    # 期权组：本地行情快照（每合约 MAX(trade_date)，见 fetch_option_quotes）
    df_opt_quotes = fetch_option_quotes() if (df['category'] == '期权交易').any() else None
    # 分组仅用于行情获取，value_float 接收的 df 仍为完整的查询结果
    df = value_float(df, current_data, _multiplier, df_opt_quotes)  # 计算市值与浮动盈亏
    # 更新bills_group表
    result = 0
    if not df.empty:
        _table = _mdl.__table__.name
        _unique_keys = ['id']
        _update_columns = _mdl.fields_f_update
        df = df_init_model(df, _mdl)
        result = upsert_df_to_db(
            df, _table, _engine, _unique_keys, _update_columns)
    else:
        logger.info("->无需更新")
    return result
