"""汇总账单及账户/标的汇总业务函数（从 bills/service.py 拆分，并合并 account_summary）。"""

import logging

import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import (
    filter_in_cols,
    filter_dtypes,
    df_init_model,
    upsert_df_to_db,
    get_sql_to_df,
    act_sql_engine,
)
from server_fast.app.bills.config import Config as BlsCfg
from server_fast.app.bills.models import Bill, Group, GroupAcc, GroupSymbol

logger = logging.getLogger("uvicorn.error")


## 汇总账单
# 更新账单中的代码
def update_symbol_bill_sql():
    _engine = settings.DB_ENGINE
    _update_dict = BlsCfg.MAP_SYMBOL  # 更新账单中的代码
    _mdl = Bill
    sql = f'''
        UPDATE {_mdl.__table__.name}
        SET symbol=:new_symbol
        WHERE symbol=:old_symbol
        '''
    params = [
        {'old_symbol': k, 'new_symbol': v} for k, v in _update_dict.items()
    ]
    result = act_sql_engine(_engine, sql, params)
    logger.info(f'更新代码:{result}')

# 删除汇总表中的旧代码
def del_old_symbol_group_sql():
    _engine = settings.DB_ENGINE
    _mdl = Group
    _update_dict = BlsCfg.MAP_SYMBOL  # 删除汇总表中的旧代码
    _keys_str = [f"'{item}'" for item in _update_dict.keys()]
    sql = f'''
        DELETE FROM {_mdl.__table__.name}
        WHERE symbol IN ({', '.join(_keys_str)})
        '''
    result = act_sql_engine(_engine, sql)
    logger.info(f'删除代码:{result}')

# 导出账单全部数据
def export_all_data_bill():
    _engine = settings.DB_ENGINE
    _mdl = Bill
    sql = f'''
        SELECT account,category,symbol,trade_time
        FROM {_mdl.__table__.name}
        '''
    df = get_sql_to_df(sql, _engine)
    df = df[~df['category'].str.contains('-')]  # 去掉包含-的数据
    return df

# 汇总资金余额
def upsert_group_cash_sql():
    _engine = settings.DB_ENGINE
    _mdl_group = Group
    _mdl_bill = Bill
    df = export_all_data_bill()
    # 按账户汇总数据(汇总资金余额用)
    group_df = df.groupby(
        by=['account']).agg(
        start_time=("trade_time", "min"),
        end_time=("trade_time", "max"),
        count=("trade_time", "count")
    )
    group_df['category'] = 'cash'
    group_df['symbol'] = 'cash'
    group_df.reset_index(inplace=True)  # 重置索引(索引变为列)
    group_df = group_df[filter_in_cols(group_df.columns, _mdl_group.db_fields())]
    _unique_keys = _mdl_group.unique_keys  # 唯一索引字段
    _table_name = _mdl_group.__table__.name
    result = upsert_df_to_db(group_df, _table_name, _engine, _unique_keys)
    logger.info(f'汇总资金:{result}')

# 汇总损益
def upsert_group_profit_sql():
    _engine = settings.DB_ENGINE
    _mdl_group = Group
    _mdl_bill = Bill
    df = export_all_data_bill()
    # 按账户、分类和标的汇总数据(汇总成本收益用)
    group_df = df.groupby(
        by=['account', 'category', 'symbol']).agg(
        start_time=("trade_time", "min"),
        end_time=("trade_time", "max"),
        count=("trade_time", "count")
    )
    group_df.reset_index(inplace=True)  # 重置索引(索引变为列)
    group_df = group_df[filter_in_cols(group_df.columns, _mdl_group.db_fields())]
    if not group_df.empty:  # 更新交易账单表
        _unique_keys = _mdl_group.unique_keys  # 唯一索引字段
        _table_name = _mdl_group.__table__.name
        result = upsert_df_to_db(group_df, _table_name, _engine, _unique_keys)
        logger.info(f'汇总收益:{result}')
    else:
        logger.info("->无需更新")


## 账户与标的汇总
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
    # 获取Group数据
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
