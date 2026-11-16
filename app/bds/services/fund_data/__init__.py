# -*- coding: utf-8 -*-
"""财务数据同步：三大财报 + 财务指标 + 指数成分股财务聚合。"""
from server_fast.app.bds.services.fund_data.fund_reports import (
    upsert_fund_balance_sql,
    upsert_fund_income_sql,
    upsert_fund_cashflow_sql,
    upsert_finance_deriv_sql,
)
from server_fast.app.bds.services.fund_data.index_fund_agg import (
    aggregate_index_fund,
)

__all__ = [
    'upsert_fund_balance_sql',
    'upsert_fund_income_sql',
    'upsert_fund_cashflow_sql',
    'upsert_finance_deriv_sql',
    'aggregate_index_fund',
]
