# -*- coding: utf-8 -*-
"""bills 应用的 Pydantic v2 响应 Schema 定义。

对应 server_fast/app/bills/models.py 中的 5 个 ORM 模型：
Group / Bill / Profit / GroupAcc / GroupSymbol。

约定：
- 使用 pydantic.BaseModel + ConfigDict(from_attributes=True)，可直接从 ORM 实例构造
- 字段类型与模型一致：nullable=False 用必填类型，nullable=True 用 Optional[...] = None
- 字段顺序与模型定义顺序一致；ProfitOut 末尾追加扁平化的关联 Bill 字段
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class GroupOut(BaseModel):
    """账单汇总响应（对应 Group 模型，共 33 字段）。"""

    model_config = ConfigDict(from_attributes=True)

    # 标识与时间
    account: Optional[str] = None  # 账户
    category: str  # 交易分类
    symbol: str  # 代码
    start_time: datetime  # 首次交易时
    end_time: datetime  # 最后交易时
    count: int  # 交易次数
    profit_time: Optional[datetime] = None  # 收益试算时间
    value_time: Optional[datetime] = None  # 市值更新时间
    daily_time: Optional[datetime] = None  # 日结时间

    # 持仓
    p_long: Optional[int] = None  # 多头持仓
    p_short: Optional[int] = None  # 空头持仓
    p_total: Optional[int] = None  # 持仓

    # 成本
    cost_t_long: Optional[Decimal] = None  # 多头成本
    cost_t_short: Optional[Decimal] = None  # 空头成本
    cost_total: Optional[Decimal] = None  # 持仓成本

    # 市值与浮盈
    value_long: Optional[Decimal] = None  # 多头市值
    value_short: Optional[Decimal] = None  # 空头市值
    value_total: Optional[Decimal] = None  # 市值
    pf_long: Optional[Decimal] = None  # 多头浮盈
    pf_short: Optional[Decimal] = None  # 空头浮盈
    pf_total: Optional[Decimal] = None  # 浮动盈亏

    # 平仓盈亏
    pl_t_long: Optional[Decimal] = None  # 多头平仓盈亏
    pl_t_short: Optional[Decimal] = None  # 空头平仓盈亏
    pl_total: Optional[Decimal] = None  # 平仓盈亏
    pl_t_other: Optional[Decimal] = None  # 其他损益
    pl_t_ft: Optional[Decimal] = None  # 税费
    pl_t_br: Optional[Decimal] = None  # 融资利息

    # 差额
    diff_br: Optional[Decimal] = None  # 融资余额
    diff_dw: Optional[Decimal] = None  # 出入净额
    diff_dwt: Optional[Decimal] = None  # 划转净额

    # 通用字段（BaseModel 提供）
    id: int  # 主键
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class BillOut(BaseModel):
    """交易收益响应（对应 Bill 模型，共 36 字段）。"""

    model_config = ConfigDict(from_attributes=True)

    # 基本信息
    trade_time: datetime  # 交易时间
    symbol: str  # 代码
    name: str  # 名称
    exec_type: str  # 操作类型
    category1: str  # 一级分类
    category: str  # 总分类
    b_s: Optional[str] = None  # 买/卖
    c_p: Optional[str] = None  # 沽/购
    o_c: Optional[str] = None  # 开/平

    # 成交数据
    price: Decimal  # 成交价
    vol: int  # 成交量
    amount: Decimal  # 成交额
    amount_act: Decimal  # 发生额
    balance: Optional[int] = None  # 余额

    # 费用明细
    fee_tax: Optional[Decimal] = None  # 税费合计
    fees: Optional[Decimal] = None  # 手续费/佣金
    taxes: Optional[Decimal] = None  # 印花税
    fee_exec1: Optional[Decimal] = None  # 过户费
    fee_exec2: Optional[Decimal] = None  # 交易过户费
    fee_exec3: Optional[Decimal] = None  # 行权过户费
    fee_hand: Optional[Decimal] = None  # 经手费
    fee_sr: Optional[Decimal] = None  # 证管费
    fee_clear: Optional[Decimal] = None  # 清算费
    fee_reg: Optional[Decimal] = None  # 交易规费
    fee_other: Optional[Decimal] = None  # 其他费用
    premium: Optional[Decimal] = None  # 权利金收支
    cash: Optional[Decimal] = None  # 资金余额

    # 编号与账户信息
    id_exec: Optional[str] = None  # 成交编号
    id_agree: Optional[str] = None  # 流水号
    currency: str  # 币种
    account_id: Optional[str] = None  # 账号
    market: Optional[str] = None  # 市场
    account: str  # 账户

    # 通用字段（BaseModel 提供）
    id: int  # 主键
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class ProfitOut(BaseModel):
    """账单收益响应（对应 Profit 模型，共 22 字段）。

    自身 18 字段 + 扁平化嵌入关联 Bill 的 account/symbol/name/trade_time（4 字段）。
    关联 Bill 可能不存在，故扁平化字段声明为 Optional。
    """

    model_config = ConfigDict(from_attributes=True)

    # 关联 Bill
    bill_id: int  # 关联账单

    # 持仓与成本
    p_long: Optional[int] = None  # 多头持仓
    p_short: Optional[int] = None  # 空头持仓
    cost_t_long: Optional[Decimal] = None  # 多头成本
    cost_t_short: Optional[Decimal] = None  # 空头成本
    cost_u_long: Optional[Decimal] = None  # u成本l
    cost_u_short: Optional[Decimal] = None  # u成本s

    # 盈亏与差额
    pl_long: Optional[Decimal] = None  # 平仓盈亏l
    pl_short: Optional[Decimal] = None  # 平仓盈亏s
    pl_other: Optional[Decimal] = None  # 其他损益
    pl_ft: Optional[Decimal] = None  # 税费
    pl_br: Optional[Decimal] = None  # 融资利息
    diff_br: Optional[Decimal] = None  # 融资余额
    diff_dw: Optional[Decimal] = None  # 入金净额
    diff_dwt: Optional[Decimal] = None  # 划转净额

    # 通用字段（BaseModel 提供）
    id: int  # 主键
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间

    # 扁平化嵌入关联 Bill 的字段（放末尾；关联 Bill 可能不存在）
    account: Optional[str] = None  # 账户
    symbol: Optional[str] = None  # 代码
    name: Optional[str] = None  # 名称
    trade_time: Optional[datetime] = None  # 交易时间


class GroupAccOut(BaseModel):
    """账户汇总响应（对应 GroupAcc 模型，共 16 字段）。"""

    model_config = ConfigDict(from_attributes=True)

    account: str  # 账户
    cash_acc: Optional[Decimal] = None  # 资金余额
    fm_acc: Optional[Decimal] = None  # 理财余额
    cost_total: Optional[Decimal] = None  # 证券成本
    value_total: Optional[Decimal] = None  # 证券市值
    acc_aset: Optional[Decimal] = None  # 账户净值
    pf_total: Optional[Decimal] = None  # 浮动盈亏
    pl_all: Optional[Decimal] = None  # 平仓盈亏
    pfl_all: Optional[Decimal] = None  # 盈亏合计
    diff_br: Optional[Decimal] = None  # 融资余额
    diff_dw: Optional[Decimal] = None  # 入金净额
    diff_dwt: Optional[Decimal] = None  # 划转净额
    status: Optional[Decimal] = None  # 校验

    # 通用字段（BaseModel 提供）
    id: int  # 主键
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class GroupSymbolOut(BaseModel):
    """标的汇总响应（对应 GroupSymbol 模型，共 16 字段）。"""

    model_config = ConfigDict(from_attributes=True)

    category: str  # 交易分类
    symbol: str  # 代码
    count: int  # 交易次数
    p_total: Optional[int] = None  # 持仓量
    cost_total: Optional[Decimal] = None  # 持仓成本
    value_total: Optional[Decimal] = None  # 当前市值
    pf_total: Optional[Decimal] = None  # 浮动盈亏
    pl_all: Optional[Decimal] = None  # 平仓盈亏
    pfl_all: Optional[Decimal] = None  # 盈亏合计
    diff_br: Optional[Decimal] = None  # 融资余额
    diff_dw: Optional[Decimal] = None  # 出入净额
    diff_dwt: Optional[Decimal] = None  # 划转净额

    # 通用字段（BaseModel 提供）
    id: int  # 主键
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间
