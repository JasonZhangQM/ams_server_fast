"""资金试算业务函数（从 bills/service.py 拆分）。"""

import logging

import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import filter_dtypes, df_init_model, upsert_df_to_db, get_sql_to_df
from server_fast.app.bills.models import Bill, Group

logger = logging.getLogger("uvicorn.error")


## 资金试算
# 获取df最后一行并添加到汇总表中
def last_row_group_cash(df: pd.DataFrame, group_dict: dict, df_empty: pd.DataFrame) -> pd.DataFrame:
    last_row = df.iloc[-1]
    last_row_add = pd.Series(
        [group_dict['id'], group_dict['account'], group_dict['category'],
         group_dict['symbol'], group_dict['start_time'],
         group_dict['end_time'], group_dict['count'],
         group_dict['end_time']
         ],  ###
        index=[
            'id', 'account', 'category', 'symbol', 'start_time', 'end_time',
            'count', 'profit_time'
        ])
    last_row = pd.concat([last_row, last_row_add], ignore_index=False, axis=0)
    df_empty = pd.concat([df_empty, last_row.to_frame().T], ignore_index=True)
    df_empty = df_empty.fillna(0)
    df_empty['cost_total'] = df_empty['cost_t_long']
    return df_empty

# 资金日结
def cash_acc_daily_group_sql(account):
    _mdl = Bill
    _engine = settings.DB_ENGINE
    sql = f'''
        SELECT trade_time, amount_act FROM {_mdl.__table__.name}
        WHERE account='{account}'
            AND category<>'-'
            AND (category1<>'转入' AND category1<>'转出')
        '''
    df = get_sql_to_df(sql, _engine)
    df = df.sort_values(by='trade_time')  # 按交易时间排序
    df['trade_time'] = df['trade_time'].dt.floor('D')  # 取交易日
    df = df.fillna(0)
    # 转换数据类型
    df = df.astype(
        filter_dtypes(list(df.columns), _mdl.to_dtype()))
    df.set_index('trade_time', inplace=True)
    daily_df = df.groupby(df.index).agg(
        cost_t_long=("amount_act", "sum")
    ).round(2)
    daily_df['cost_t_long'] = daily_df['cost_t_long'].cumsum().round(2)  # 累计和
    return daily_df

# 资金试算
def cash_update_group_sql():
    _engine = settings.DB_ENGINE
    _mdl_g = Group
    sql = f'''
        SELECT {','.join(_mdl_g.fields_pl)} FROM {_mdl_g.__table__.name}
        WHERE category='cash'
            AND (end_time<>profit_time OR profit_time IS NULL);
        '''
    group_list = (get_sql_to_df(sql, _engine)).to_dict('records')
    df_group = pd.DataFrame()  # 初始化df
    for group_dict in group_list:
        _account = group_dict['account']
        daily_df = cash_acc_daily_group_sql(_account)  # 获取账单数据
        df_group = last_row_group_cash(daily_df, group_dict, df_group)
    logger.info("资金试算更新汇总表")
    if not df_group.empty:  # 更新bills_group表
        df_group = df_init_model(df_group, _mdl_g, is_id=True)
        _table = _mdl_g.__table__.name
        _unique_keys = _mdl_g.unique_keys
        _fields_update = _mdl_g.fields_pl_update
        result = upsert_df_to_db(
            df_group, _table, _engine, _unique_keys, _fields_update)
        logger.info(f'->更新成功:{result}')
    else:
        logger.info("->无需更新")
