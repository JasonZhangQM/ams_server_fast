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
