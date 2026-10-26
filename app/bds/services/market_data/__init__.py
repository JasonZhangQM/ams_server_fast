# -*- coding: utf-8 -*-
"""行情数据同步：指数历史行情、指数实时行情、每日估值指标、每日市值指标、期权行情。

`option_market` 是配置驱动的期权行情同步（读 `bds_option_config`，写 `bds_option_market`），
调用方式是模块级（`option_market.build_sync_plan()` 等），故不在此处摊平成函数名——
与 `services/index_valuation` 的处理方式一致。
"""
from server_fast.app.bds.services.market_data.index_history import (
    sync_one_index,
    upsert_index_history_sql,
)
from server_fast.app.bds.services.market_data.index_realtime import (
    fetch_realtime_index_prices,
)
from server_fast.app.bds.services.market_data.daily_valuation import (
    upsert_daily_valuation_sql,
)
from server_fast.app.bds.services.market_data.daily_mktvalue import (
    upsert_daily_mktvalue_sql,
)

__all__ = [
    'sync_one_index',
    'upsert_index_history_sql',
    'fetch_realtime_index_prices',
    'upsert_daily_valuation_sql',
    'upsert_daily_mktvalue_sql',
]
