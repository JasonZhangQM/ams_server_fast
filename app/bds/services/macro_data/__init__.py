# -*- coding: utf-8 -*-
"""宏观数据同步：经济指标、黄金储备、美债收益率指标。"""
from server_fast.app.bds.services.macro_data.economic_indicator import (
    upsert_all_economic_indicators_sql,
    upsert_economic_indicator_from_wscn_sql,
    upsert_economic_indicator_sql,
)
from server_fast.app.bds.services.macro_data.gold_reserve import (
    upsert_all_gold_reserves_sql,
    upsert_gold_reserve_sql,
)
from server_fast.app.bds.services.macro_data.daily_indicator import (
    upsert_all_daily_indicators_sql,
    upsert_daily_indicator_sql,
)

__all__ = [
    'upsert_all_economic_indicators_sql',
    'upsert_economic_indicator_from_wscn_sql',
    'upsert_economic_indicator_sql',
    'upsert_all_gold_reserves_sql',
    'upsert_gold_reserve_sql',
    'upsert_all_daily_indicators_sql',
    'upsert_daily_indicator_sql',
]
