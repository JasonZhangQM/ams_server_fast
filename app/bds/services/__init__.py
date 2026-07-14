# -*- coding: utf-8 -*-
"""bds 业务函数统一出口。

从 services/data_sync.py（基础市场数据 + 财务数据）和 services/economic_indicator.py（经济指标）
统一导出，router.py 只需从此处导入。
"""
from server_fast.app.bds.services.data_sync import (
    insert_trade_date_em_sql,
    upsert_daily_valuation_sql,
    upsert_finance_deriv_sql,
    upsert_fund_balance_sql,
    upsert_fund_cashflow_sql,
    upsert_fund_income_sql,
    upsert_index_constituent_sql,
    upsert_index_history_sql,
    upsert_symbol_info_excel_sql,
)
from server_fast.app.bds.services.economic_indicator import (
    upsert_all_economic_indicators_sql,
    upsert_economic_indicator_sql,
)

__all__ = [
    'insert_trade_date_em_sql',
    'upsert_daily_valuation_sql',
    'upsert_finance_deriv_sql',
    'upsert_fund_balance_sql',
    'upsert_fund_cashflow_sql',
    'upsert_fund_income_sql',
    'upsert_index_constituent_sql',
    'upsert_index_history_sql',
    'upsert_symbol_info_excel_sql',
    'upsert_all_economic_indicators_sql',
    'upsert_economic_indicator_sql',
]
