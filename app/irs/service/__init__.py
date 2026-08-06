# -*- coding: utf-8 -*-
"""irs 业务函数包：按业务领域拆分为子模块，统一 re-export 保持调用方不变。

子模块：
- common：日志器、通用 session 封装
- value_monitor：估值监测（ValueMonitor）
- discount_monitor：贴水监测（DiscountMonitor）
- option_monitor：期权监测（OptionMonitor）

router.py 通过 `from server_fast.app.irs import service` + `service.xxx` 调用，
本 __init__ re-export 所有公开函数以保持该调用方式不变。
"""
from server_fast.app.irs.service.common import (
    logger,
)
from server_fast.app.irs.service.value_monitor import (
    get_history_em_df,
    handle_hlc_df,
    update_value_monitor_hlc_sql,
    update_value_monitor_em_orm,
)
from server_fast.app.irs.service.discount_monitor import (
    real_symbols_em,
    symbol_infos_em,
    upsert_discount_monitor_config_sql,
    upsert_discount_monitor_em_sql,
    update_is_main_em_sql,
    discount_yield_em_orm,
)
from server_fast.app.irs.service.option_monitor import (
    option_monitor_sync_orm,
)

__all__ = [
    # common
    'logger',
    # value_monitor
    'get_history_em_df',
    'handle_hlc_df',
    'update_value_monitor_hlc_sql',
    'update_value_monitor_em_orm',
    # discount_monitor
    'real_symbols_em',
    'symbol_infos_em',
    'upsert_discount_monitor_config_sql',
    'upsert_discount_monitor_em_sql',
    'update_is_main_em_sql',
    'discount_yield_em_orm',
    # option_monitor
    'option_monitor_sync_orm',
]
