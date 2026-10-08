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
    pl_cumulative 为按年升序的累计求和。

    资产与收益率（Modified Dietz 近似）：
    - 年末资产 = 年初资产 + 当年出入金净额 + 当年 pl_all
    - 日均资产 = 年初资产 + Σ[每笔出入金 × 时间权重]，权重 = 该笔发生日至年末剩余天数 / 当年总天数
    - 收益率 = 当年 pl_all / 日均资产（日均资产 < 10000 时收益率无意义，置 NULL）
    - 第一年年初资产为 0，其后逐年递推。空数据时跳过 UPSERT。

    注意：出入金现金流不能直接取 p.diff_dw——该字段是收益试算分组内的
    滚动累计值（每行存"截至该笔的累计净入金"），求和会重复累加。
    需从 bills_bill 原始流水按 category1 逐笔计算（与 cash_calc 口径一致）。
    """
    _engine = settings.DB_ENGINE
    _mdl = ProfitYear
    # JOIN bills_bill 取 trade_time，YEAR() 提取年度
    # pl_br（融资利息）纳入 pl_all 计算，与 bills_group_acc 的合计口径保持一致
    # dw_flow：单笔出入金现金流（入金+、出金-），其余类型为 0
    sql = f'''
        SELECT YEAR(b.trade_time) AS year, b.trade_time,
               p.pl_long, p.pl_short, p.pl_other, p.pl_br,
               CASE WHEN b.category1='出金' THEN -ABS(b.amount_act)
                    WHEN b.category1='入金' THEN ABS(b.amount_act)
                    ELSE 0 END AS dw_flow
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
    numeric_cols = ['pl_long', 'pl_short', 'pl_other', 'pl_br', 'dw_flow']
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
    df_year = df_year.sort_values('year').reset_index(drop=True)
    df_year['pl_cumulative'] = df_year['pl_all'].cumsum()

    # ---- 日均资产与收益率（Modified Dietz：年初资产 + 出入金时间加权）----
    # 每笔出入金的时间权重：发生日至当年末的剩余天数占比（发生当年逐步生效）
    df['trade_time'] = pd.to_datetime(df['trade_time'])
    year_end = df['trade_time'].dt.to_period('Y').dt.end_time.dt.normalize()
    year_start = df['trade_time'].dt.to_period('Y').dt.start_time.dt.normalize()
    year_days = (year_end - year_start).dt.days + 1  # 当年总天数（兼容闰年）
    df['dw_weighted'] = df['dw_flow'] * ((year_end - df['trade_time'].dt.normalize()).dt.days / year_days)
    df_dw = df.groupby('year')[['dw_flow', 'dw_weighted']].sum()

    # 逐年递推：首年年初资产为 0，年末资产 = 年初 + 出入金净额 + 当年盈亏
    asset_begin = 0.0
    for idx, row in df_year.iterrows():
        dw_sum = df_dw.loc[row['year'], 'dw_flow'] if row['year'] in df_dw.index else 0.0
        dw_weighted = df_dw.loc[row['year'], 'dw_weighted'] if row['year'] in df_dw.index else 0.0
        asset_end = asset_begin + dw_sum + row['pl_all']
        asset_avg = asset_begin + dw_weighted
        df_year.loc[idx, 'asset_begin'] = asset_begin
        df_year.loc[idx, 'asset_end'] = asset_end
        df_year.loc[idx, 'asset_avg'] = asset_avg
        # 日均资产过小（如账户近乎空仓的年份）时收益率会被放大到无意义，置 NULL
        df_year.loc[idx, 'return_rate'] = (
            row['pl_all'] / asset_avg if asset_avg >= 10000 else None
        )
        asset_begin = asset_end  # 递推：下一年年初 = 本年年末

    df_in = df_init_model(df_year, _mdl)
    _table = _mdl.__table__.name
    _unique_keys = ['year']
    result = upsert_df_to_db(df_in, _table, _engine, _unique_keys)
    logger.info(f"->更新成功:{result}")
    return result
