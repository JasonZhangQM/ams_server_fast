"""汇总账单业务函数（从 bills/service.py 拆分）。"""

import logging

import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import filter_in_cols, upsert_df_to_db, get_sql_to_df, act_sql_engine
from server_fast.app.bills.config import Config as BlsCfg
from server_fast.app.bills.models import Bill, Group

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
