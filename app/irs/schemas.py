# -*- coding: utf-8 -*-
"""irs 应用 Pydantic v2 响应 Schema 定义。

对应 router.py 中 GET 路由的返回结构：
- DiscountMonitorOut  /irs/discounts-monitor    贴水监测全字段（合并配置+监测）
- ValueMonitorOut     /irs/value-monitors       估值监测全字段
- OptionMonitorOut    /irs/option-monitors      期权监测全字段

所有 Schema 均启用 from_attributes=True 以支持从 ORM 实例直接构造。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# =========================================================================
# 贴水监测：对应 DiscountMonitor 模型全字段（16 字段，合并后单表）
# =========================================================================
class DiscountMonitorOut(BaseModel):
    """贴水监测响应（对应 /irs/discounts-monitor，合并配置+监测全字段）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol_con: str                                 # 连续合约
    symbol: Optional[str] = None                    # 真实合约
    is_main: bool                                   # 主力
    symbol_type: Optional[str] = None               # 合约类别
    con_name: Optional[str] = None                  # 连续周期
    symbol_ud: Optional[str] = None                 # 标的代码
    delisted_date: Optional[date] = None            # 到期日
    days_left: Optional[int] = None                 # 剩余天数
    position: Optional[int] = None                  # 持仓量
    price: Optional[Decimal] = None                 # 合约现价
    price_ud: Optional[Decimal] = None              # 基础现价
    discount: Optional[Decimal] = None              # 贴水
    ratio: Optional[Decimal] = None                 # 贴水率(%)
    ratio_y: Optional[Decimal] = None               # 贴水率(%Y)
    id: int                                         # 主键
    create_time: datetime                           # 创建时间
    update_time: datetime                           # 更新时间


# =========================================================================
# 估值监测：对应 ValueMonitor 模型全字段（25 字段）
# =========================================================================
class ValueMonitorOut(BaseModel):
    """估值监测响应（对应 /irs/value-monitors，独立表含估值区间+行情+监测字段）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                # 代码
    name: Optional[str] = None                 # 名称
    # 估值区间
    pp_el: Optional[Decimal] = None            # 极低
    pp_l: Optional[Decimal] = None             # 低
    pp_m: Optional[Decimal] = None             # 中
    pp_h: Optional[Decimal] = None             # 高
    pp_eh: Optional[Decimal] = None            # 极高
    # 行情字段
    py_close: Optional[Decimal] = None         # 上年末
    y_high: Optional[Decimal] = None           # 年高
    y_low: Optional[Decimal] = None            # 年低
    price: Optional[Decimal] = None            # 最新价
    # 行情监测字段
    pv_yh: Optional[Decimal] = None            # 年高(%)
    pv_yl: Optional[Decimal] = None            # 年低(%)
    pv_yy: Optional[Decimal] = None            # 最新(%)
    # 估值监测字段
    pv_el: Optional[Decimal] = None            # 极低(%)
    pv_l: Optional[Decimal] = None             # 低(%)
    pv_m: Optional[Decimal] = None             # 中(%)
    pv_h: Optional[Decimal] = None             # 高(%)
    pv_eh: Optional[Decimal] = None            # 极高(%)
    pv_el_y: Optional[Decimal] = None          # 极低(y%)
    pv_l_y: Optional[Decimal] = None           # 低(y%)
    pv_m_y: Optional[Decimal] = None           # 中(y%)
    pv_h_y: Optional[Decimal] = None           # 高(y%)
    pv_eh_y: Optional[Decimal] = None          # 极高(y%)
    id: int                                    # 主键
    create_time: datetime                      # 创建时间
    update_time: datetime                      # 更新时间


# =========================================================================
# 估值监测新增：请求 Schema（7 个必填字段）
# =========================================================================
class ValueMonitorCreate(BaseModel):
    """估值监测新增请求（对应 POST /irs/value-monitors）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                # 代码
    name: str                                  # 名称
    pp_el: Decimal                             # 极低
    pp_l: Decimal                              # 低
    pp_m: Decimal                              # 中
    pp_h: Decimal                              # 高
    pp_eh: Decimal                             # 极高


# =========================================================================
# 估值监测修改：请求 Schema（6 个必填字段 + 4 个可选行情字段，symbol 不可改）
# =========================================================================
class ValueMonitorUpdate(BaseModel):
    """估值监测修改请求（对应 PUT /irs/value-monitors/{id}）。

    name/pp_* 为必填字段；py_close/y_high/y_low/price 为可选行情字段，
    None 表示不修改，作为同步接口拉取失败时的兜底编辑入口。
    """

    model_config = ConfigDict(from_attributes=True)

    name: str                                  # 名称
    pp_el: Decimal                             # 极低
    pp_l: Decimal                              # 低
    pp_m: Decimal                              # 中
    pp_h: Decimal                              # 高
    pp_eh: Decimal                             # 极高
    # 行情字段（可选，None 表示不修改）
    py_close: Optional[Decimal] = None         # 上年末
    y_high: Optional[Decimal] = None           # 年高
    y_low: Optional[Decimal] = None            # 年低
    price: Optional[Decimal] = None            # 最新价


# =========================================================================
# 期权监测合并：对应 OptionMonitor 模型全字段（19 字段，合并配置+监测单表）
# =========================================================================
class OptionMonitorOut(BaseModel):
    """期权监测合并响应（对应 /irs/option-monitors，合并配置+监测全字段）。"""

    model_config = ConfigDict(from_attributes=True)

    underlying_symbol: str                           # 标的代码
    price_strike: Decimal                            # 行权价
    delisted_date: date                              # 到期日
    days_left: Optional[int] = None                  # 剩余天数
    multiplier: int                                  # 期权乘数
    symbol: str                                      # 期权代码
    option_type: str                                 # 期权类型
    price_ud: Optional[Decimal] = None               # 标的现价
    price: Optional[Decimal] = None                  # 期权现价
    value_t: Optional[Decimal] = None                # 时间价值
    value_i: Optional[Decimal] = None                # 内在价值
    atm_i: Optional[Decimal] = None                  # 平值(%)
    ratio_t: Optional[Decimal] = None                # 时间(%)
    ratio_i: Optional[Decimal] = None                # 内在(%)
    ratio_t_y: Optional[Decimal] = None              # 时间(%Y)
    ratio_i_y: Optional[Decimal] = None              # 内在(%Y)
    id: int                                          # 主键
    create_time: datetime                            # 创建时间
    update_time: datetime                            # 更新时间
