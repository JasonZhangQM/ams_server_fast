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
    """账单汇总响应（对应 Group 模型，共 34 字段）。"""

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

    # 持仓
    p_long: Optional[int] = None  # 多头持仓
    p_short: Optional[int] = None  # 空头持仓
    p_total: Optional[int] = None  # 持仓

    # 成本
    cost_t_long: Optional[Decimal] = None  # 多头成本
    cost_t_short: Optional[Decimal] = None  # 空头成本
    cost_total: Optional[Decimal] = None  # 持仓成本

    # 市值与浮盈
    price: Optional[Decimal] = None  # 最新价
    multiplier: Optional[int] = None  # 乘数
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

    # ---- v3 派生：归属标的与成交笔记（list_bills 带出，不落 bills_bill 表）----
    vm_symbol: Optional[str] = None  # 归属标的（来自 bills_group_symbol.vm_symbol；融资融券恒为 None）
    note_idea: Optional[str] = None  # 「想法」正文
    note_idea_at: Optional[datetime] = None  # 「想法」落笔时间（晚于成交 = 事后补写）
    note_review: Optional[str] = None  # 「复盘」正文
    note_review_at: Optional[datetime] = None  # 「复盘」落笔时间

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
    acc_aset: Optional[Decimal] = None  # 账户价值（未扣融资负债）
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
    """标的汇总响应（对应 GroupSymbol 模型：14 个派生字段 + vm_symbol，笔记列已全部下线）。"""

    model_config = ConfigDict(from_attributes=True)

    category: str  # 交易分类
    symbol: str  # 代码
    count: int  # 交易次数
    p_total: Optional[int] = None  # 持仓量
    cost_total: Optional[Decimal] = None  # 持仓成本
    price: Optional[Decimal] = None  # 最新价
    multiplier: Optional[int] = None  # 乘数
    value_total: Optional[Decimal] = None  # 当前市值
    pf_total: Optional[Decimal] = None  # 浮动盈亏
    pl_all: Optional[Decimal] = None  # 平仓盈亏
    pfl_all: Optional[Decimal] = None  # 盈亏合计
    diff_br: Optional[Decimal] = None  # 融资余额
    diff_dw: Optional[Decimal] = None  # 出入净额
    diff_dwt: Optional[Decimal] = None  # 划转净额

    # v3：唯一的可编辑列（不进 _DERIVED_COLS，重算不覆盖）
    vm_symbol: Optional[str] = None  # 归属标的；融资融券与未归属为 None

    # 通用字段（BaseModel 提供）
    id: int  # 主键
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class GroupSymbolCodeOut(BaseModel):
    """券商码候选（下拉选项）响应 —— `GET /bills/group-symbols/symbol-options`。

    「期权行情同步清单」（`bds_option_config.broker_symbol`）内联编辑时用它做选项源。
    **只供前端跨模块调用**：bds 后端不 import bills 的任何东西（规则 4.12 依赖只能向下），
    写入仍走 `PUT /bds/option-config/{id}`，bills 只回答「有哪些合法账单码」这一个事实。

    口径：
    - 按 `symbol` 去重 —— 本表唯一键是 `(category, symbol)`，同一个码跨分类会占多行；
    - 排除空码与资金流水占位码（`constants.attachable_sql` 同一口径，不在这里另写白名单）；
    - `p_total` / `value_total` 只用于在选项里标「持仓中」，不参与任何判断。
    """

    model_config = ConfigDict(from_attributes=True)

    symbol: str  # 账单口径代码（如 SHQQ.10012562 / CFFEX.IO2611-P-4000）
    category: str  # 交易分类（期权交易 / 期货交易 / 证券交易）
    p_total: Optional[int] = None  # 持仓量（跨分类取最大）
    value_total: Optional[Decimal] = None  # 当前市值（跨分类求和）


class GroupSymbolVmUpdate(BaseModel):
    """归属标的修改请求（PATCH /bills/group-symbols/{id}/vm）。

    v3 起合约表只剩这一个可编辑字段。融资融券行（symbol='RZRQ'）传任何值都会被
    后端以 400 拒绝——融资只是融资行为，name 不承担归属含义（v3 §4.5.1 规则一）。
    显式传 null 表示清空归属。
    """

    model_config = ConfigDict(from_attributes=True)

    vm_symbol: Optional[str] = None  # 归属标的（irs_value_monitor.symbol）


