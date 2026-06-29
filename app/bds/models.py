# -*- coding: utf-8 -*-
"""bds 应用的 SQLAlchemy 2.0 模型。

由 Django (server_dj/apps/bds/models.py) 迁移而来：
- SymbolInfo: 证券信息
- TradeDate: 交易日历

继承 (Base, BaseModel)：Base 来自 server_fast.common.db，
BaseModel mixin 提供 id/create_time/update_time 通用字段及
to_dtype/map_fields/db_fields/to_dict 方法。
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from server_fast.common.db import Base
from server_fast.common.models import BaseModel


class SymbolInfo(Base, BaseModel):
    """证券信息模型（对应表 bds_symbol_info）。"""

    __tablename__ = "bds_symbol_info"

    # ---- 基础信息 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="代码")
    name: Mapped[str] = mapped_column(String(32), nullable=False, comment="名称")
    industry: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="所属行业")
    online_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="上市日期")

    # ---- 行情/估值指标：除 price 精度为 4 位外，其余均为 Numeric(12, 2) ----
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最新价")
    rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="涨幅%")
    pe_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PE(静)")
    pe_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PE(TTM)")
    pb: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PB")
    dy_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="股息(TTM%)")
    roe: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="ROE(%)")
    yoy_in: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="总收入(%)")
    yoy_np: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="净利润(%)")
    gpm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="毛利率(%)")
    dar: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="负债率(%)")

    # ---- 附加类属性：被 BaseModel 的 map_fields() 等方法使用 ----
    # 字段匹配外部表头：{db_field: [别名...]}
    cols_map_fields = {
        "symbol": ["代码"],
        "name": ["名称"],
        "industry": ["所属行业"],
        "online_date": ["上市日期"],
        "offline_date": ["退市日期"],
        "price": ["最新", "最新价"],
        "rate": ["涨幅%"],
        "pe_lyr": ["市盈率(静)"],
        "pe_ttm": ["市盈率(TTM)"],
        "pb": ["市净率"],
        "dy_ttm": ["股息率TTM%"],
        "roe": ["加权净资产收益率%"],
        "yoy_in": ["营业总收入同比%"],
        "yoy_np": ["归属净利润同比%"],
        "gpm": ["销售毛利率%"],
        "dar": ["资产负债比率%"],
    }
    # 唯一键字段列表
    unique_keys = ["symbol"]
    # 需要 '—' 替换的字段列表
    fields_replace = [
        "online_date", "price", "rate", "pe_lyr", "pe_ttm", "pb", "dy_ttm",
        "roe", "yoy_in", "yoy_np", "gpm", "dar",
    ]

    # 表级参数：保留原 Django Meta.indexes 中的命名索引（与 unique=True 互不冲突）
    __table_args__ = (
        Index("k_bds_symbol_info_symbol", "symbol"),
    )

    def __str__(self) -> str:
        """模型实例字符串表示，便于调试与日志输出。"""
        return f"{self.symbol}-{self.name}"


class TradeDate(Base, BaseModel):
    """交易日历模型（对应表 bds_trade_date）。"""

    __tablename__ = "bds_trade_date"

    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日")

    def __str__(self) -> str:
        """模型实例字符串表示，便于调试与日志输出。"""
        return f"交易日:{self.trade_date}"
