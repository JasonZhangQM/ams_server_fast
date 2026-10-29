# -*- coding: utf-8 -*-
"""irs 应用 SQLAlchemy 2.0 模型定义。

- 2 个 ORM 模型继承 (Base, BaseModel)：DiscountMonitor、ValueMonitor
  （期权监测 OptionMonitor 已于 2026-10-29 下线，行情统一走 bds_option_market）
- 衍生字段计算逻辑通过 SQLAlchemy before_insert / before_update 事件钩子实现
- 保留所有自定义类属性（cols_map_fields / unique_keys / fields_request 等）
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    BigInteger,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from server_fast.common.db import Base
from server_fast.common.models import BaseModel


def _on_insert_update(model):
    """装饰器：把函数同时注册为 model 的 before_insert + before_update 钩子。

    在 flush 前对 target 实例赋值计算字段。
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
    # 标的层属性（v3 新增）：主题的**唯一定义处**（合约层 theme 已删）
    theme: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="主题（黄金/中国宽基/美国宽基/银行/家电…）")
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


class DiscountMonitor(Base, BaseModel):
    """贴水监测（合并原 SymbolDiscount 配置 + MonitorDiscount 监测为单表）。

    表名 irs_discount_monitor，兼具配置与监测功能：
    - 配置字段：symbol_con/symbol/is_main/symbol_type/symbol_ud/delisted_date
    - 监测字段：days_left/position/price/price_ud/discount/ratio/ratio_y
    - price/price_ud 由原 MonitorDiscount 的 NotNull 改为 nullable=True（同步行情前可能无值）
    """

    __tablename__ = "irs_discount_monitor"

    # 期权主力标记常量
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
# 事件钩子：flush 前自动计算衍生字段
# =========================================================================


@_on_insert_update(ValueMonitor)
def _compute_value_monitor(mapper, connection, target):
    """ValueMonitor 计算逻辑：基于本表 price/py_close/y_high/y_low/pp_* 计算监测指标(%)。

    ⚠️ price 为空时**必须清空** pv_*，不能 return 了事（v3 §5.3）：
    否则改档位/改上游后 pv_* 会保留上一版算出的旧值，页面上看起来像"按新档位算的"，
    实际是旧偏离冒充新值。清空至少让"算不出来"显示为 null，而不是一个假的数。
    """
    if not target.price:
        for f in ("pv_yh", "pv_yl", "pv_yy",
                  "pv_el", "pv_l", "pv_m", "pv_h", "pv_eh",
                  "pv_el_y", "pv_l_y", "pv_m_y", "pv_h_y", "pv_eh_y"):
            setattr(target, f, None)
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


