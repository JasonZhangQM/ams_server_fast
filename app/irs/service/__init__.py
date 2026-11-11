# -*- coding: utf-8 -*-
"""irs 业务函数包：按业务领域拆分为子模块，统一 re-export 保持调用方不变。

子模块：
- common：日志器、通用 session 封装
- underlying_monitor：标的监测（UnderlyingMonitor）
- discount_monitor：贴水监测（DiscountMonitor）
（期权监测 option_monitor 已于 2026-10-29 下线，行情统一走 bds_option_market）

router.py 通过 `from server_fast.app.irs import service` + `service.xxx` 调用，
本 __init__ re-export 所有公开函数以保持该调用方式不变。
"""
from server_fast.app.irs.service.common import (
    logger,
)
from server_fast.app.irs.service.underlying_monitor import (
    get_history_em_df,
    handle_hlc_df,
    update_underlying_monitor_hlc_sql,
    update_underlying_monitor_em_orm,
)
from server_fast.app.irs.service.discount_monitor import (
    real_symbols_em,
    symbol_infos_em,
    upsert_discount_monitor_config_sql,
    upsert_discount_monitor_em_sql,
    update_is_main_em_sql,
    discount_yield_em_orm,
)
from server_fast.app.irs.service import (
    um_version,
    um_research,
    um_plan,
    um_review,
    um_map,
    um_today,
    um_percentile,
    um_budget,
    budget,
    risk_scenario,
    um_level_scenario,
    um_market_stats,
    um_simulate,
)

__all__ = [
    # common
    'logger',
    # underlying_monitor
    'get_history_em_df',
    'handle_hlc_df',
    'update_underlying_monitor_hlc_sql',
    'update_underlying_monitor_em_orm',
    # discount_monitor
    'real_symbols_em',
    'symbol_infos_em',
    'upsert_discount_monitor_config_sql',
    'upsert_discount_monitor_em_sql',
    'update_is_main_em_sql',
    'discount_yield_em_orm',
    # v3：标的层档案
    'um_version',
    'um_research',
    'um_plan',
    'um_review',
    'um_map',
    'um_today',
    # 2026-11-05：最新估值分位截面表（irs_um_percentile）
    'um_percentile',
    # 2026-11-06：加仓点 × 风险预算
    'um_budget',
    # 2026-11-08：风险看板（自 bills 模块整体迁入）
    'budget',
    'risk_scenario',
    # 2026-11-08：标的监测抽屉「到档情景」（复用 risk_scenario.run 的 shifts）
    'um_level_scenario',
    # 2026-11-08：标的监测抽屉「估值与走势」（分位 + 估值 + 收益 + 回撤）
    'um_market_stats',
    # 2026-11-11：推演工作台（多标的假设价 × 组合联动推演）
    'um_simulate',
]
