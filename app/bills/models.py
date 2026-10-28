# -*- coding: utf-8 -*-
"""bills 应用的 SQLAlchemy 2.0 模型定义。

共 9 个模型：Bill / Profit / Group / GroupAcc / GroupSymbol / BillNote / ProfitYear /
RiskParam / ScenarioGrid。

- 继承 (Base, BaseModel)，BaseModel 提供 id/create_time/update_time 及通用方法
- 外键列名以 _id 结尾
- unique_together 与同名唯一索引合并为 UniqueConstraint
- 笔记（logic/plan/invalidate/review/theme）v3 起已从 GroupSymbol 整体迁出：
  标的层档案 -> irs_vm_*，单笔成交 -> BillNote。GroupSymbol 只留 vm_symbol 一个手动列。

2026-10-26：原 `OptionContract` 已两次迁移 —— 先由 `bills_option_contract` 迁至
`app/bds/models.py`（改名为 `bds_option_contract`），当日又**并入 `bds_option_config`**
（三表变两表）。理由：合约规格是市场事实、与账单格式无关，且它的 13 个字段里只有
`margin_type` 不可被行情表或品种表替代，不值得单独一张表。
本模块如需读规格，用 `from server_fast.app.bds.models import OptionConfig`，
`broker_symbol` 才是账单口径的代码（`symbol` 是交易所标准码）。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server_fast.common.db import Base
from server_fast.common.models import BaseModel


class Bill(Base, BaseModel):
    """交易收益表。"""

    __tablename__ = "bills_bill"

    # 基本信息
    trade_time: Mapped[datetime] = mapped_column(DateTime, comment="交易时间")
    symbol: Mapped[str] = mapped_column(String(32), comment="代码")
    name: Mapped[str] = mapped_column(String(32), comment="名称")
    exec_type: Mapped[str] = mapped_column(String(16), comment="操作类型")
    category1: Mapped[str] = mapped_column(String(16), comment="一级分类")
    category: Mapped[str] = mapped_column(String(16), comment="总分类")
    b_s: Mapped[Optional[str]] = mapped_column(String(4), comment="买/卖")
    c_p: Mapped[Optional[str]] = mapped_column(String(4), comment="沽/购")
    o_c: Mapped[Optional[str]] = mapped_column(String(4), comment="开/平")

    # 成交数据
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), comment="成交价")
    vol: Mapped[int] = mapped_column(Integer, comment="成交量")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), comment="成交额")
    amount_act: Mapped[Decimal] = mapped_column(Numeric(12, 2), comment="发生额")
    balance: Mapped[Optional[int]] = mapped_column(Integer, comment="余额")

    # 费用明细
    fee_tax: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="税费合计")
    fees: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="手续费/佣金")
    taxes: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="印花税")
    fee_exec1: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="过户费")
    fee_exec2: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="交易过户费")
    fee_exec3: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="行权过户费")
    fee_hand: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="经手费")
    fee_sr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="证管费")
    fee_clear: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="清算费")
    fee_reg: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="交易规费")
    fee_other: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="其他费用")
    premium: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="权利金收支")
    cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="资金余额")

    # 编号与账户信息（account_id 为股东账号 CharField，非外键）
    id_exec: Mapped[Optional[str]] = mapped_column(String(16), comment="成交编号")
    id_agree: Mapped[Optional[str]] = mapped_column(String(16), comment="流水号")
    currency: Mapped[str] = mapped_column(String(8), comment="币种")
    account_id: Mapped[Optional[str]] = mapped_column(String(16), comment="账号")
    market: Mapped[Optional[str]] = mapped_column(String(16), comment="市场")
    account: Mapped[str] = mapped_column(String(16), comment="账户")

    # ---- 自定义类属性 ----
    unique_keys = [
        'account', 'trade_time', 'symbol', 'exec_type', 'amount_act', 'id_agree', 'cash',
    ]
    cols_map_fields = {
        'trade_time': ['交易时间', '成交日期', '清算日期', '交易日期'],
        'symbol': ['证券代码', '合约编码', '合约', '期权合约编码'],
        'name': ['证券名称', '合约简称', '品种', '期权合约简称'],
        'exec_type': ['交易类别', '操作', '投/保', '业务标识'],
        'b_s': ['买卖', '买/卖', '买卖方向'],
        'c_p': ['类别'],
        'o_c': ['开平', '开平仓方向'],
        'price': ['成交均价', '成交价格', '成交价'],
        'vol': ['成交数量', '手数', '发生数量'],
        'amount': ['成交金额', '成交额'],
        'amount_act': ['发生金额', '清算金额', '业务金额'],
        'balance': ['股票余额', '余额', '股份余额', '后证券额'],
        'fees': ['手续费', '佣金', '净佣金'],
        'taxes': ['印花税'],
        'fee_exec1': ['过户费'],
        'fee_exec2': ['交易过户费'],
        'fee_exec3': ['行权过户费'],
        'fee_hand': ['经手费', '一级经手费'],
        'fee_sr': ['证管费'],
        'fee_clear': ['清算费', '结算费'],
        'fee_reg': ['交易规费'],
        'fee_other': ['其他费用'],
        'premium': ['权利金收支'],
        'cash': ['资金余额', '本次金额', '后资金额', '当前余额'],
        'id_exec': ['成交编号'],
        'id_agree': ['流水号', '合同编号', '成交序号'],
        'currency': ['币种', '货币单位'],
        'account_id': ['股东账号', '资金账号', '资金账户', '股东帐户'],
        'market': ['交易市场', '交易所', '市场'],
    }
    fields_fee = [  # 费用字段
        'fees', 'taxes', 'fee_exec1', 'fee_exec2', 'fee_exec3', 'fee_hand', 'fee_sr', 'fee_clear',
        'fee_reg', 'fee_other',
    ]
    fields_pl = [  # 收益试算所需字段
        'id', 'account', 'symbol', 'trade_time', 'category1', 'category', 'b_s', 'c_p', 'o_c',
        'vol', 'amount', 'amount_act', 'fee_tax',
    ]

    __table_args__ = (
        # 唯一约束
        UniqueConstraint(
            'account', 'trade_time', 'symbol', 'exec_type', 'amount_act', 'id_agree', 'cash',
            name='uk_bills_bill',
        ),
        # 普通索引
        Index('k_bills_bill_symbol', 'symbol'),
    )

    def __str__(self) -> str:
        trade_str = self.trade_time.strftime('%Y-%m-%d %H:%M:%S') if self.trade_time else ''
        return f"{self.account}-{self.symbol}-{trade_str}"


class Profit(Base, BaseModel):
    """账单收益模型。

    通过 bill_id 外键列 + relationship 与 Bill 一对一关联。
    """

    __tablename__ = "bills_profit"

    # OneToOne 关联 Bill：外键列 bill_id + unique 体现一对一
    # 外键列类型须与被引用的 bills_bill.id（BigInteger）一致，否则 MySQL 建表报 errno 3780
    bill_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey('bills_bill.id', ondelete='CASCADE'),
        unique=True,
        comment="关联账单",
    )
    # ORM 关联对象（可选，便于联表查询）
    bill: Mapped["Bill"] = relationship("Bill")

    # 持仓与成本
    p_long: Mapped[Optional[int]] = mapped_column(Integer, comment="多头持仓")
    p_short: Mapped[Optional[int]] = mapped_column(Integer, comment="空头持仓")
    cost_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="多头成本")
    cost_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="空头成本")
    cost_u_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="u成本l")
    cost_u_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="u成本s")

    # 盈亏与差额
    pl_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏l")
    pl_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏s")
    pl_other: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="其他损益")
    pl_ft: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="税费")
    pl_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资利息")
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资余额")
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="入金净额")
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="划转净额")

    # ---- 保留的自定义类属性 ----
    fields_pl_update = [  # 收益试算追加字段
        'p_long', 'p_short',
        'cost_t_long', 'cost_t_short',
        'cost_u_long', 'cost_u_short',
        'pl_long', 'pl_short',
        'pl_other', 'pl_br',
        'pl_ft',
        'diff_br', 'diff_dw', 'diff_dwt',
    ]
    fields_daily_latest = [  # 收益日结最后字段
        'p_long', 'p_short',
        'cost_t_long', 'cost_t_short',
        'cost_u_long', 'cost_u_short',
    ]

    __table_args__ = (
        Index('idx_bills_profit_bill_id', 'bill_id'),
    )

    def __str__(self) -> str:
        return f'账单收益-{self.bill_id or self.id}'


class Group(Base, BaseModel):
    """账单汇总模型。"""

    __tablename__ = "bills_group"

    # 标识与时间
    account: Mapped[Optional[str]] = mapped_column(String(16), comment="账户")
    category: Mapped[str] = mapped_column(String(16), comment="交易分类")
    symbol: Mapped[str] = mapped_column(String(32), comment="代码")
    start_time: Mapped[datetime] = mapped_column(DateTime, comment="首次交易时")
    end_time: Mapped[datetime] = mapped_column(DateTime, comment="最后交易时")
    count: Mapped[int] = mapped_column(Integer, comment="交易次数")
    profit_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="收益试算时间")
    value_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="市值更新时间")

    # 持仓
    p_long: Mapped[Optional[int]] = mapped_column(Integer, comment="多头持仓")
    p_short: Mapped[Optional[int]] = mapped_column(Integer, comment="空头持仓")
    p_total: Mapped[Optional[int]] = mapped_column(Integer, comment="持仓")

    # 成本
    cost_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="多头成本")
    cost_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="空头成本")
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="持仓成本")

    # 市值与浮盈
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="最新价")
    multiplier: Mapped[Optional[int]] = mapped_column(Integer, comment="乘数")
    value_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="多头市值")
    value_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="空头市值")
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="市值")
    pf_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="多头浮盈")
    pf_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="空头浮盈")
    pf_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="浮动盈亏")

    # 平仓盈亏
    pl_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="多头平仓盈亏")
    pl_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="空头平仓盈亏")
    pl_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏")
    pl_t_other: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="其他损益")
    pl_t_ft: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="税费")
    pl_t_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资利息")

    # 差额
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资余额")
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="出入净额")
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="划转净额")

    # ---- 保留的自定义类属性 ----
    unique_keys = ['account', 'category', 'symbol']
    fields_pl = [
        'id', 'account', 'category', 'symbol',
        'start_time', 'end_time', 'count', 'profit_time',
    ]
    fields_pl_update = [  # 收益试算追加字段
        'profit_time',
        'p_long', 'p_short', 'p_total',
        'cost_t_long', 'cost_t_short', 'cost_total',
        'pl_t_long', 'pl_t_short', 'pl_total',
        'pl_t_other',
        'pl_t_ft',
        'pl_t_br',
        'diff_br', 'diff_dw', 'diff_dwt',
    ]
    fields_cash_update = [  # 资金试算追加字段
        'profit_time',
        'cost_t_long',
        'cost_total',
    ]
    fields_f = [  # 市值试算所需字段
        'id', 'account', 'category', 'symbol',
        'start_time', 'end_time', 'count',
        'value_time',
        'p_long', 'p_short',
        'cost_t_long', 'cost_t_short',
    ]
    fields_f_update = [  # 市值试算追加字段
        'value_time',
        'price', 'multiplier',
        'value_long', 'value_short', 'value_total',
        'pf_long', 'pf_short', 'pf_total',
    ]
    fields_api_details = [  # 详情字段
        'account', 'category', 'symbol',
        'p_total', 'cost_total', 'value_total',
        'pf_total', 'pl_total', 'pl_t_other', 'pl_t_br',
        'diff_br', 'diff_dw', 'diff_dwt',
    ]

    # unique_together + 同名唯一索引合并为 UniqueConstraint
    __table_args__ = (
        UniqueConstraint('account', 'category', 'symbol', name='uk_bills_group'),
        Index('idx_bills_group_account', 'account'),
        Index('idx_bills_group_category', 'category'),
        Index('idx_bills_group_symbol', 'symbol'),
        Index('idx_bills_group_time_range', 'start_time', 'end_time'),
    )

    def __str__(self) -> str:
        account = self.account or '未知账户'
        return f'账单汇总-{account}-{self.category}-{self.symbol}'


class GroupAcc(BaseModel, Base):
    """账户汇总模型。"""

    __tablename__ = "bills_group_acc"

    account: Mapped[str] = mapped_column(String(16), unique=True, comment="账户")
    cash_acc: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="资金余额")
    fm_acc: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="理财余额")
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="证券成本")
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="证券市值")
    acc_aset: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="账户价值")
    pf_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="浮动盈亏")
    pl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏")
    pfl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="盈亏合计")
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资余额")
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="入金净额")
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="划转净额")
    status: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="校验")

    # ---- 保留的自定义类属性（原样保留，即使引用了其他模型字段）----
    fields_update = [
        'account', 'category', 'symbol',
        'p_total', 'cost_total', 'value_total',
        'pf_total', 'pl_total', 'pl_t_other', 'pl_t_br',
        'diff_br', 'diff_dw', 'diff_dwt', 'status',
    ]

    def __str__(self) -> str:
        return f'{self.account}'


class GroupSymbol(Base, BaseModel):
    """标的汇总模型。"""

    __tablename__ = "bills_group_symbol"

    category: Mapped[str] = mapped_column(String(16), comment="交易分类")
    symbol: Mapped[str] = mapped_column(String(32), comment="代码")
    count: Mapped[int] = mapped_column(Integer, comment="交易次数")
    p_total: Mapped[Optional[int]] = mapped_column(Integer, comment="持仓量")
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="持仓成本")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="最新价")
    multiplier: Mapped[Optional[int]] = mapped_column(Integer, comment="乘数")
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="当前市值")
    pf_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="浮动盈亏")
    pl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏")
    pfl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="盈亏合计")
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资余额")
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="出入净额")
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="划转净额")

    # ---- v3 归属标的（唯一的手动列；不进 _DERIVED_COLS 白名单，重算不会覆盖）----
    # 融资融券行（symbol='RZRQ'）恒为 NULL：融资只是一个融资行为，name 不承担归属含义
    vm_symbol: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("irs_value_monitor.symbol", ondelete="RESTRICT"),
        comment="归属标的（irs_value_monitor.symbol）；融资融券与未归属为 NULL")

    # ---- 保留的自定义类属性 ----
    unique_keys = ['category', 'symbol']

    # unique_together + 同名唯一索引合并为 UniqueConstraint
    __table_args__ = (
        UniqueConstraint('category', 'symbol', name='uk_bills_group_symbol'),
    )

    def __str__(self) -> str:
        return f'标的汇总-{self.category}-{self.symbol}'


class BillNote(Base, BaseModel):
    """成交笔记（1:N，挂 bills_bill.id）。

    - kind 只有「想法 / 复盘」，同 kind 只有一份（UK bill_id+kind）；
    - ON DELETE SET NULL + biz_key：既有"删账户末笔 -> 重导"会换新 id，
      CASCADE 会让笔记静默蒸发，SET NULL 留孤儿等 relink 找回；
    - content_at 一律后端写，前端不可传（否则"当时写的"可以伪造）。
    """

    __tablename__ = "bills_bill_note"

    KIND_IDEA = "想法"
    KIND_REVIEW = "复盘"

    bill_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("bills_bill.id", ondelete="SET NULL"),
        nullable=True, comment="关联 bills_bill.id；成交被删时置 NULL，靠 biz_key 找回")
    biz_key: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="成交自然键 md5（与 uk_bills_bill 同口径）")
    kind: Mapped[str] = mapped_column(String(8), nullable=False, comment="类型：想法/复盘")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="正文")
    content_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
        comment="落笔时间（后端填，前端不可传）")

    __table_args__ = (
        UniqueConstraint('bill_id', 'kind', name='uk_bbn_bill_kind'),
        Index('k_bbn_biz', 'biz_key'),
        {'comment': '成交笔记'},
    )

    def __str__(self) -> str:
        return f'成交笔记-{self.bill_id}-{self.kind}'


class ProfitYear(Base, BaseModel):
    """年度收益统计模型（按年度汇总 Profit 表的盈亏合计）。"""

    __tablename__ = "bills_profit_year"

    year: Mapped[int] = mapped_column(Integer, unique=True, comment="年度")
    pl_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏l")
    pl_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏s")
    pl_other: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="其他损益")
    pl_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资利息")
    pl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="总平仓盈亏")
    # 累计盈亏：该年度及之前所有年份 pl_all 的累计求和
    pl_cumulative: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="累计盈亏")
    # 资产与收益率（Modified Dietz：年初资产 + 出入金现金流时间加权）
    asset_begin: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment="年初资产")
    asset_end: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment="年末资产")
    asset_avg: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), comment="日均资产")
    return_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment="收益率")

    def __str__(self) -> str:
        return f'年度收益-{self.year}'


class RiskParam(Base, BaseModel):
    """风险参数（v2.0 起只保留保证金率），人工维护 + 日结单反解。"""

    __tablename__ = "bills_risk_param"

    scope: Mapped[str] = mapped_column(String(16), comment="品种或账户")
    param_key: Mapped[str] = mapped_column(String(32), comment="margin_rate/underlying_price")
    # margin_rate:保证金率
    param_value: Mapped[Decimal] = mapped_column(Numeric(12, 6), comment="参数值")
    source: Mapped[str] = mapped_column(String(16), comment="manual/statement/calc")
    # manual:人工按交易所公式常量录入；statement:从券商日结单反解；calc:预留（程序计算回写，目前无代码写入）
    effective_on: Mapped[date] = mapped_column(Date, comment="生效日")
    note: Mapped[Optional[str]] = mapped_column(String(200), comment="备注")

    __table_args__ = (
        UniqueConstraint('scope', 'param_key', 'effective_on', name='uk_param'),
    )

    def __str__(self) -> str:
        return f'风险参数-{self.scope}-{self.param_key}'


class ScenarioGrid(Base, BaseModel):
    """情景压力测试矩阵（任务化重算，铁律 4.8）。

    - loss_amt 负=亏；net_after = 组合净值 − loss；
    - risk_ratio 穿仓时写 9999（DECIMAL 存不了 inf）；
    - account='ALL' 是组合行，其余是分账户行（分账户必看，铁律 §七-6）。
    """

    __tablename__ = "bills_scenario_grid"

    calc_date: Mapped[date] = mapped_column(Date, comment="计算基准日")
    # ⚠️ 16 而不是 8：MODES 的键是 sync / astock_only（11 字符）/ index_only（10 字符），
    # 原先 String(8) 装不下，`run_and_persist` 一写就 `Data too long for column 'mode'`。
    # 只因 category 精确匹配缺陷让 compute() 从未产出过有效 grid，这条路径没被走到。
    mode: Mapped[str] = mapped_column(String(16), comment="sync/astock_only/index_only")
    drop_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), comment="情景档位,跌幅；负数=涨幅（v2.0 起含上行情景档）")
    account: Mapped[str] = mapped_column(String(16), comment="账户；ALL=组合")
    loss_amt: Mapped[Decimal] = mapped_column(Numeric(14, 2), comment="亏损额（正=亏，负=盈）")
    net_after: Mapped[Decimal] = mapped_column(Numeric(14, 2), comment="情景后净值")
    margin_req: Mapped[Decimal] = mapped_column(Numeric(14, 2), comment="保证金需求")
    # 真实风险度 = 保证金 / 情景后净值。净值为 0/负时算不出来，写 9999 兜底（DECIMAL 存不了 inf/None）
    risk_ratio_real: Mapped[Decimal] = mapped_column(Numeric(10, 4), comment="真实口径风险度")
    # 交易所风险度 = 保证金 / (情景后净值 + 融资余额)。 追保判定只用这个口径 ；同样 9999 兜底
    risk_ratio_exch: Mapped[Decimal] = mapped_column(Numeric(10, 4), comment="交易所口径风险度")
    breached: Mapped[int] = mapped_column(Integer, default=0, comment="1=已追保/穿仓")
    engine_ver: Mapped[str] = mapped_column(String(16), comment="公式版本")

    __table_args__ = (
        UniqueConstraint('calc_date', 'mode', 'drop_pct', 'account', name='uk_grid'),
        Index('k_grid_date', 'calc_date'),
        {'comment': '情景压力测试矩阵'},
    )

    def __str__(self) -> str:
        return f'情景-{self.calc_date}-{self.mode}-{self.drop_pct}-{self.account}'

