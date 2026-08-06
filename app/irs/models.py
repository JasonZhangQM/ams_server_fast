# -*- coding: utf-8 -*-
"""irs 应用 SQLAlchemy 2.0 模型定义。

迁移自 server_dj/apps/irs/models.py，要点：
- 9 个 ORM 模型继承 (Base, BaseModel)，表名与原 Django class Meta.db_table 完全一致
- 原 Django save() 中的自动计算逻辑改写为 SQLAlchemy before_insert / before_update 事件钩子
- 保留所有自定义类属性（cols_map_fields / unique_keys / fields_request 等）
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from server_fast.common.db import Base
from server_fast.common.models import BaseModel


def _on_insert_update(model):
    """装饰器：把函数同时注册为 model 的 before_insert + before_update 钩子。

    替代 Django save() 中的自动计算逻辑：在 flush 前对 target 实例赋值计算字段。
    """

    def decorator(fn):
        event.listens_for(model, "before_insert")(fn)
        event.listens_for(model, "before_update")(fn)
        return fn

    return decorator


# =========================================================================
# 模型定义
# =========================================================================


class ValueMonitor(Base, BaseModel):
    """估值监测"""

    __tablename__ = "irs_value_monitor"
    __table_args__ = (
        Index("k_irs_value_monitor_symbol", "symbol"),
    )

    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, comment="代码")
    name: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="名称")
    #估值区间
    pp_el: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="极低")
    pp_l: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="低")
    pp_m: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="中")
    pp_h: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="高")
    pp_eh: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="极高")
    # 行情字段
    py_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="上年末")
    y_high: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="年高")
    y_low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="年低")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最新价")
    # 行情监测字段
    pv_yh: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="年高(%)")
    pv_yl: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="年低(%)")
    pv_yy: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="最新(%)")
    # 估值监测字段
    pv_el: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="极低(%)")
    pv_l: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="低(%)")
    pv_m: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="中(%)")
    pv_h: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="高(%)")
    pv_eh: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="极高(%)")
    pv_el_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="极低(y%)")
    pv_l_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="低(y%)")
    pv_m_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="中(y%)")
    pv_h_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="高(y%)")
    pv_eh_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="极高(y%)")

    # ---- 保留的自定义类属性 ----
    unique_keys = ["symbol"]
    # 年度行情更新时仅覆盖这三列，保护 pp_* 等用户手动配置字段
    fields_hlc_update = ["py_close", "y_high", "y_low"]

    def __str__(self):
        return f"{self.name}"


class OptionMonitor(Base, BaseModel):
    """期权监测合并表（合并原 SymbolOption 配置 + MonitorOption 监测为单表）。

    表名 irs_option_monitor，兼具配置与监测功能：
    - 配置字段：underlying_symbol/price_strike/delisted_date/days_left/multiplier
    - 监测字段：symbol/option_type/price_ud/price/value_t/value_i/atm_i/ratio_t/ratio_i/ratio_t_y/ratio_i_y
    - underlying_symbol 直存标的代码（原 underlying_id 外键改为字符串），multiplier 直存期权乘数（原 value_per 计算字段改为直存）
    """

    __tablename__ = "irs_option_monitor"
    __table_args__ = (
        UniqueConstraint(
            "underlying_symbol", "price_strike", "delisted_date", "option_type",
            name="uk_irs_option_monitor",
        ),
    )

    # 期权类型常量（保留原 MonitorOption 类属性）
    OPTION_TYPE_CALL = "call"  # 认购
    OPTION_TYPE_PUT = "put"    # 认沽

    # ---- 原 SymbolOption 配置字段 ----
    underlying_symbol: Mapped[str] = mapped_column(String(16), nullable=False, comment="标的代码")
    price_strike: Mapped[Decimal] = mapped_column(Numeric(9, 4), nullable=False, comment="行权价")
    delisted_date: Mapped[date] = mapped_column(Date, nullable=False, comment="到期日")
    days_left: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="剩余天数")
    multiplier: Mapped[int] = mapped_column(Integer, nullable=False, comment="期权乘数")

    # ---- 原 MonitorOption 监测字段 ----
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, comment="期权代码")
    option_type: Mapped[str] = mapped_column(
        String(8), nullable=False, default=OPTION_TYPE_CALL, comment="期权类型"
    )
    price_ud: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, default=Decimal("1"), comment="标的现价"
    )
    price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 4), nullable=True, default=Decimal("1"), comment="期权现价"
    )
    value_t: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4), nullable=True, comment="时间价值")
    value_i: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4), nullable=True, comment="内在价值")
    atm_i: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="平值(%)")
    ratio_t: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="时间(%)")
    ratio_i: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="内在(%)")
    ratio_t_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="时间(%Y)")
    ratio_i_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="内在(%Y)")

    # ---- 保留的自定义类属性（合并自 MonitorOption）----
    cols_map_fields = {
        "symbol": ["代码"],
        "price": ["最新"],
    }

    def __str__(self):
        return (
            f"{self.underlying_symbol}-{self.price_strike}-{self.delisted_date.year}"
            f"{self.delisted_date.month}-{self.option_type}"
        )


class DiscountMonitor(Base, BaseModel):
    """贴水监测（合并原 SymbolDiscount 配置 + MonitorDiscount 监测为单表）。

    表名 irs_discount_monitor，兼具配置与监测功能：
    - 配置字段：symbol_con/symbol/is_main/symbol_type/symbol_ud/delisted_date
    - 监测字段：days_left/position/price/price_ud/discount/ratio/ratio_y
    - price/price_ud 由原 MonitorDiscount 的 NotNull 改为 nullable=True（同步行情前可能无值）
    """

    __tablename__ = "irs_discount_monitor"

    # 期权主力标记常量（保留原 Django 类属性）
    OPTION_MAIN = True    # 是
    OPTION_MINOR = False  # 否

    # ---- 原有配置字段 ----
    symbol_con: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, comment="连续合约")
    symbol: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="真实合约")
    is_main: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=OPTION_MINOR, comment="主力"
    )
    symbol_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="合约类别")
    con_name: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="连续周期")
    symbol_ud: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="标的代码")
    delisted_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="到期日")

    # ---- 新增监测字段（从 MonitorDiscount 合并） ----
    days_left: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="剩余天数")
    position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="持仓量")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="合约现价")
    price_ud: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="基础现价")
    discount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="贴水")
    ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="贴水率(%)")
    ratio_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="贴水率(%Y)")

    # ---- 保留的自定义类属性 ----
    cols_map_fields = {
        "symbol": ["代码"],
        "symbol_ud": ["underlying_symbol"],
        "delisted_date": ["delisted_date"],
    }
    unique_keys = ["symbol_con"]
    fields_yiels = ["id", "symbol", "symbol_ud", "delisted_date"]

    def __str__(self):
        return f"{self.symbol_con}({self.symbol})"


# =========================================================================
# 事件钩子：替代原 Django save() 中的自动计算逻辑
# =========================================================================


@_on_insert_update(ValueMonitor)
def _compute_value_monitor(mapper, connection, target):
    """ValueMonitor 计算逻辑：基于本表 price/py_close/y_high/y_low/pp_* 计算监测指标(%)。

    仅当 price 非空时执行计算，避免除零异常。
    """
    if not target.price:
        return
    # 相对上年末收益率 = (行情价/上年末 - 1) * 100
    target.pv_yh = (target.y_high / target.py_close - Decimal("1")) * Decimal("100")
    target.pv_yl = (target.y_low / target.py_close - Decimal("1")) * Decimal("100")
    target.pv_yy = (target.price / target.py_close - Decimal("1")) * Decimal("100")
    # 估值收益率 = (估值价/最新价 - 1) * 100
    target.pv_el = (target.pp_el / target.price - Decimal("1")) * Decimal("100")
    target.pv_l = (target.pp_l / target.price - Decimal("1")) * Decimal("100")
    target.pv_m = (target.pp_m / target.price - Decimal("1")) * Decimal("100")
    target.pv_h = (target.pp_h / target.price - Decimal("1")) * Decimal("100")
    target.pv_eh = (target.pp_eh / target.price - Decimal("1")) * Decimal("100")
    # 相对上年末收益率 = (估值价/上年末 - 1) * 100
    target.pv_el_y = (target.pp_el / target.py_close - Decimal("1")) * Decimal("100")
    target.pv_l_y = (target.pp_l / target.py_close - Decimal("1")) * Decimal("100")
    target.pv_m_y = (target.pp_m / target.py_close - Decimal("1")) * Decimal("100")
    target.pv_h_y = (target.pp_h / target.py_close - Decimal("1")) * Decimal("100")
    target.pv_eh_y = (target.pp_eh / target.py_close - Decimal("1")) * Decimal("100")


@_on_insert_update(OptionMonitor)
def _compute_option_monitor(mapper, connection, target):
    """OptionMonitor 计算逻辑（合并原 SymbolOption + MonitorOption 的 save()）：
    - days_left = (delisted_date - today).days
    - multiplier 直接存储，不计算（原 value_per 计算已移除）
    - atm_i/value_i/value_t/ratio_t/ratio_i/ratio_t_y/ratio_i_y 基于本表字段计算
    """
    # 来自 SymbolOption 钩子（仅 days_left，multiplier 直存不计算）
    target.days_left = (target.delisted_date - date.today()).days
    # price_ud 或 price 为 None 时（如 gm 终端不可用），跳过衍生计算避免除以 None
    if target.price_ud is None or target.price is None:
        target.atm_i = None
        target.value_i = None
        target.value_t = None
        target.ratio_t = None
        target.ratio_i = None
        target.ratio_t_y = None
        target.ratio_i_y = None
        return
    # 来自 MonitorOption 钩子（option.xxx 改为 target.xxx，直接读本表字段）
    target.atm_i = (target.price_strike / target.price_ud - Decimal("1")) * Decimal("100")
    # 内在价值：认购/认沽方向不同
    if target.option_type == OptionMonitor.OPTION_TYPE_CALL:
        # 认购：行权价 > 标的现价时内在为 0，否则 = 标的现价 - 行权价
        target.value_i = Decimal("0") if target.price_strike > target.price_ud else target.price_ud - target.price_strike
    else:
        # 认沽：行权价 < 标的现价时内在为 0，否则 = 行权价 - 标的现价
        target.value_i = Decimal("0") if target.price_strike < target.price_ud else target.price_strike - target.price_ud
    # 时间价值 = 现价 - 内在价值
    target.value_t = target.price - target.value_i
    # 时间/内在价值占行权价百分比
    target.ratio_t = (target.value_t / target.price_strike) * Decimal("100")
    target.ratio_i = (target.value_i / target.price_strike) * Decimal("100")
    # 年化百分比（剩余天数为 0 时置 0，避免除零）
    if target.days_left != 0:
        target.ratio_t_y = target.ratio_t * Decimal("365") / target.days_left
        target.ratio_i_y = target.ratio_i * Decimal("365") / target.days_left
    else:
        target.ratio_t_y = Decimal("0")
        target.ratio_i_y = Decimal("0")


@_on_insert_update(DiscountMonitor)
def _compute_discount_monitor(mapper, connection, target):
    """DiscountMonitor 计算逻辑（合并原 SymbolDiscount + MonitorDiscount 的 save()）：
    - symbol_type/con_name：由 Config.SYMBOL_CON_LIST 配置取数（service 层写入），钩子不再解析
    - days_left：若 delisted_date 有值，计算 (delisted_date - today).days
    - discount/ratio/ratio_y：仅当 price 和 price_ud 均有值时计算，避免除零
      - discount  = price_ud - price  （基础现价 - 合约现价）
      - ratio     = discount / price * 100
      - ratio_y   = ratio * 365 / days_left （days_left 为 None/0 时置 0）
    """
    # 1. 计算剩余天数（delisted_date 为空则跳过，保留原值）
    if target.delisted_date is not None:
        target.days_left = (target.delisted_date - date.today()).days
    # 2. 计算贴水指标（price/price_ud 任一为空则跳过，避免除零）
    if target.price is not None and target.price_ud is not None:
        target.discount = target.price_ud - target.price
        target.ratio = (target.discount / target.price) * Decimal("100")
        if target.days_left not in (None, 0):
            target.ratio_y = target.ratio * Decimal("365") / target.days_left
        else:
            target.ratio_y = Decimal("0")
