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

from pydantic import BaseModel, ConfigDict


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
