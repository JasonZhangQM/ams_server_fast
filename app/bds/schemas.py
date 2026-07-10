# -*- coding: utf-8 -*-
"""bds 应用的 Pydantic v2 响应 Schema。

用于 API 响应序列化，字段与 ORM 模型一一对应：
- TradeDateOut: 交易日历响应模型（对应 TradeDate）
- SymbolInfoOut: 证券信息响应模型（对应 SymbolInfo）

均配置 from_attributes=True，支持从 ORM 实例或 dict 直接构造。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TradeDateOut(BaseModel):
    """交易日历响应 Schema（对应 bds.TradeDate 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int  # 主键
    trade_date: date  # 交易日
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class SymbolInfoOut(BaseModel):
    """证券信息响应 Schema（对应 bds.SymbolInfo 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int  # 主键
    symbol: str  # 代码
    name: str  # 名称
    industry: Optional[str] = None  # 所属行业
    online_date: Optional[date] = None  # 上市日期
    price: Optional[Decimal] = None  # 最新价
    rate: Optional[Decimal] = None  # 涨幅%
    pe_lyr: Optional[Decimal] = None  # PE(静)
    pe_ttm: Optional[Decimal] = None  # PE(TTM)
    pb: Optional[Decimal] = None  # PB
    dy_ttm: Optional[Decimal] = None  # 股息(TTM%)
    roe: Optional[Decimal] = None  # ROE(%)
    yoy_in: Optional[Decimal] = None  # 总收入(%)
    yoy_np: Optional[Decimal] = None  # 净利润(%)
    gpm: Optional[Decimal] = None  # 毛利率(%)
    dar: Optional[Decimal] = None  # 负债率(%)
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class IndexHistoryOut(BaseModel):
    """指数历史行情响应 Schema（对应 bds.IndexHistory 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int  # 主键
    symbol: str  # 代码
    trade_date: date  # 交易日期
    open: Optional[Decimal] = None  # 开盘价
    high: Optional[Decimal] = None  # 最高价
    low: Optional[Decimal] = None  # 最低价
    close: Optional[Decimal] = None  # 收盘价
    sec_name: Optional[str] = None  # 指数名称（从 Config.INDEX_CODE 查找，不存数据库）
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class IndexConstituentOut(BaseModel):
    """指数成分股响应 Schema（对应 bds.IndexConstituent 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    index_code: str  # 指数代码
    symbol: str  # 成分股代码
    weight: Optional[Decimal] = None  # 权重
    trade_date: date  # 交易日期
    sec_name: Optional[str] = None  # 指数名称（不入库，从 Config.INDEX_CODE 查找）


class FundBalanceOut(BaseModel):
    """资产负债表响应 Schema（对应 bds.FundBalance 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="股票代码")
    pub_date: Optional[date] = Field(default=None, description="发布日期")
    rpt_date: Optional[date] = Field(default=None, description="报告日期")
    rpt_type: Optional[int] = Field(default=None, description="报表类型")
    data_type: Optional[int] = Field(default=None, description="数据类型")
    # ---- 资产类字段 ----
    mny_cptl: Optional[Decimal] = Field(default=None, description="货币资金")
    acct_rcv: Optional[Decimal] = Field(default=None, description="应收账款")
    invt: Optional[Decimal] = Field(default=None, description="存货")
    ttl_cur_ast: Optional[Decimal] = Field(default=None, description="流动资产合计")
    fix_ast: Optional[Decimal] = Field(default=None, description="固定资产")
    lt_eqy_inv: Optional[Decimal] = Field(default=None, description="长期股权投资")
    intg_ast: Optional[Decimal] = Field(default=None, description="无形资产")
    gw: Optional[Decimal] = Field(default=None, description="商誉")
    ttl_ncur_ast: Optional[Decimal] = Field(default=None, description="非流动资产合计")
    ttl_ast: Optional[Decimal] = Field(default=None, description="资产总计")
    # ---- 负债类字段 ----
    sht_ln: Optional[Decimal] = Field(default=None, description="短期借款")
    acct_pay: Optional[Decimal] = Field(default=None, description="应付账款")
    ttl_cur_liab: Optional[Decimal] = Field(default=None, description="流动负债合计")
    lt_ln: Optional[Decimal] = Field(default=None, description="长期借款")
    ttl_ncur_liab: Optional[Decimal] = Field(default=None, description="非流动负债合计")
    ttl_liab: Optional[Decimal] = Field(default=None, description="负债合计")
    # ---- 权益类字段 ----
    cptl_rsv: Optional[Decimal] = Field(default=None, description="资本公积")
    ret_prof: Optional[Decimal] = Field(default=None, description="未分配利润")
    ttl_eqy_pcom: Optional[Decimal] = Field(default=None, description="归母股东权益合计")
    ttl_eqy: Optional[Decimal] = Field(default=None, description="股东权益合计")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")
