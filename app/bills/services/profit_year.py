# -*- coding: utf-8 -*-
"""年度收益统计业务函数（按年度汇总 Profit 表盈亏合计）。"""

import logging

import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import (
    df_init_model,
    upsert_df_to_db,
    get_sql_to_df,
)
from server_fast.app.bills.models import Bill, Profit, ProfitYear

logger = logging.getLogger("uvicorn.error")


# 年度收益统计
def upsert_profit_year_sql():
    """按年度聚合 Profit 表盈亏并 UPSERT 到 bills_profit_year 表。

    通过 JOIN bills_bill 取 trade_time 提取年份，对 pl_long/pl_short/pl_other/pl_br
    按 year 分组求和，pl_all 为四者合计（与 group-accs 口径一致），
    pl_cumulative 为按年升序的累计求和。空数据时跳过 UPSERT。
    """
    _engine = settings.DB_ENGINE
    _mdl = ProfitYear
    # JOIN bills_bill 取 trade_time，YEAR() 提取年度
    # pl_br（融资利息）纳入 pl_all 计算，与 bills_group_acc 的合计口径保持一致
    sql = f'''
        SELECT YEAR(b.trade_time) AS year,
               p.pl_long, p.pl_short, p.pl_other, p.pl_br
        FROM bills_profit p
        JOIN bills_bill b ON p.bill_id = b.id
        WHERE b.trade_time IS NOT NULL;
    '''
    df = get_sql_to_df(sql, _engine)
    logger.info("年度收益统计")
    if df.empty:
        logger.info("->无需更新")
        return 0
    # MySQL Decimal 列在 pandas 中为 object 类型，groupby(numeric_only=True) 会过滤掉
    # 需先转为 float 再聚合
    numeric_cols = ['pl_long', 'pl_short', 'pl_other', 'pl_br']
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors='coerce').fillna(0)
    # 按 year 分组求和
    df_year = df.groupby('year', as_index=False)[numeric_cols].sum()
    # pl_all = pl_long + pl_short + pl_other + pl_br（与 group-accs 口径一致）
    df_year['pl_all'] = (
        df_year['pl_long'].fillna(0)
        + df_year['pl_short'].fillna(0)
        + df_year['pl_other'].fillna(0)
        + df_year['pl_br'].fillna(0)
    )
    # 累计盈亏：按年度升序后对 pl_all 累计求和（该年及之前所有年份合计）
    df_year = df_year.sort_values('year')
    df_year['pl_cumulative'] = df_year['pl_all'].cumsum()
    df_in = df_init_model(df_year, _mdl)
    _table = _mdl.__table__.name
    _unique_keys = ['year']
    result = upsert_df_to_db(df_in, _table, _engine, _unique_keys)
    logger.info(f"->更新成功:{result}")
    return result
