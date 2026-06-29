# -*- coding: utf-8 -*-
"""irs 应用 Pydantic v2 响应 Schema 定义。

对应 router.py 中 8 个 GET 路由的返回结构：
- SymbolValueOut      /irs/symbol-values        估值配置全字段
- SymbolKpiOut        /irs/symbol-kpis          估值指标自身字段
- MonitorValueOut     /irs/value-monitor        按 MonitorValue.fields_request 输出
- SymbolOptionOut     /irs/symbol-options       期权配置 + 标的扁平化字段
- MonitorOptionOut    /irs/monitor-options      期权监测 + 期权/标的扁平化字段
- MonitorOptionTOut   /irs/monitor-option-ts    期权T价 + 期权/标的扁平化字段
- SymbolDiscountOut   /irs/symbol-discounts     贴水配置全字段
- MonitorDiscountOut  /irs/monitor-discounts    贴水监测 + 贴水配置扁平化字段

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
    pp_el: Optional[Decimal] = None            # 估值价-极低
    pp_l: Optional[Decimal] = None             # 估值价-低
    pp_m: Optional[Decimal] = None             # 估值价-中（自动计算）
    pp_h: Optional[Decimal] = None             # 估值价-高
    pp_eh: Optional[Decimal] = None            # 估值价-极高
    vix: Optional[Decimal] = None              # 回撤率
    p_total: Optional[int] = None              # 总批次
    p_init: Optional[int] = None               # 首批
    p_inc: Optional[int] = None                # 加批次
    v2: Optional[int] = None                   # 第二批买入量（自动计算）
    v3: Optional[int] = None                   # 第三批买入量（自动计算）
    m_tot: Optional[Decimal] = None            # 总金额(万)（自动计算）
    m_init: Optional[Decimal] = None           # 首笔金额(万)（自动计算）
    bg_p_bid1: Optional[Decimal] = None        # 买点1（自动计算）
    bg_p_bid2: Optional[Decimal] = None        # 买点2（自动计算）
    bg_p_bid3: Optional[Decimal] = None        # 买点3（自动计算）
    py_close: Optional[Decimal] = None         # 上年末收盘价
    y_high: Optional[Decimal] = None           # 近期高点
    y_low: Optional[Decimal] = None            # 近期低点
    last_close: Optional[Decimal] = None       # 最新收盘价
    id: int                                    # 主键
    create_time: datetime                      # 创建时间
    update_time: datetime                      # 更新时间


# =========================================================================
# 估值指标：对应 SymbolKpi 自身字段（不含关联，20 字段）
# =========================================================================
class SymbolKpiOut(BaseModel):
    """估值指标响应（对应 /irs/symbol-kpis）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol_value_id: int                                    # 关联估值配置ID
    last_ratio: Optional[Decimal] = None                    # 最新涨跌幅(%)
    max_ratio: Optional[Decimal] = None                     # 区间最大涨幅(%)
    min_ratio: Optional[Decimal] = None                     # 区间最小跌幅(%)
    roe_cut: Optional[Decimal] = None                       # ROE(扣非)
    inc_oper_yoy: Optional[Decimal] = None                  # 营收同比(%)
    net_prof_pcom_cut_yoy: Optional[Decimal] = None         # 扣非净利同比(%)
    sale_gpm: Optional[Decimal] = None                      # 销售毛利率(%)
    sale_npm: Optional[Decimal] = None                      # 销售净利率(%)
    ast_liab_rate: Optional[Decimal] = None                 # 资产负债率(%)
    pe_ttm_cut: Optional[Decimal] = None                    # PE(TTM扣非)
    pe_lyr_cut: Optional[Decimal] = None                    # PE(LYR扣非)
    pb_lyr: Optional[Decimal] = None                        # PB(LYR)
    pcf_ttm_oper: Optional[Decimal] = None                  # PCF(TTM经营)
    peg_lyr: Optional[Decimal] = None                       # PEG(LYR)
    dy_ttm: Optional[Decimal] = None                        # 股息率(TTM)(%)
    dy_lfy: Optional[Decimal] = None                        # 股息率(LFY)(%)
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
        default=None, alias="symbol_value__symbol", description="估值配置代码"
    )
    symbol_value_pp_el: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_el", description="估值价-极低"
    )
    symbol_value_pp_l: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_l", description="估值价-低"
    )
    symbol_value_pp_m: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_m", description="估值价-中"
    )
    symbol_value_pp_h: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_h", description="估值价-高"
    )
    symbol_value_pp_eh: Optional[Decimal] = Field(
        default=None, alias="symbol_value__pp_eh", description="估值价-极高"
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
    rh: Optional[Decimal] = None                            # 近期高点
    price: Optional[Decimal] = None                         # 最新价


# =========================================================================
# 期权配置：SymbolOption 自身字段 + 标的扁平化（11 字段）
# =========================================================================
class SymbolOptionOut(BaseModel):
    """期权配置响应（对应 /irs/symbol-options）。"""

    model_config = ConfigDict(from_attributes=True)

    underlying_id: int                                    # 关联标的ID
    price_strike: Decimal                                 # 行权价
    delisted_date: date                                   # 到期日
    days_left: Optional[int] = None                       # 剩余天数（自动计算）
    value_per: Optional[Decimal] = None                   # 单点价值（自动计算）
    id: int                                               # 主键
    create_time: datetime                                 # 创建时间
    update_time: datetime                                 # 更新时间
    # 扁平化嵌入 SymbolUnderlying 字段（关联对象可能为 None）
    underlying_symbol: Optional[str] = None               # 标的代码
    underlying_name: Optional[str] = None                 # 标的名称
    underlying_multiplier: Optional[int] = None           # 标的乘数


# =========================================================================
# 期权监测：MonitorOption 自身字段 + 期权/标的扁平化（21 字段）
# =========================================================================
class MonitorOptionOut(BaseModel):
    """期权监测响应（对应 /irs/monitor-options）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                          # 期权代码
    option_id: int                                       # 关联期权配置ID
    option_type: str                                     # 期权类型(call/put)
    price_ud: Optional[Decimal] = None                   # 标的现价
    price: Optional[Decimal] = None                      # 期权现价
    value_t: Optional[Decimal] = None                    # 时间价值（自动计算）
    value_i: Optional[Decimal] = None                    # 内在价值（自动计算）
    atm_i: Optional[Decimal] = None                      # 平值度(%)
    ratio_t: Optional[Decimal] = None                    # 时间价值占比(%)
    ratio_i: Optional[Decimal] = None                    # 内在价值占比(%)
    ratio_t_y: Optional[Decimal] = None                  # 年化时间占比(%)
    ratio_i_y: Optional[Decimal] = None                  # 年化内在占比(%)
    id: int                                              # 主键
    create_time: datetime                                # 创建时间
    update_time: datetime                                # 更新时间
    # 扁平化嵌入 SymbolOption 字段
    option_price_strike: Optional[Decimal] = None        # 行权价
    option_delisted_date: Optional[date] = None          # 到期日
    option_days_left: Optional[int] = None               # 剩余天数
    option_value_per: Optional[Decimal] = None           # 单点价值
    # 扁平化嵌入 SymbolUnderlying 字段
    underlying_symbol: Optional[str] = None              # 标的代码
    underlying_name: Optional[str] = None                # 标的名称


# =========================================================================
# 期权T价：MonitorOptionT 自身字段 + 期权/标的扁平化（24 字段）
# =========================================================================
class MonitorOptionTOut(BaseModel):
    """期权T型报价响应（对应 /irs/monitor-option-ts）。"""

    model_config = ConfigDict(from_attributes=True)

    option_id: int                                       # 关联期权配置ID
    price_ud: Optional[Decimal] = None                   # 标的现价
    price_c: Optional[Decimal] = None                    # 认购期权现价
    value_t_c: Optional[Decimal] = None                  # 认购时间价值
    value_i_c: Optional[Decimal] = None                  # 认购内在价值
    ratio_t_c: Optional[Decimal] = None                  # 认购时间占比(%)
    ratio_i_c: Optional[Decimal] = None                  # 认购内在占比(%)
    ratio_t_y_c: Optional[Decimal] = None                # 认购年化时间占比(%)
    ratio_i_y_c: Optional[Decimal] = None                # 认购年化内在占比(%)
    price_p: Optional[Decimal] = None                    # 认沽期权现价
    value_t_p: Optional[Decimal] = None                  # 认沽时间价值
    value_i_p: Optional[Decimal] = None                  # 认沽内在价值
    ratio_t_p: Optional[Decimal] = None                  # 认沽时间占比(%)
    ratio_i_p: Optional[Decimal] = None                  # 认沽内在占比(%)
    ratio_t_y_p: Optional[Decimal] = None                # 认沽年化时间占比(%)
    ratio_i_y_p: Optional[Decimal] = None                # 认沽年化内在占比(%)
    id: int                                              # 主键
    create_time: datetime                                # 创建时间
    update_time: datetime                                # 更新时间
    # 扁平化嵌入 SymbolOption 字段
    option_price_strike: Optional[Decimal] = None        # 行权价
    option_delisted_date: Optional[date] = None          # 到期日
    option_days_left: Optional[int] = None               # 剩余天数
    # 扁平化嵌入 SymbolUnderlying 字段
    underlying_symbol: Optional[str] = None              # 标的代码
    underlying_name: Optional[str] = None                # 标的名称


# =========================================================================
# 贴水配置：对应 SymbolDiscount 模型全字段（9 字段）
# =========================================================================
class SymbolDiscountOut(BaseModel):
    """贴水配置响应（对应 /irs/symbol-discounts）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol_con: str                                 # 连续合约代码
    symbol: Optional[str] = None                    # 真实合约代码
    is_main: bool                                   # 是否主力
    symbol_type: Optional[str] = None               # 合约类别（自动解析）
    symbol_ud: Optional[str] = None                 # 标的代码
    delisted_date: Optional[date] = None            # 到期日
    id: int                                         # 主键
    create_time: datetime                           # 创建时间
    update_time: datetime                           # 更新时间


# =========================================================================
# 贴水监测：MonitorDiscount 自身字段 + 贴水配置扁平化（17 字段）
# =========================================================================
class MonitorDiscountOut(BaseModel):
    """贴水监测响应（对应 /irs/monitor-discounts）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol_real_id: int                             # 关联贴水配置ID
    days_left: Optional[int] = None                 # 剩余天数（自动计算）
    position: Optional[int] = None                  # 累计持仓
    price: Decimal                                  # 合约现价
    price_ud: Decimal                               # 基础现价
    discount: Optional[Decimal] = None              # 贴水值（自动计算）
    ratio: Optional[Decimal] = None                 # 贴水率(%)
    ratio_y: Optional[Decimal] = None               # 年化贴水率(%)
    id: int                                         # 主键
    create_time: datetime                           # 创建时间
    update_time: datetime                           # 更新时间
    # 扁平化嵌入 SymbolDiscount 字段（关联对象可能为 None）
    symbol: Optional[str] = None                    # 真实合约代码
    symbol_con: Optional[str] = None                # 连续合约代码
    is_main: Optional[bool] = None                  # 是否主力
    symbol_type: Optional[str] = None               # 合约类别
    symbol_ud: Optional[str] = None                 # 标的代码
    delisted_date: Optional[date] = None            # 到期日
