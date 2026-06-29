# -*- coding: utf-8 -*-
"""bills 应用的 SQLAlchemy 2.0 模型定义。

由 Django (server_dj/apps/bills/models/) 迁移而来，共 7 个模型：
Bill / Profit / Group / GroupAcc / GroupSymbol / DailyValue / DailyAcc。

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
    trade_time: Mapped[datetime] = mapped_column(DateTime)
    symbol: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(32))
    exec_type: Mapped[str] = mapped_column(String(16))
    category1: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(16))
    b_s: Mapped[Optional[str]] = mapped_column(String(4))
    c_p: Mapped[Optional[str]] = mapped_column(String(4))
    o_c: Mapped[Optional[str]] = mapped_column(String(4))

    # 成交数据
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    vol: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    amount_act: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    balance: Mapped[Optional[int]] = mapped_column(Integer)

    # 费用明细
    fee_tax: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fees: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    taxes: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_exec1: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_exec2: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_exec3: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_hand: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_sr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_clear: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_reg: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fee_other: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    premium: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # 编号与账户信息（account_id 为股东账号 CharField，非外键）
    id_exec: Mapped[Optional[str]] = mapped_column(String(16))
    id_agree: Mapped[Optional[str]] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    account_id: Mapped[Optional[str]] = mapped_column(String(16))
    market: Mapped[Optional[str]] = mapped_column(String(16))
    account: Mapped[str] = mapped_column(String(16))

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
    bill_id: Mapped[int] = mapped_column(Integer, ForeignKey('bills_bill.id'), unique=True)
    # ORM 关联对象（可选，便于联表查询），对应原 related_name='bill_profit'
    bill: Mapped["Bill"] = relationship("Bill")

    # 持仓与成本
    p_long: Mapped[Optional[int]] = mapped_column(Integer)
    p_short: Mapped[Optional[int]] = mapped_column(Integer)
    cost_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cost_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cost_u_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    cost_u_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))

    # 盈亏与差额
    pl_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_other: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_ft: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

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
    account: Mapped[Optional[str]] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    count: Mapped[int] = mapped_column(Integer)
    profit_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    value_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    daily_time: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # 持仓
    p_long: Mapped[Optional[int]] = mapped_column(Integer)
    p_short: Mapped[Optional[int]] = mapped_column(Integer)
    p_total: Mapped[Optional[int]] = mapped_column(Integer)

    # 成本
    cost_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cost_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # 市值与浮盈
    value_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    value_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pf_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pf_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pf_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # 平仓盈亏
    pl_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_t_other: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_t_ft: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_t_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # 差额
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

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

    account: Mapped[str] = mapped_column(String(16), unique=True)
    cash_acc: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    fm_acc: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    acc_aset: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pf_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pfl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pfl_day: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    status: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

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

    category: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    count: Mapped[int] = mapped_column(Integer)
    p_total: Mapped[Optional[int]] = mapped_column(Integer)
    cost_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    value_d_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    value_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pf_d_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pf_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pfl_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_br: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dw: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    diff_dwt: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # ---- 保留的自定义类属性 ----
    unique_keys = ['category', 'symbol']

    # unique_together + 同名唯一索引合并为 UniqueConstraint
    __table_args__ = (
        UniqueConstraint('category', 'symbol', name='uk_bills_group_symbol'),
    )

    def __str__(self) -> str:
        return f'标的汇总-{self.category}-{self.symbol}'


class DailyValue(Base, BaseModel):
    """日结(交易)模型（对应 Django bills_daily_value）。"""

    __tablename__ = "bills_daily_value"

    account: Mapped[Optional[str]] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    multiplier: Mapped[Optional[int]] = mapped_column(Integer)
    trade_date: Mapped[date] = mapped_column(Date)
    p_long: Mapped[Optional[int]] = mapped_column(Integer)
    p_short: Mapped[Optional[int]] = mapped_column(Integer)
    cost_t_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cost_t_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    value_d_long: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    value_d_short: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # ---- 保留的自定义类属性 ----
    unique_keys = ['account', 'category', 'symbol', 'trade_date']
    fields_update = [  # 收益日结最后字段
        'close', 'multiplier',
        'p_long', 'p_short',
        'cost_t_long', 'cost_t_short',
        'value_d_long', 'value_d_short',
    ]
    fields_group_symbol = [  # 计算 group_symbol 当日盈亏用
        'category', 'symbol',
        'value_d_long', 'value_d_short',
    ]

    # unique_together + 同名唯一索引合并为 UniqueConstraint
    __table_args__ = (
        UniqueConstraint('account', 'category', 'symbol', 'trade_date', name='uk_bills_daily_value'),
        Index('idx_bills_daily_value_account', 'account'),
        Index('idx_bills_daily_value_category', 'category'),
        Index('idx_bills_daily_value_symbol', 'symbol'),
        Index('idx_bills_daily_value_date', 'trade_date'),
    )

    def __str__(self) -> str:
        account = self.account or '未知账户'
        return f'账单汇总-{account}-{self.category}-{self.symbol}'


class DailyAcc(Base, BaseModel):
    """日结(账户)模型（对应 Django bills_daily_acc）。"""

    __tablename__ = "bills_daily_acc"

    # 周期类型常量（原 Django DAILY_TYPE_CHOICES），保留供业务层使用
    DAILY_TYPE_DAY = 'day'
    DAILY_TYPE_MONTH = 'month'
    DAILY_TYPE_QUARTER = 'quarter'
    DAILY_TYPE_YEAR = 'year'
    DAILY_TYPE_CHOICES = (
        (DAILY_TYPE_DAY, '日'),
        (DAILY_TYPE_MONTH, '月'),
        (DAILY_TYPE_QUARTER, '季'),
        (DAILY_TYPE_YEAR, '年'),
    )

    account: Mapped[Optional[str]] = mapped_column(String(16))
    # default 引用上方类常量，默认按日统计
    daily_type: Mapped[str] = mapped_column(String(8), default=DAILY_TYPE_DAY)
    trade_date: Mapped[str] = mapped_column(String(16))

    # 资产快照（非空）
    daily_l: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    daily_s: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    daily_cash: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    crj: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    hz: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    daily_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    # 损益（可空）
    daily_l_cg: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    daily_s_cg: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cg_daily: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cg_cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cg_all: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    cg_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 4))

    # 累计（非空）
    cum_cg: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    cum_crj: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    # ---- 保留的自定义类属性 ----
    unique_keys = ['account', 'trade_date', 'daily_type']
    fields_update = [  # 收益日结最后字段
        'daily_l', 'daily_s', 'daily_cash', 'crj', 'hz',
        'daily_value', 'daily_l_cg', 'daily_s_cg', 'cg_daily',
        'cg_cash', 'cg_all', 'cg_pct', 'cum_cg', 'cum_crj',
    ]
    fields_group_acc = [  # 计算账户汇总当日收益时需要的字段
        'account', 'trade_date',
        'crj', 'hz', 'daily_value',
    ]
    agg_rules_daily_acc = {  # 聚合规则：字段 -> 聚合方式
        'daily_l': 'last',
        'daily_s': 'last',
        'daily_cash': 'last',
        'crj': 'sum',
        'hz': 'sum',
        'daily_value': 'last',
        'daily_l_cg': 'sum',
        'daily_s_cg': 'sum',
        'cg_daily': 'sum',
        'cg_cash': 'sum',
        'cg_all': 'sum',
        'cum_cg': 'last',
        'cum_crj': 'last',
    }

    # unique_together 转为唯一约束；uk_bills_value_acc 为原 Django 显式普通索引
    __table_args__ = (
        UniqueConstraint('account', 'trade_date', 'daily_type', name='uk_bills_daily_acc'),
        Index('uk_bills_value_acc', 'account', 'trade_date'),
    )

    def __str__(self) -> str:
        account = self.account or '未知账户'
        return f'账单汇总-{account}'
