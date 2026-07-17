# -*- coding: utf-8 -*-
"""irs 应用 Pydantic v2 响应 Schema 定义。

对应 router.py 中 7 个 GET 路由的返回结构：
- SymbolValueOut      /irs/symbol-values        估值配置全字段
- SymbolKpiOut        /irs/symbol-kpis          估值指标自身字段
- MonitorValueOut     /irs/value-monitor        按 MonitorValue.fields_request 输出
- SymbolOptionOut     /irs/symbol-options       期权配置 + 标的扁平化字段
- MonitorOptionOut    /irs/monitor-options      期权监测 + 期权/标的扁平化字段
- MonitorOptionTOut   /irs/monitor-option-ts    期权T价 + 期权/标的扁平化字段
- DiscountMonitorOut  /irs/monitor-discounts    贴水监测全字段（合并配置+监测）

所有 Schema 均启用 from_attributes=True 以支持从 ORM 实例直接构造；
MonitorValueOut 额外启用 populate_by_name=True，以同时接受双下划线别名与字段名入参。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# =========================================================================
# 估值配置：对应 SymbolValue 模型全字段（25 字段）
# =========================================================================
class SymbolValueOut(BaseModel):
    """估值配置响应（对应 /irs/symbol-values）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                # 代码
    name: Optional[str] = None                 # 名称
    pp_el: Optional[Decimal] = None            # 极低
    pp_l: Optional[Decimal] = None             # 低
    pp_m: Optional[Decimal] = None             # 中
    pp_h: Optional[Decimal] = None             # 高
    pp_eh: Optional[Decimal] = None            # 极高
    vix: Optional[Decimal] = None              # 波指
    p_total: Optional[int] = None              # 目标量
    p_init: Optional[int] = None               # V1
    p_inc: Optional[int] = None                # 增量
    v2: Optional[int] = None                   # V2
    v3: Optional[int] = None                   # V3
    m_tot: Optional[Decimal] = None            # 目标(万)
    m_init: Optional[Decimal] = None           # 首笔(万)
    bg_p_bid1: Optional[Decimal] = None        # 买点1
    bg_p_bid2: Optional[Decimal] = None        # 买点2
    bg_p_bid3: Optional[Decimal] = None        # 买点3
    py_close: Optional[Decimal] = None         # 上年末
    y_high: Optional[Decimal] = None           # 年高
    y_low: Optional[Decimal] = None            # 年低
    last_close: Optional[Decimal] = None       # 昨收
    id: int                                    # 主键
    create_time: datetime                      # 创建时间
    update_time: datetime                      # 更新时间


# =========================================================================
# 估值指标：对应 SymbolKpi 自身字段（不含关联，20 字段）
# =========================================================================
class SymbolKpiOut(BaseModel):
    """估值指标响应（对应 /irs/symbol-kpis）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol_value_id: int                                    # 估值标的
    last_ratio: Optional[Decimal] = None                    # 昨收%
    max_ratio: Optional[Decimal] = None                     # 年高%
    min_ratio: Optional[Decimal] = None                     # 年低%
    roe_cut: Optional[Decimal] = None                       # ROE(cut)
    inc_oper_yoy: Optional[Decimal] = None                  # 营收yoy)
    net_prof_pcom_cut_yoy: Optional[Decimal] = None         # 净利yoy
    sale_gpm: Optional[Decimal] = None                      # 毛利率
    sale_npm: Optional[Decimal] = None                      # 净利率
    ast_liab_rate: Optional[Decimal] = None                 # 负债率
    pe_ttm_cut: Optional[Decimal] = None                    # PE(ttm)
    pe_lyr_cut: Optional[Decimal] = None                    # PE(lyr)
    pb_lyr: Optional[Decimal] = None                        # PB(lyr)
    pcf_ttm_oper: Optional[Decimal] = None                  # PCo(ttm)
    peg_lyr: Optional[Decimal] = None                       # PEG(lyr)
    dy_ttm: Optional[Decimal] = None                        # DY(ttm)
    dy_lfy: Optional[Decimal] = None                        # DY(lfy)
    id: int                                                 # 主键
    create_time: datetime                                   # 创建时间
    update_time: datetime                                   # 更新时间


# =========================================================================
# 估值监测：按 MonitorValue.fields_request 输出（12 字段）
# 含双下划线字段通过 alias 映射到单下划线字段名；symbol_value__vr 不存在，值为 None
# =========================================================================
class MonitorValueOut(BaseModel):
    """估值监测响应（对应 /irs/value-monitor）。

    router 按 MonitorValue.fields_request 构建字典，键为原始双下划线路径
    （如 'symbol_value__symbol'），此处通过 Field(alias=...) 映射到合法字段名。
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # 关联 SymbolValue 字段（经 _resolve_field 解析，关联为 None 时取 None）
    symbol_value_symbol: Optional[str] = Field(
        default=None, alias="symbol_value__symbol", description="代码"
    )
    symbol_value_pp_el: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_el", description="极低"
    )
    symbol_value_pp_l: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_l", description="低"
    )
    symbol_value_pp_m: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_m", description="中"
    )
    symbol_value_pp_h: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_h", description="高"
    )
    symbol_value_pp_eh: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_eh", description="极高"
    )
    symbol_value_bg_p_bid1: Optional[Decimal] = Field(
        default=None, alias="symbol_value__bg_p_bid1", description="买点1"
    )
    symbol_value_bg_p_bid2: Optional[Decimal] = Field(
        default=None, alias="symbol_value__bg_p_bid2", description="买点2"
    )
    symbol_value_bg_p_bid3: Optional[Decimal] = Field(
        default=None, alias="symbol_value__bg_p_bid3", description="买点3"
    )
    # symbol_value__vr 在模型中不存在，router 中 _resolve_field 返回 None
    symbol_value_vr: Optional[Any] = Field(
        default=None, alias="symbol_value__vr", description="保留字段(不存在)"
    )
    # MonitorValue 自身字段
    rh: Optional[Decimal] = None                            # 阶段高
    price: Optional[Decimal] = None                         # 最新价


