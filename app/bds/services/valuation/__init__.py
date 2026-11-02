# -*- coding: utf-8 -*-
"""估值分位查询域：指数（index_valuation，根目录历史遗留）+ 个股（stock_valuation）。

个股估值分位直接读 `bds_daily_valuation`（个股日频 PE/PB/PS/PCF/DY，唯一键
symbol+trade_date，最左前缀即 symbol），**现算不落库**——与指数分位同理由：
窗口每天滑动、当前值每天新增，存下来当天有效次日即过期。单标的序列约 1~6 千行，
走唯一键前缀毫秒级，无聚合表需求。
模块级调用方式与 `index_valuation` 一致，不摊平进 `__all__`。
"""
from . import stock_valuation

__all__ = ["stock_valuation"]
