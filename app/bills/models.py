# -*- coding: utf-8 -*-
"""bills 应用的 SQLAlchemy 2.0 模型定义。

由 Django (server_dj/apps/bills/models/) 迁移而来，共 7 个模型：
Bill / Profit / Group / GroupAcc / GroupSymbol 。

迁移约定：
- 继承 (Base, BaseModel)，BaseModel 提供 id/create_time/update_time 及通用方法
- __tablename__ 与原 Django Meta.db_table 完全一致
- 外键列名以 _id 结尾（与 Django 数据库列名一致）
- 原模型所有自定义类属性（cols_map_fields/unique_keys/fields_*/agg_rules_daily_acc 等）全部保留
- unique_together 与同名唯一索引合并为 UniqueConstraint
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server_fast.common.db import Base
from server_fast.common.models import BaseModel


class Bill(Base, BaseModel):
    """交易收益表（对应 Django bills_bill）。"""

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

    # ---- 以下为原 Django 模型保留的自定义类属性 ----
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
        # 唯一约束（原 Django UniqueConstraint uk_bills_bill）
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
    """账单收益模型（对应 Django bills_profit）。

    原 Django 使用 OneToOneField(Bill) 关联，此处转为 bill_id 外键列 + relationship。
    """

    __tablename__ = "bills_profit"

    # OneToOne 关联 Bill：外键列名 bill_id（与 Django 数据库列名一致），unique 体现一对一
    bill_id: Mapped[int] = mapped_column(Integer, ForeignKey('bills_bill.id'), unique=True, comment="关联账单")
    # ORM 关联对象（可选，便于联表查询），对应原 related_name='bill_profit'
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
    """账单汇总模型（对应 Django bills_group）。"""

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
    daily_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="日结时间")

    # 持仓
    p_long: Mapped[Optional[int]] = mapped_column(Integer, comment="多头持仓")
    p_short: Mapped[Optional[int]] = mapped_column(Integer, comment="空头持仓")
    p_total: Mapped[Optional[int]] = mapped_column(Integer, comment="持仓")

    # 成本
    cost_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="多头成本")
    cost_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="空头成本")
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="持仓成本")

    # 市值与浮盈
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
    fields_cash_update = [  # 资金试算最佳字段
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
        'value_long', 'value_short', 'value_total',
        'pf_long', 'pf_short', 'pf_total',
    ]
    fields_daily = [  # 收益日结试算所需字段
        'id', 'account', 'category', 'symbol',
        'start_time', 'end_time', 'count', 'daily_time',
    ]
    fields_d_update = [
        'daily_time',
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
    """账户汇总模型（对应 Django bills_group_acc）。"""

    __tablename__ = "bills_group_acc"

    account: Mapped[str] = mapped_column(String(16), unique=True, comment="账户")
    cash_acc: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="资金余额")
    fm_acc: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="理财余额")
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="证券成本")
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="证券市值")
    acc_aset: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="账户净值")
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
    """标的汇总模型（对应 Django bills_group_symbol）。"""

    __tablename__ = "bills_group_symbol"

    category: Mapped[str] = mapped_column(String(16), comment="交易分类")
    symbol: Mapped[str] = mapped_column(String(32), comment="代码")
    count: Mapped[int] = mapped_column(Integer, comment="交易次数")
    p_total: Mapped[Optional[int]] = mapped_column(Integer, comment="持仓量")
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="持仓成本")
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="当前市值")
    pf_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="浮动盈亏")
    pl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="平仓盈亏")
    pfl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="盈亏合计")
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="融资余额")
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="出入净额")
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="划转净额")

    # ---- 保留的自定义类属性 ----
    unique_keys = ['category', 'symbol']

    # unique_together + 同名唯一索引合并为 UniqueConstraint
    __table_args__ = (
        UniqueConstraint('category', 'symbol', name='uk_bills_group_symbol'),
    )

    def __str__(self) -> str:
        return f'标的汇总-{self.category}-{self.symbol}'


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

    def __str__(self) -> str:
        return f'年度收益-{self.year}'