# =========================================================================
# 期权配置：SymbolOption 自身字段 + 标的扁平化（11 字段）
# =========================================================================
class SymbolOptionOut(BaseModel):
    """期权配置响应（对应 /irs/symbol-options）。"""

    model_config = ConfigDict(from_attributes=True)

    underlying_id: int                                    # 期权标的
    price_strike: Decimal                                 # 行权价
    delisted_date: date                                   # 行权日
    days_left: Optional[int] = None                       # 剩余天数
    value_per: Optional[Decimal] = None                   # 单点价值
    id: int                                               # 主键
    create_time: datetime                                 # 创建时间
    update_time: datetime                                 # 更新时间
    # 扁平化嵌入 SymbolUnderlying 字段（关联对象可能为 None）
    underlying_symbol: Optional[str] = None               # 代码
    underlying_name: Optional[str] = None                 # 名称
    underlying_multiplier: Optional[int] = None           # 期权乘数


# =========================================================================
# 期权监测：MonitorOption 自身字段 + 期权/标的扁平化（21 字段）
# =========================================================================
class MonitorOptionOut(BaseModel):
    """期权监测响应（对应 /irs/monitor-options）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                          # 期权代码
    option_id: int                                       # 期权配置
    option_type: str                                     # 期权类型
    price_ud: Optional[Decimal] = None                   # 标的现价
    price: Optional[Decimal] = None                      # 期权现价
    value_t: Optional[Decimal] = None                    # 时间价值
    value_i: Optional[Decimal] = None                    # 内在价值
    atm_i: Optional[Decimal] = None                      # 平值(%)
    ratio_t: Optional[Decimal] = None                    # 时间(%)
    ratio_i: Optional[Decimal] = None                    # 内在(%)
    ratio_t_y: Optional[Decimal] = None                  # 时间(%Y)
    ratio_i_y: Optional[Decimal] = None                  # 内在(%Y)
    id: int                                              # 主键
    create_time: datetime                                # 创建时间
    update_time: datetime                                # 更新时间
    # 扁平化嵌入 SymbolOption 字段
    option_price_strike: Optional[Decimal] = None        # 行权价
    option_delisted_date: Optional[date] = None          # 行权日
    option_days_left: Optional[int] = None               # 剩余天数
    option_value_per: Optional[Decimal] = None           # 单点价值
    # 扁平化嵌入 SymbolUnderlying 字段
    underlying_symbol: Optional[str] = None              # 代码
    underlying_name: Optional[str] = None                # 名称


# =========================================================================
# 期权T价：MonitorOptionT 自身字段 + 期权/标的扁平化（24 字段）
# =========================================================================
class MonitorOptionTOut(BaseModel):
    """期权T型报价响应（对应 /irs/monitor-option-ts）。"""

    model_config = ConfigDict(from_attributes=True)

    option_id: int                                       # 期权配置
    price_ud: Optional[Decimal] = None                   # 标的现价
    price_c: Optional[Decimal] = None                    # 认购现价
    value_t_c: Optional[Decimal] = None                  # 时间价值c
    value_i_c: Optional[Decimal] = None                  # 内在价值c
    ratio_t_c: Optional[Decimal] = None                  # 时间(%)c
    ratio_i_c: Optional[Decimal] = None                  # 内在(%)c
    ratio_t_y_c: Optional[Decimal] = None                # 时间(%Y)c
    ratio_i_y_c: Optional[Decimal] = None                # 内在(%Y)c
    price_p: Optional[Decimal] = None                    # 认沽现价
    value_t_p: Optional[Decimal] = None                  # 时间价值p
    value_i_p: Optional[Decimal] = None                  # 内在价值p
    ratio_t_p: Optional[Decimal] = None                  # 时间(%)p
    ratio_i_p: Optional[Decimal] = None                  # 内在(%)p
    ratio_t_y_p: Optional[Decimal] = None                # 时间(%Y)p
    ratio_i_y_p: Optional[Decimal] = None                # 内在(%Y)p
    id: int                                              # 主键
    create_time: datetime                                # 创建时间
    update_time: datetime                                # 更新时间
    # 扁平化嵌入 SymbolOption 字段
    option_price_strike: Optional[Decimal] = None        # 行权价
    option_delisted_date: Optional[date] = None          # 行权日
    option_days_left: Optional[int] = None               # 剩余天数
    # 扁平化嵌入 SymbolUnderlying 字段
    underlying_symbol: Optional[str] = None              # 代码
    underlying_name: Optional[str] = None                # 名称


# =========================================================================
# 贴水监测：对应 DiscountMonitor 模型全字段（16 字段，合并后单表）
# =========================================================================
class DiscountMonitorOut(BaseModel):
    """贴水监测响应（对应 /irs/monitor-discounts，合并配置+监测全字段）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol_con: str                                 # 连续合约
    symbol: Optional[str] = None                    # 真实合约
    is_main: bool                                   # 主力
    symbol_type: Optional[str] = None               # 合约类别
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
