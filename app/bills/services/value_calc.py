"""实时市值业务函数（从 bills/service.py 拆分）。"""
import logging
import pandas as pd
import akshare as ak
from gm.api import *
from server_fast.config import settings
from server_fast.common.utils import filter_dtypes, map_value, df_init_model, upsert_df_to_db, get_sql_to_df, call_with_timeout
from server_fast.app.bills.config import Config as BlsCfg
from server_fast.app.bills.models import Group

logger = logging.getLogger("uvicorn.error")

# 实时市值
# 根据实时数据，计算持有市值与浮动盈亏
def value_float(df, current_data, multiplier):
    symbol_dict = {item['symbol']: item['price'] for item in current_data}
    df['price'] = df['symbol'].map(symbol_dict)
    df['multiplier'] = df['symbol'].apply(
        map_value, multiplier=multiplier)
    cdt = (df['p_long'] > 0 & df['price'].notna())  # 多头持仓市值=多头持仓数量*最新价格*乘数
    df.loc[cdt, 'value_long'] = (
        df.loc[cdt, 'price'] * df.loc[cdt, 'p_long'] * df.loc[cdt, 'multiplier']
    ).round(2)
    cdt = (df['p_short'] > 0 & df['price'].notna())  # 空头持仓市值=空头持仓数量*最新价格*乘数
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
    # 计算浮动盈亏
    df = df.fillna(0)
    df['value_total'] = (
        df['value_long'] + df['value_short']).round(2)
    df['pf_long'] = (
        df['value_long'] - df['cost_t_long']).round(2)
    df['pf_short'] = (
        df['cost_t_short'] - df['value_short']).round(2)
    df['pf_total'] = (
        df['pf_long'] + df['pf_short']).round(2)
    # 市值更新时间
    df['value_time'] = df['end_time']
    return df

# 获取期货实时行情（通过 AkShare）
# 解析数据库 symbol（如 SHEX.au2608），逐个调用 AkShare 接口获取实时价格
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
        market = BlsCfg.MAP_AKSHARE_MARKET.get(exchange_code)
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
    _multiplier = BlsCfg.MAP_MULTIPLIER
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
    # 按 category 分组获取行情：期货交易走 AkShare，其他走 gm API
    df_futures = df[df['category'] == '期货交易']
    df_others = df[df['category'] != '期货交易']
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
    # 分组仅用于行情获取，value_float 接收的 df 仍为完整的查询结果
    df = value_float(df, current_data, _multiplier)  # 计算市值与浮动盈亏
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
