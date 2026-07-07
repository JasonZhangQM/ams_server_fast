"""实时市值业务函数（从 bills/service.py 拆分）。"""
import logging
import pandas as pd
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
    # 获取实时数据（带超时保护，防止 gm 终端未启动时无限阻塞）
    try:
        current_data = call_with_timeout(current, timeout=10)(
            list(df['symbol']), fields=['symbol', 'price'])
    except Exception as e:
        logger.error(f'*****获取实时数据失败:{e}')
        raise e
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
