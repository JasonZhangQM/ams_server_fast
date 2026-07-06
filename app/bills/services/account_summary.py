"""账户与标的汇总业务函数（从 bills/service.py 拆分）。"""

import logging

import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import (
    filter_dtypes,
    df_init_model,
    upsert_df_to_db,
    get_sql_to_df,
)
from server_fast.app.bills.models import Group, GroupAcc, GroupSymbol

logger = logging.getLogger("uvicorn.error")


# 账户汇总
# 取出汇总所有数据，汇总平仓盈亏，总盈亏
def get_group_sql_df():
    _engine = settings.DB_ENGINE
    _mdl = Group
    sql = f'''
        SELECT {','.join(_mdl.fields_api_details)}
        FROM bills_group
        ORDER BY account;
    '''
    df = get_sql_to_df(sql, _engine)
    df = df.fillna(0)
    df = df.astype(filter_dtypes(df.columns, _mdl.to_dtype()))
    df['pl_all'] = (
        df['pl_total'] + df['pl_t_other'] + df['pl_t_br'])
    df['pfl_all'] = (
        df['pl_all'] + df['pf_total'])
    return df

# 账户汇总
def upsert_group_acc_sql():
    _engine = settings.DB_ENGINE
    _mdl = GroupAcc
    # 获取Group数据，按account汇总
    df_g = get_group_sql_df()  # 获取数据
    df_acc = df_g.groupby('account').sum(numeric_only=True)
    df_acc['status'] = (  # 验证
        df_acc['cost_total'] - df_acc['pl_all'] -
        df_acc['diff_dw'] - df_acc['diff_dwt']
    ).round(0)
    # 提出现金、理财与市值合并(区分现金、理财和证券市值)
    df_cash_acc = df_g[df_g['category'] == 'cash']
    df_cash_acc = df_cash_acc.fillna(0)
    df_cash_acc = df_cash_acc.groupby('account').sum(numeric_only=True)
    df_acc['cash_acc'] = df_cash_acc['cost_total']
    df_fm_acc = df_g[df_g['category'] == '理财']
    df_fm_acc = df_fm_acc.fillna(0)
    df_fm_acc = df_fm_acc.groupby('account').sum(numeric_only=True)
    df_acc['fm_acc'] = df_fm_acc['cost_total']
    df_acc = df_acc.fillna(0)
    df_acc['cost_total'] = (  # 证券成本剔除现金、理财
        df_acc['cost_total'] - df_acc['cash_acc'] - df_acc['fm_acc'])
    df_acc['acc_aset'] = df_acc['value_total']  # 账户净值
    df_acc['value_total'] = (  # 证券市值剔除现金、理财
        df_acc['value_total'] - df_acc['cash_acc'] - df_acc['fm_acc'])

    # 求合计数
    new_row_df = df_acc.sum(
        numeric_only=True
    ).to_frame().T.set_index([['合计']])
    df_acc = pd.concat([df_acc, new_row_df])
    df_acc.index.names = ['account']  # 索引重命名

    df_acc.reset_index(inplace=True)
    result = 0
    if not df_acc.empty:
        df_in = df_init_model(df_acc, _mdl)
        _table = _mdl.__table__.name
        _unique_keys = ['account']
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys)
    return result

# 标的汇总
def upsert_group_symbol_sql():
    _engine = settings.DB_ENGINE
    _mdl = Group
    _mdl_symbol = GroupSymbol
    # 获取Group数据（原 Django _meta.fields 改为 __table__.columns）
    sql = f'''
        SELECT {','.join([col.name for col in _mdl.__table__.columns])}
        FROM bills_group
        ORDER BY account;
    '''
    df_g = get_sql_to_df(sql, _engine)
    df_g = df_g.fillna(0)
    df_g = df_g.astype(filter_dtypes(df_g.columns, _mdl.to_dtype()))
    df_g['pl_all'] = (  # 平仓盈亏
        df_g['pl_total'] + df_g['pl_t_other'] + df_g['pl_t_br'])
    df_g['pfl_all'] = (  # 盈亏合计
        df_g['pl_all'] + df_g['pf_total'])

    df_symbol = df_g.groupby(['category', 'symbol']).sum(numeric_only=True)

    df_symbol.reset_index(inplace=True)
    result = 0
    if not df_symbol.empty:
        df_in = df_init_model(df_symbol, _mdl_symbol)
        _table = _mdl_symbol.__table__.name
        _unique_keys = _mdl_symbol.unique_keys
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys)
    return result
