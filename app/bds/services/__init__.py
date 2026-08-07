# -*- coding: utf-8 -*-
"""bds 业务函数统一出口。

按 6 个数据域分目录组织，router.py 只需从此处导入：
- macro_data/         宏观数据（经济指标、黄金储备、美债收益率）
- fund_data/          财务数据（三大财报 + 财务指标）
- market_data/        行情数据（指数历史、实时行情、每日估值）
- index_constituent/  指数成分
- symbol_info/        证券信息
- trade_date/         交易日历
"""
from server_fast.app.bds.services.trade_date import (
    insert_trade_date_em_sql,
)
from server_fast.app.bds.services.symbol_info import (
    upsert_symbol_info_excel_sql,
)
from server_fast.app.bds.services.market_data import (
    fetch_realtime_index_prices,
    upsert_index_history_sql,
    upsert_daily_valuation_sql,
)
from server_fast.app.bds.services.index_constituent import (
    upsert_index_constituent_sql,
)
from server_fast.app.bds.services.fund_data import (
    upsert_fund_balance_sql,
    upsert_fund_cashflow_sql,
    upsert_fund_income_sql,
    upsert_finance_deriv_sql,
)
from server_fast.app.bds.services.macro_data import (
    upsert_all_economic_indicators_sql,
    upsert_economic_indicator_from_wscn_sql,
    upsert_economic_indicator_sql,
    upsert_all_gold_reserves_sql,
    upsert_gold_reserve_sql,
    upsert_all_daily_indicators_sql,
    upsert_daily_indicator_sql,
)

__all__ = [
    # 交易日历
    'insert_trade_date_em_sql',
    # 证券信息
    'upsert_symbol_info_excel_sql',
    # 行情数据
    'fetch_realtime_index_prices',
    'upsert_index_history_sql',
    'upsert_daily_valuation_sql',
    # 指数成分
    'upsert_index_constituent_sql',
    # 财务数据
    'upsert_fund_balance_sql',
    'upsert_fund_cashflow_sql',
    'upsert_fund_income_sql',
    'upsert_finance_deriv_sql',
    # 宏观数据
    'upsert_all_economic_indicators_sql',
    'upsert_economic_indicator_from_wscn_sql',
    'upsert_economic_indicator_sql',
    'upsert_all_gold_reserves_sql',
    'upsert_gold_reserve_sql',
    'upsert_all_daily_indicators_sql',
    'upsert_daily_indicator_sql',
]
