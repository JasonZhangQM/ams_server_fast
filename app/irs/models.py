# -*- coding: utf-8 -*-
"""irs 应用 SQLAlchemy 2.0 模型定义。

迁移自 server_dj/apps/irs/models.py，要点：
- 9 个 ORM 模型继承 (Base, BaseModel)，表名与原 Django class Meta.db_table 完全一致
- 原 Django save() 中的自动计算逻辑改写为 SQLAlchemy before_insert / before_update 事件钩子
- 保留所有自定义类属性（cols_map_fields / unique_keys / fields_request 等）
- 外键列名统一以 _id 结尾（symbol_value_id / underlying_id / option_id / symbol_real_id）
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class SymbolValue(Base, BaseModel):
    """估值配置（原 irs.SymbolValue）。"""

    __tablename__ = "irs_symbol_value"
    __table_args__ = (
        # 索引名与原 Django models.Index(name=...) 一致
        Index("k_bds_symbol_value_symbol", "symbol"),
    )

    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, comment="代码")
    name: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="名称")
    pp_el: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="极低")
    pp_l: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="低")
    pp_m: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="中")
    pp_h: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="高")
    pp_eh: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="极高")
    vix: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2), nullable=True, comment="波指")
    p_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="目标量")
    p_init: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="V1")
    p_inc: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="增量")
    v2: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="V2")
    v3: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="V3")
    m_tot: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="目标(万)")
    m_init: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="首笔(万)")
    bg_p_bid1: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="买点1")
    bg_p_bid2: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="买点2")
    bg_p_bid3: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="买点3")
    py_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="上年末")
    y_high: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="年高")
    y_low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="年低")
    last_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="昨收")

    # ---- 保留的自定义类属性 ----
    fields_hlc_update = ["py_close", "y_high", "y_low", "last_close"]
    fields_value = [
        "id", "symbol", "pp_el", "pp_l", "pp_m", "pp_h", "pp_eh",
        "bg_p_bid1", "bg_p_bid2", "bg_p_bid3",
    ]
    unique_keys = ["symbol"]

    # 反向关系：SymbolKpi / MonitorValue 通过 symbol_value_id 关联回本表
    symbol_kpi: Mapped["SymbolKpi"] = relationship(
        "SymbolKpi", uselist=False, back_populates="symbol_value"
    )
    symbol_value_monitor: Mapped["MonitorValue"] = relationship(
        "MonitorValue", uselist=False, back_populates="symbol_value"
    )

    def __str__(self):
        return f"{self.name}"


class SymbolKpi(Base, BaseModel):
    """估值指标（原 irs.SymbolKpi）。OneToOne -> SymbolValue。"""

    __tablename__ = "irs_symbol_kpi"

    # 外键列名 symbol_value_id（Django db_column 显式指定）
    symbol_value_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("irs_symbol_value.id"), unique=True, nullable=False, comment="估值标的"
    )
    last_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="昨收%")
    max_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="年高%")
    min_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="年低%")
    roe_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="ROE(cut)")
    inc_oper_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="营收yoy)")
    net_prof_pcom_cut_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="净利yoy")
    sale_gpm: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="毛利率")
    sale_npm: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="净利率")
    ast_liab_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="负债率")
    pe_ttm_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="PE(ttm)")
    pe_lyr_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="PE(lyr)")
    pb_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="PB(lyr)")
    pcf_ttm_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="PCo(ttm)")
    peg_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="PEG(lyr)")
    dy_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="DY(ttm)")
    dy_lfy: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="DY(lfy)")

    # 关联对象（事件钩子通过 target.symbol_value 读取关联字段）
    symbol_value: Mapped["SymbolValue"] = relationship(
        "SymbolValue", back_populates="symbol_kpi"
    )

    def __str__(self):
        return f"{self.symbol_value.symbol}"


class MonitorValue(Base, BaseModel):
    """估值监测（原 irs.MonitorValue）。OneToOne -> SymbolValue。"""

    __tablename__ = "irs_monitor_value"

    symbol_value_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("irs_symbol_value.id"), unique=True, nullable=False, comment="估值标的"
    )
    rh: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="阶段高")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最新价")
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
    bg_d_bid1: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="买1(%)")
    bg_d_bid2: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="买2(%)")
    bg_d_bid3: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="买3(%)")
    hd_diff: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4), nullable=True, comment="回撤值")
    hd_target: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="回撤点")
    hd_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="回撤(%)")

    # ---- 保留的自定义类属性（路由层会使用） ----
    fields_request = [
        "symbol_value__symbol",
        "symbol_value__pp_el",
        "symbol_value__pp_l",
        "symbol_value__pp_m",
        "symbol_value__pp_h",
        "symbol_value__pp_eh",
        "symbol_value__bg_p_bid1",
        "symbol_value__bg_p_bid2",
        "symbol_value__bg_p_bid3",
        "symbol_value__vr",
        "rh",
        "price",
    ]

    symbol_value: Mapped["SymbolValue"] = relationship(
        "SymbolValue", back_populates="symbol_value_monitor"
    )

    def __str__(self):
        return f"{self.symbol_value.symbol}"


class SymbolUnderlying(BaseModel, Base):
    """期权标的（原 irs.SymbolUnderlying）。无 save() 计算逻辑。"""

    __tablename__ = "irs_symbol_underlying"

    symbol: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, comment="代码")
    name: Mapped[str] = mapped_column(String(16), nullable=False, comment="名称")
    multiplier: Mapped[int] = mapped_column(Integer, nullable=False, comment="期权乘数")

    # ---- 保留的自定义类属性 ----
    fields_not_null = ["id", "symbol", "name", "multiplier"]
    cols_map_fields = {
        "symbol": ["代码"],
        "name": ["名称"],
        "multiplier": ["乘数"],
    }
    unique_keys = ["symbol"]

    # 反向关系：多个 SymbolOption 指向本标的
    underlying_symbol: Mapped[list["SymbolOption"]] = relationship(
        "SymbolOption", back_populates="underlying"
    )

    def __str__(self):
        return f"{self.name}"


class SymbolOption(Base, BaseModel):
    """期权配置（原 irs.SymbolOption）。ManyToOne -> SymbolUnderlying。"""

    __tablename__ = "irs_symbol_option"
    __table_args__ = (
        # 联合唯一约束，外键列名 underlying_id（Django 默认列名）
        UniqueConstraint(
            "underlying_id", "price_strike", "delisted_date",
            name="uk_irs_symbol_option",
        ),
    )

    underlying_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("irs_symbol_underlying.id"), nullable=False, comment="期权标的"
    )
    price_strike: Mapped[Decimal] = mapped_column(Numeric(9, 4), nullable=False, comment="行权价")
    delisted_date: Mapped[date] = mapped_column(Date, nullable=False, comment="行权日")
    days_left: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="剩余天数")
    value_per: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="单点价值")

    underlying: Mapped["SymbolUnderlying"] = relationship(
        "SymbolUnderlying", back_populates="underlying_symbol"
    )
    # 反向：多个 MonitorOption + 一个 MonitorOptionT
    symbol_option_option: Mapped[list["MonitorOption"]] = relationship(
        "MonitorOption", back_populates="option"
    )
    symbol_option_t: Mapped["MonitorOptionT"] = relationship(
        "MonitorOptionT", uselist=False, back_populates="option"
    )

    def __str__(self):
        return (
            f"{self.underlying.name}.{self.underlying.symbol[:4]}"
            f"-{self.price_strike}-{self.days_left}"
        )


class MonitorOption(Base, BaseModel):
    """期权监测（原 irs.MonitorOption）。ManyToOne -> SymbolOption。"""

    __tablename__ = "irs_monitor_option"

    # 期权类型常量（保留原 Django 类属性）
    OPTION_TYPE_CALL = "call"  # 认购
    OPTION_TYPE_PUT = "put"    # 认沽

    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, comment="期权代码")
    option_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("irs_symbol_option.id"), nullable=False, comment="期权配置"
    )
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

    # ---- 保留的自定义类属性 ----
    cols_map_fields = {
        "symbol": ["代码"],
        "price": ["最新"],
    }

    option: Mapped["SymbolOption"] = relationship(
        "SymbolOption", back_populates="symbol_option_option"
    )

    def __str__(self):
        return (
            f"{self.option.underlying.name}.{self.option.underlying.symbol[:4]}"
            f"-{self.option.price_strike}-{self.option.delisted_date.year}"
            f"{self.option.delisted_date.month}-{self.option_type}"
        )


class MonitorOptionT(Base, BaseModel):
    """期权T价（原 irs.MonitorOptionT）。OneToOne -> SymbolOption。

    原 Django 模型无 save() 计算逻辑，故无事件钩子。
    """

    __tablename__ = "irs_monitor_option_t"

    option_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("irs_symbol_option.id"), unique=True, nullable=False, comment="期权配置"
    )
    price_ud: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, default=Decimal("1"), comment="标的现价"
    )
    price_c: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 4), nullable=True, default=Decimal("1"), comment="认购现价"
    )
    value_t_c: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4), nullable=True, comment="时间价值c")
    value_i_c: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4), nullable=True, comment="内在价值c")
    ratio_t_c: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="时间(%)c")
    ratio_i_c: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="内在(%)c")
    ratio_t_y_c: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="时间(%Y)c")
    ratio_i_y_c: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="内在(%Y)c")
    price_p: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 4), nullable=True, default=Decimal("1"), comment="认沽现价"
    )
    value_t_p: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4), nullable=True, comment="时间价值p")
    value_i_p: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4), nullable=True, comment="内在价值p")
    ratio_t_p: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="时间(%)p")
    ratio_i_p: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="内在(%)p")
    ratio_t_y_p: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="时间(%Y)p")
    ratio_i_y_p: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="内在(%Y)p")

    option: Mapped["SymbolOption"] = relationship(
        "SymbolOption", back_populates="symbol_option_t"
    )

    def __str__(self):
        return (
            f"{self.option.underlying.name}.{self.option.underlying.symbol[:4]}"
            f"-{self.option.price_strike}-{self.option.delisted_date.year}"
            f"{self.option.delisted_date.month}"
        )


class SymbolDiscount(Base, BaseModel):
    """贴水配置（原 irs.SymbolDiscount）。无外键。"""

    __tablename__ = "irs_symbol_discount"

    # 期权主力标记常量（保留原 Django 类属性）
    OPTION_MAIN = True    # 是
    OPTION_MINOR = False  # 否

    symbol_con: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, comment="连续合约")
    symbol: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="真实合约")
    is_main: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=OPTION_MINOR, comment="主力"
    )
    symbol_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="合约类别")
    symbol_ud: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="标的代码")
    delisted_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="到期日")

    # ---- 保留的自定义类属性 ----
    cols_map_fields = {
        "symbol": ["代码"],
        "symbol_ud": ["underlying_symbol"],
        "delisted_date": ["delisted_date"],
    }
    unique_keys = ["symbol_con"]
    update_is_main = ["is_main"]
    fields_yiels = ["id", "symbol", "symbol_ud", "delisted_date"]

    # 反向关系：一个 MonitorDiscount 指向本贴水配置
    symbol_real_monitor: Mapped["MonitorDiscount"] = relationship(
        "MonitorDiscount", uselist=False, back_populates="symbol_real"
    )

    def __str__(self):
        return f"{self.symbol_con}({self.symbol})"


class MonitorDiscount(Base, BaseModel):
    """贴水监测（原 irs.MonitorDiscount）。OneToOne -> SymbolDiscount。"""

    __tablename__ = "irs_monitor_discount"

    # 外键列名 symbol_real_id（Django db_column 显式指定）
    symbol_real_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("irs_symbol_discount.id"), unique=True, nullable=False, comment="估值标的"
    )
    days_left: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="剩余天数")
    position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="持仓量")
    price: Mapped[Decimal] = mapped_column(Numeric(9, 2), nullable=False, comment="合约现价")
    price_ud: Mapped[Decimal] = mapped_column(Numeric(9, 2), nullable=False, comment="基础现价")
    discount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="贴水")
    ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="贴水率(%)")
    ratio_y: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 2), nullable=True, comment="贴水率(%Y)")

    # ---- 保留的自定义类属性 ----
    cols_map_fields = {
        "symbol": ["代码"],
        "price": ["最新"],
        "position": ["cum_position"],
    }
    unique_keys = ["symbol_real_id"]
    fields_update_sql = [
        "position", "price", "price_ud",
        "days_left", "discount", "ratio", "ratio_y",
    ]

    symbol_real: Mapped["SymbolDiscount"] = relationship(
        "SymbolDiscount", back_populates="symbol_real_monitor"
    )

    def __str__(self):
        return f"{self.symbol_real.symbol}({self.symbol_real.symbol_con})"


# =========================================================================
# 事件钩子：替代原 Django save() 中的自动计算逻辑
# =========================================================================


@_on_insert_update(SymbolValue)
def _compute_symbol_value(mapper, connection, target):
    """SymbolValue 计算逻辑（原 save()）：
    - pp_m = (pp_l + pp_h) / 2
    - bg_p_bid1 = pp_l
    - bg_p_bid2 = bg_p_bid1 * (0.1 - vix)
    - bg_p_bid3 = bg_p_bid2 * (0.1 - vix)
    - v2/v3 为分批买入量，m_init/m_tot 为对应金额(万)
    """
    target.pp_m = (target.pp_l + target.pp_h) * Decimal("0.5")
    target.bg_p_bid1 = target.pp_l
    target.bg_p_bid2 = target.bg_p_bid1 * (Decimal("0.1") - target.vix)
    target.bg_p_bid3 = target.bg_p_bid2 * (Decimal("0.1") - target.vix)
    # v2：若 p_init*2+p_inc > p_total 则取差额，否则取 p_init+p_inc
    v2 = (
        (target.p_total - target.p_init)
        if target.p_init * 2 + target.p_inc - target.p_total > 0
        else target.p_init + target.p_inc
    )
    # v3：剩余量，不能为负
    v3 = (
        (target.p_total - target.p_init - v2)
        if target.p_total - target.p_init - v2 > 0
        else Decimal("0")
    )
    # 首笔金额(万) = p_init * bg_p_bid1 * 0.0001
    target.m_init = target.p_init * target.bg_p_bid1 * Decimal("0.0001")
    # 总金额(万) = (m_init + v2*bg_p_bid2 + v3*bg_p_bid3) * 0.0001
    target.m_tot = (
        target.m_init + v2 * target.bg_p_bid2 + v3 * target.bg_p_bid3
    ) * Decimal("0.0001")


@_on_insert_update(SymbolKpi)
def _compute_symbol_kpi(mapper, connection, target):
    """SymbolKpi 计算逻辑（原 save()）：基于关联 SymbolValue 的价格计算涨跌幅(%)。
    - last_ratio = (last_close - py_close) / py_close * 100
    - max_ratio  = (y_high  - py_close) / py_close * 100
    - min_ratio  = (y_low   - py_close) / py_close * 100
    py_close 为 0 时所有 ratio 置 0，避免除零。
    """
    sv = target.symbol_value
    if sv.py_close:
        target.last_ratio = (
            (sv.last_close - sv.py_close) / sv.py_close * Decimal("100")
        )
        target.max_ratio = (
            (sv.y_high - sv.py_close) / sv.py_close * Decimal("100")
        )
        target.min_ratio = (
            (sv.y_low - sv.py_close) / sv.py_close * Decimal("100")
        )
    else:
        target.last_ratio = Decimal("0")
        target.max_ratio = Decimal("0")
        target.min_ratio = Decimal("0")


@_on_insert_update(MonitorValue)
def _compute_monitor_value(mapper, connection, target):
    """MonitorValue 计算逻辑（原 save()）：基于关联 SymbolValue 和最新价计算估值收益率(%)。
    仅当 price 非空时执行计算。
    """
    if not target.price:
        return
    sv = target.symbol_value
    # 估值收益率 = (估值价/最新价 - 1) * 100
    target.pv_el = (sv.pp_el / target.price - Decimal("1")) * Decimal("100")
    target.pv_l = (sv.pp_l / target.price - Decimal("1")) * Decimal("100")
    target.pv_m = (sv.pp_m / target.price - Decimal("1")) * Decimal("100")
    target.pv_h = (sv.pp_h / target.price - Decimal("1")) * Decimal("100")
    target.pv_eh = (sv.pp_eh / target.price - Decimal("1")) * Decimal("100")
    # 相对上年末收益率 = (估值价/上年末 - 1) * 100
    target.pv_el_y = (sv.pp_el / sv.py_close - Decimal("1")) * Decimal("100")
    target.pv_l_y = (sv.pp_l / sv.py_close - Decimal("1")) * Decimal("100")
    target.pv_m_y = (sv.pp_m / sv.py_close - Decimal("1")) * Decimal("100")
    target.pv_h_y = (sv.pp_h / sv.py_close - Decimal("1")) * Decimal("100")
    target.pv_eh_y = (sv.pp_eh / sv.py_close - Decimal("1")) * Decimal("100")
    # 买点回测率 = (目标价/最新价 - 1) * 100
    target.bg_d_bid1 = (sv.bg_p_bid1 / target.price - Decimal("1")) * Decimal("100")
    target.bg_d_bid2 = (sv.bg_p_bid2 / target.price - Decimal("1")) * Decimal("100")
    target.bg_d_bid3 = (sv.bg_p_bid3 / target.price - Decimal("1")) * Decimal("100")
    # 阶段高点更新：若近期高点低于最新价，则刷新为最新价
    if target.rh < target.price:
        target.rh = target.price
    # 回撤目标 = 近期高 * (1 - vix)
    target.hd_target = target.rh * (Decimal("1") - sv.vix)
    # 回撤值 = 最新价 - 目标价
    target.hd_diff = target.price - target.hd_target
    # 回撤率 = (目标价/最新价 - 1) * 100
    target.hd_ratio = (target.hd_target / target.price - Decimal("1")) * Decimal("100")


@_on_insert_update(SymbolOption)
def _compute_symbol_option(mapper, connection, target):
    """SymbolOption 计算逻辑（原 save()）：
    - days_left = (delisted_date - today).days
    - value_per = price_strike * underlying.multiplier （单点价值）
    """
    target.days_left = (target.delisted_date - date.today()).days
    target.value_per = target.price_strike * target.underlying.multiplier


@_on_insert_update(MonitorOption)
def _compute_monitor_option(mapper, connection, target):
    """MonitorOption 计算逻辑（原 save()）：基于关联 SymbolOption 计算期权指标。
    - atm_i  = (行权价/标的现价 - 1) * 100  （平值度）
    - value_i = 内在价值（认购/认沽方向不同）
    - value_t = 时间价值 = 现价 - 内在价值
    - ratio_t / ratio_i = 时间/内在价值占行权价百分比
    - ratio_t_y / ratio_i_y = 年化时间/内在百分比（按 365/days_left）
    """
    opt = target.option
    # 平值度(%)
    target.atm_i = (opt.price_strike / target.price_ud - Decimal("1")) * Decimal("100")
    # 内在价值：认购/认沽方向不同
    if target.option_type == MonitorOption.OPTION_TYPE_CALL:
        # 认购：行权价 > 标的现价时内在为 0，否则 = 标的现价 - 行权价
        if opt.price_strike > target.price_ud:
            target.value_i = Decimal("0")
        else:
            target.value_i = target.price_ud - opt.price_strike
    else:
        # 认沽：行权价 < 标的现价时内在为 0，否则 = 行权价 - 标的现价
        if opt.price_strike < target.price_ud:
            target.value_i = Decimal("0")
        else:
            target.value_i = opt.price_strike - target.price_ud
    # 时间价值 = 现价 - 内在价值
    target.value_t = target.price - target.value_i
    # 时间/内在价值占行权价百分比
    target.ratio_t = (target.value_t / opt.price_strike) * Decimal("100")
    target.ratio_i = (target.value_i / opt.price_strike) * Decimal("100")
    # 年化百分比（剩余天数为 0 时置 0，避免除零）
    if opt.days_left != 0:
        target.ratio_t_y = target.ratio_t * Decimal("365") / opt.days_left
        target.ratio_i_y = target.ratio_i * Decimal("365") / opt.days_left
    else:
        target.ratio_t_y = Decimal("0")
        target.ratio_i_y = Decimal("0")


@_on_insert_update(SymbolDiscount)
def _compute_symbol_discount(mapper, connection, target):
    """SymbolDiscount 计算逻辑（原 save()）：从 symbol_con 解析合约类别。
    例：'IF2306.CCFX' -> 'IF.23'
    """
    parts = target.symbol_con.split(".")
    target.symbol_type = f"{parts[0]}.{parts[1][:2]}"


@_on_insert_update(MonitorDiscount)
def _compute_monitor_discount(mapper, connection, target):
    """MonitorDiscount 计算逻辑（原 save()）：基于关联 SymbolDiscount 计算贴水。
    - days_left = (delisted_date - today).days
    - discount  = price_ud - price  （基础现价 - 合约现价）
    - ratio     = discount / price * 100
    - ratio_y   = ratio * 365 / days_left （年化贴水率，days_left=0 时置 0）
    """
    target.days_left = (target.symbol_real.delisted_date - date.today()).days
    target.discount = target.price_ud - target.price
    target.ratio = (target.discount / target.price) * Decimal("100")
    if target.days_left != 0:
        target.ratio_y = target.ratio * Decimal("365") / target.days_left
    else:
        target.ratio_y = Decimal("0")