@_on_insert_update(DiscountMonitor)
def _compute_discount_monitor(mapper, connection, target):
    """DiscountMonitor 计算逻辑：
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


# =========================================================================
# v3 新增：标的层档案（档位版本 / 研究定稿 / 逻辑 / 证伪 / 剧本）
#
# 外键一律 ON DELETE RESTRICT：主表历史上被删过约 47 次（AUTO_INCREMENT=56 只有 8 行），
# 用 CASCADE 会让"随手删一行"变成"静默清空该标的的全部研究与档位档案"。
# =========================================================================


class VmVersion(Base, BaseModel):
    """档位版本：某日档位 = effective_date <= D 的最大版（生效区间语义，不是每日快照）。"""

    __tablename__ = "irs_vm_version"
    __table_args__ = (
        UniqueConstraint("symbol", "effective_date", name="uk_vmv_symbol_date"),
        Index("k_vmv_date", "effective_date"),
    )

    symbol: Mapped[str] = mapped_column(
        String(32), ForeignKey("irs_value_monitor.symbol", ondelete="RESTRICT"),
        nullable=False, comment="标的代码")
    effective_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="生效日期（自该日起适用，含当日）")
    pp_el: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="极低")
    pp_l: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="低")
    pp_m: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="中")
    pp_h: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="高")
    pp_eh: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="极高")
    note: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="调整理由（一句话）")

    def __str__(self):
        return f"{self.symbol}@{self.effective_date}"


class VmResearch(Base, BaseModel):
    """标的研究定稿（1:N，按 research_date 留版）。

    选新日期 = 新建，选历史日期 = 修改（与档位版本同构）。
    research_date 是"我哪天定了这个结论"，不是报告里的数据截止日。
    """

    __tablename__ = "irs_vm_research"
    __table_args__ = (
        UniqueConstraint("symbol", "research_date", name="uk_vmr_symbol_date"),
        Index("k_vmr_date", "research_date"),
    )

    CONCLUSION_CHOICES = ["买入", "增持", "持有", "观望", "回避", "不入池"]

    symbol: Mapped[str] = mapped_column(
        String(32), ForeignKey("irs_value_monitor.symbol", ondelete="RESTRICT"),
        nullable=False, comment="标的代码")
    research_date: Mapped[date] = mapped_column(Date, nullable=False, comment="研究定稿日")
    thesis: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="一句话本质：买它本质上在买什么")
    conclusion: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="结论")
    max_risk: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="最大风险：最可能让我亏钱的那件事")
    expectation_gap: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="预期差：市场已定价什么 / 我认为未定价什么")
    pre_condition: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="前置条件（组合约束，与标的质地分开写）")
    next_review: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="下次复核日期")
    research_note: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, comment="知识库研究报告名（双链名，不含 .md）")
    research_brief: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="结论摘要（<=800 字，禁止抄报告正文）")
    research_log: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="研究过程：怎么得出结论 / 换过什么锚 / 推翻过什么")

    unique_keys = ["symbol", "research_date"]

    def __str__(self):
        return f"{self.symbol}@{self.research_date}({self.conclusion})"


class VmLogic(Base, BaseModel):
    """投资逻辑条目（2-4 条，带 完好/减弱/破坏 状态）。

    单独成表而不是塞进一段 TEXT：state 要被查询——"列出全部 state='破坏' 的逻辑"
    是复盘第一步，文本做不到，一条 SQL 就行。
    """

    __tablename__ = "irs_vm_logic"
    __table_args__ = (
        UniqueConstraint("symbol", "seq", name="uk_vml_symbol_seq"),
        Index("k_vml_state", "state"),
    )

    STATE_CHOICES = ["完好", "减弱", "破坏"]

    symbol: Mapped[str] = mapped_column(
        String(32), ForeignKey("irs_value_monitor.symbol", ondelete="RESTRICT"),
        nullable=False, comment="标的代码")
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="序号 1-4")
    content: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="逻辑：逻辑->影响路径->是否已定价")
    pricing: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True, comment="是否已被定价：未定价/部分/已充分")
    state: Mapped[str] = mapped_column(String(8), nullable=False, default="完好", comment="状态")
    state_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="状态最后变更时间（state 真变时由 service 写）")
    evidence: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="证据（报告章节 / 数据代码 / 指标）")

    unique_keys = ["symbol", "seq"]

    def __str__(self):
        return f"{self.symbol}#{self.seq}({self.state})"


class VmInvalidate(Base, BaseModel):
    """证伪条件登记：只登记判据与触发状态，**不自动计算**。"""

    __tablename__ = "irs_vm_invalidate"
    __table_args__ = (
        UniqueConstraint("symbol", "seq", name="uk_vmi_symbol_seq"),
        Index("k_vmi_status", "status"),
    )

    STATUS_CHOICES = ["未触发", "已触发", "已失效"]

    symbol: Mapped[str] = mapped_column(
        String(32), ForeignKey("irs_value_monitor.symbol", ondelete="RESTRICT"),
        nullable=False, comment="标的代码")
    logic_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="对应逻辑条目")
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="序号")
    proposition: Mapped[str] = mapped_column(String(200), nullable=False, comment="被证伪的命题")
    signal: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="证伪信号 + 阈值（必须可量化）")
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="数据源")
    action: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="触发后动作：减仓/清仓/重估/暂停加仓")
    judge: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True, comment="判定类别：price/percentile/discount/manual")
    judge_expr: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="判据表达式（如 price<=3900）")
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="未触发", comment="状态")
    triggered_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="触发日")

    unique_keys = ["symbol", "seq"]

    def __str__(self):
        return f"{self.symbol}#{self.seq}({self.status})"


class VmPlan(Base, BaseModel):
    """剧本 —— "到什么价、做什么"的唯一载体（必须写真实代码和点位）。

    剧本长期有效、可反复执行；执行次数与盈亏归因走 `bills_bill_note.plan_id`，
    本表不记「做没做」。状态**不落库**、派生：暂停 / 生效。
    trigger_price 与 trigger_ref 二选一：后者引用档位（pp_el/pp_l/pp_m/pp_h/pp_eh），
    改档位后剧本自动跟随，不用手改 8 条剧本。
    """

    __tablename__ = "irs_vm_plan"
    __table_args__ = (
        UniqueConstraint("vm_symbol", "seq", name="uk_vmp_symbol_seq"),
        Index("k_vmp_trade", "trade_symbol"),
    )

    TRIGGER_REFS = ["pp_el", "pp_l", "pp_m", "pp_h", "pp_eh"]

    vm_symbol: Mapped[str] = mapped_column(
        String(32), ForeignKey("irs_value_monitor.symbol", ondelete="RESTRICT"),
        nullable=False, comment="标的代码")
    trade_symbol: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="交易代码（真实合约）")
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="序号")
    b_s: Mapped[str] = mapped_column(String(4), nullable=False, comment="买/卖")
    o_c: Mapped[Optional[str]] = mapped_column(String(4), nullable=True, comment="开/平")
    trigger_op: Mapped[str] = mapped_column(
        String(8), nullable=False, default="lte", comment="lte(<=)/gte(>=)/range(区间)")
    trigger_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="触发点位（与 trigger_ref 二选一）")
    trigger_ref: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True, comment="触发价引用档位：pp_el/pp_l/pp_m/pp_h/pp_eh")
    trigger_price2: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="区间上沿（trigger_op=range 时必填）")
    qty: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="每次执行手数（空=到价再定量）")
    reason: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="为什么这么做（对应哪条逻辑）")
    is_cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="手动作废")

    unique_keys = ["vm_symbol", "seq"]

    def __str__(self):
        return f"{self.vm_symbol}#{self.seq}({self.b_s}{self.o_c or ''})"


class VmReview(Base, BaseModel):
    """标的复盘 —— 锚定「研究版本」，同一版本可多次复盘。

    与「投研」同层：研究是事前假设，复盘是事后成绩单。
    一次复盘回答「这版研究的结论兑现了没有」，因此必须挂到具体版本；
    同一版本可反复复盘（持仓中、平仓后、季度末），故不设唯一键。
    执行合规（有没有按剧本做）由机器判定，不在这里写；本表只记判断。
    盈亏归因走 `bills_bill_note.plan_id`，本表不参与计算。
    """

    __tablename__ = "irs_vm_review"
    __table_args__ = (
        Index("k_vmr_symbol_date", "symbol", "review_date"),
        Index("k_vmr_research_date", "research_id", "review_date"),
        {"comment": "标的复盘（锚定研究版本）"},
    )

    symbol: Mapped[str] = mapped_column(
        String(32), ForeignKey("irs_value_monitor.symbol", ondelete="CASCADE"),
        nullable=False, comment="标的代码")
    research_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("irs_vm_research.id", ondelete="CASCADE"),
        nullable=False, comment="被复盘的研究版本 irs_vm_research.id")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="复盘正文")
    review_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="落笔日期（后端填，前端不可传）")

    def __str__(self):
        return f"{self.symbol}复盘(research={self.research_id})"


class VmStateLog(Base, BaseModel):
    """逻辑 / 证伪状态变更审计日志（append-only，复盘用）。

    主表（VmLogic.state / VmInvalidate.status）仍只存当前态，本表只增不改：
    每一次 state/status **真正变化**时追加一行，旧值不覆盖，「三个月前判过什么」可回放。
    """

    __tablename__ = "irs_vm_state_log"
    __table_args__ = (
        Index("idx_subject", "subject_type", "subject_id", "changed_at"),
    )

    LOGIC = "logic"
    INVALIDATE = "invalidate"

    subject_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="logic=逻辑；invalidate=证伪")
    subject_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="对应 irs_vm_logic.id / irs_vm_invalidate.id")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标的代码")
    from_state: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True, comment="变更前（首次登记为 NULL）")
    to_state: Mapped[str] = mapped_column(String(16), nullable=False, comment="变更后")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="变更时间")
    note: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="变更理由/证据（可选）")

    def __str__(self):
        return f"{self.subject_type}#{self.subject_id}:{self.from_state}->{self.to_state}"


class VmTarget(Base, BaseModel):
    """标的级目标仓位（决策层）：目标占总资产比例；漂移 = 当前 − 目标在前端派生。"""

    __tablename__ = "irs_vm_target"
    __table_args__ = (
        UniqueConstraint("symbol", name="uk_target_symbol"),
    )

    symbol: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="标的（vm_symbol）")
    target_pct: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True, comment="目标仓位占总资产比例（%）")
    note: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="目标仓位说明")

    unique_keys = ["symbol"]

    def __str__(self):
        return f"{self.symbol}@{self.target_pct}%"