class BillNoteOut(BaseModel):
    """成交笔记响应（对应 bills_bill_note）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bill_id: Optional[int] = None
    biz_key: str
    kind: str
    content: str
    content_at: datetime
    create_time: datetime
    update_time: datetime


class BillNoteSave(BaseModel):
    """成交笔记保存请求（PUT /bills/bills/{id}/note/{kind}）。

    content_at 由后端写，前端不可传——否则"当时写的"可以被伪造，反自欺就失效了。
    """

    content: str


class ProfitYearOut(BaseModel):
    """年度收益响应（对应 ProfitYear 模型，共 14 字段）。"""

    model_config = ConfigDict(from_attributes=True)

    year: int  # 年度
    pl_long: Optional[Decimal] = None  # 平仓盈亏l
    pl_short: Optional[Decimal] = None  # 平仓盈亏s
    pl_other: Optional[Decimal] = None  # 其他损益
    pl_br: Optional[Decimal] = None  # 融资利息
    pl_all: Optional[Decimal] = None  # 总平仓盈亏
    pl_cumulative: Optional[Decimal] = None  # 累计盈亏
    asset_begin: Optional[Decimal] = None  # 年初资产
    asset_end: Optional[Decimal] = None  # 年末资产
    asset_avg: Optional[Decimal] = None  # 日均资产
    return_rate: Optional[Decimal] = None  # 收益率

    # 通用字段（BaseModel 提供）
    id: int  # 主键
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


# =========================================================================
# 请求 Schema（Pydantic 入参）
# =========================================================================


class BillUpdate(BaseModel):
    """Bill 修改请求（PATCH /bills/bills/{id}）。

    所有字段均为可选，只传需要修改的字段。None 表示不修改该字段。
    Bill 含唯一约束 (account, trade_time, symbol, exec_type, amount_act, id_agree, cash)，
    修改唯一键字段可能触发 IntegrityError，路由层会捕获并返回 400。
    """

    model_config = ConfigDict(from_attributes=True)

    # 基本信息
    trade_time: Optional[datetime] = None  # 交易时间
    symbol: Optional[str] = None  # 代码
    name: Optional[str] = None  # 名称
    exec_type: Optional[str] = None  # 操作类型
    category1: Optional[str] = None  # 一级分类
    category: Optional[str] = None  # 总分类
    b_s: Optional[str] = None  # 买/卖
    c_p: Optional[str] = None  # 沽/购
    o_c: Optional[str] = None  # 开/平

    # 成交数据
    price: Optional[Decimal] = None  # 成交价
    vol: Optional[int] = None  # 成交量
    amount: Optional[Decimal] = None  # 成交额
    amount_act: Optional[Decimal] = None  # 发生额
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
    currency: Optional[str] = None  # 币种
    account_id: Optional[str] = None  # 账号
    market: Optional[str] = None  # 市场
    account: Optional[str] = None  # 账户


class GroupUpdate(BaseModel):
    """Group 修改请求（PATCH /bills/group/{id}）。

    仅允许修改 profit_time（收益试算时间）和 value_time（市值更新时间），
    其余字段由后端批量导入/同步流程自动生成，不允许前端手动编辑。
    """

    model_config = ConfigDict(from_attributes=True)

    profit_time: Optional[datetime] = None  # 收益试算时间
    value_time: Optional[datetime] = None  # 市值更新时间


# =========================================================================
# 风险看板（GET /bills/risk-board，对应 risk_scenario.compute() 输出）
# =========================================================================
class RiskBoardAccountOut(BaseModel):
    """账户风险卡（净值口径）。"""

    net0: float  # 基准净值（模型盯市修正后）
    fin: float  # 融资余额
    eq: float  # 客户权益 = net0 + 融资
    margin: float  # 保证金占用
    real: Optional[float] = None  # 真实风险度 = 保证金 / 净资产
    exch: Optional[float] = None  # 交易所风险度 = 保证金 / 客户权益
    coverage: Optional[float] = None  # 自有资金保证金覆盖率 = 净资产 / 保证金
    breached: bool = False  # 是否已触发追保（交易所口径）


class RiskBoardPositionOut(BaseModel):
    """逐仓持仓明细。"""

    acct: str  # 账户
    symbol: str  # 交易代码
    kind: str  # fut / call / put / sec
    und_key: Optional[str] = None  # 情景分组键
    qty: int  # 持仓数量（带符号，期权为张数）
    nominal: float  # 名义市值
    max_loss: Optional[float] = None  # 最大亏损；null=卖认购理论上行无上限
    drop1_loss: float  # 标的变动 1% 的盈亏敏感度
    margin_now: float  # 当前保证金
    cp: Optional[str] = None  # C=认购 / P=认沽（仅期权）
    side: Optional[str] = None  # long=买方 / short=卖方（仅期权）
    strike: Optional[float] = None  # 行权价（仅期权）
    expiry: Optional[str] = None  # 到期日 ISO 字符串（仅期权）
    days: Optional[int] = None  # 剩余天数（仅期权）
    px: Optional[float] = None  # 盯市单价（仅期权）
    sigma: Optional[float] = None  # 隐含波动率（仅期权）
    delta: Optional[float] = None  # delta（仅期权）


class RiskBoardDetailOut(BaseModel):
    """单情景下的逐仓保证金/亏损（保证金结构图用）。"""

    name: str  # 交易代码
    acct: str  # 账户
    kind: str  # fut / call / put / sec
    mtype: Optional[str] = None  # 保证金公式族：cffex / etf / shex
    und_key: Optional[str] = None  # 情景分组键
    margin: float  # 该情景下的保证金
    loss: float  # 该情景下的盈亏（正=亏）


class RiskBoardScenarioOut(BaseModel):
    """单个情景 × 账户的推演结果。"""

    loss: float  # 组合亏损（正=亏，负=盈）
    net: float  # 剩余净值
    margin: float  # 保证金占用
    real: Optional[float] = None  # 真实风险度
    exch: Optional[float] = None  # 交易所风险度
    breached: bool = False  # 是否追保
    detail: Optional[list[RiskBoardDetailOut]] = None  # 逐仓明细（仅 ALL 行且 detail=true）


class RiskBoardFirstCallOut(BaseModel):
    """追保临界点。"""

    account: str  # 账户
    drop: float  # 临界跌幅；负数=上涨侧触发
    direction: str  # down / up / now
    net: Optional[float] = None  # 临界点剩余净值
    margin: Optional[float] = None  # 临界点保证金


class RiskBoardOut(BaseModel):
    """风险看板完整响应（字段说明与 risk_scenario.compute() 一一对应）。"""

    model_config = ConfigDict(from_attributes=True)

    engine_ver: str  # 引擎版本
    calc_date: str  # 计算日
    data_as_of: Optional[str] = None  # 期权行情快照日
    stale_days: int  # 行情滞后交易日数（0=当日，1=T−1）
    horizon: str  # now=即期 / expiry=到期
    iv_mult: float  # IV 冲击倍数
    net_total: float  # 组合净资产
    margin_total: float  # 组合保证金
    real_total: Optional[float] = None  # 组合真实风险度
    max_exposure: float  # 数学最大亏损合计（不含无上限合约）
    max_exposure_unbounded: bool  # 是否存在理论上行无上限的卖认购
    accounts: dict[str, RiskBoardAccountOut]  # 分账户风险卡
    positions: list[RiskBoardPositionOut]  # 逐仓明细
    grid: dict[str, dict[str, dict[str, RiskBoardScenarioOut]]]  # [mode][drop_pct][account]
    first_call: dict[str, Optional[RiskBoardFirstCallOut]]  # [mode] 最近追保点
    first_call_all: dict[str, dict[str, Optional[RiskBoardFirstCallOut]]]  # [mode][account]
    mode_names: dict[str, str]  # 情景模式中文名
    steps: list[float]  # 情景档位（负数=涨幅）
    upside_steps: list[float]  # 上行情景档位（负数=涨幅）
    warnings: list[str]  # 数据降级/兜底提示
