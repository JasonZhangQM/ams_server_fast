# -*- coding: utf-8 -*-
"""irs 应用 Pydantic v2 响应 Schema 定义。

对应 router.py 中 GET 路由的返回结构：
- DiscountMonitorOut  /irs/discounts-monitor    贴水监测全字段（合并配置+监测）
- UnderlyingMonitorOut     /irs/underlying-monitors       标的监测全字段

所有 Schema 均启用 from_attributes=True 以支持从 ORM 实例直接构造。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# =========================================================================
# 贴水监测：对应 DiscountMonitor 模型全字段（16 字段，合并后单表）
# =========================================================================
class DiscountMonitorOut(BaseModel):
    """贴水监测响应（对应 /irs/discounts-monitor，合并配置+监测全字段）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol_con: str                                 # 连续合约
    symbol: Optional[str] = None                    # 真实合约
    is_main: bool                                   # 主力
    symbol_type: Optional[str] = None               # 合约类别
    con_name: Optional[str] = None                  # 连续周期
    symbol_ud: Optional[str] = None                 # 标的代码
    delisted_date: Optional[date] = None            # 到期日
    days_left: Optional[int] = None                 # 剩余天数
    position: Optional[int] = None                  # 持仓量
    price: Optional[Decimal] = None                 # 合约现价
    price_ud: Optional[Decimal] = None              # 基础现价
    discount: Optional[Decimal] = None              # 贴水
    ratio: Optional[Decimal] = None                 # 贴水率(%)
    ratio_y: Optional[Decimal] = None               # 贴水率(%Y)
    id: int                                         # 主键
    create_time: datetime                           # 创建时间
    update_time: datetime                           # 更新时间


# =========================================================================
# 标的监测：对应 UnderlyingMonitor 模型全字段（25 字段）
# =========================================================================
class UnderlyingMonitorOut(BaseModel):
    """标的监测响应（对应 /irs/underlying-monitors，独立表含估值区间+行情+监测字段）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                # 代码
    name: Optional[str] = None                 # 名称
    # 标的层属性（v3 新增）
    theme: Optional[str] = None                # 主题（唯一定义处）
    # 估值区间
    pp_el: Optional[Decimal] = None            # 极低
    pp_l: Optional[Decimal] = None             # 低
    pp_m: Optional[Decimal] = None             # 中
    pp_h: Optional[Decimal] = None             # 高
    pp_eh: Optional[Decimal] = None            # 极高
    # 行情字段
    py_close: Optional[Decimal] = None         # 上年末
    y_high: Optional[Decimal] = None           # 年高
    y_low: Optional[Decimal] = None            # 年低
    price: Optional[Decimal] = None            # 最新价
    # 行情监测字段
    pv_yh: Optional[Decimal] = None            # 年高(%)
    pv_yl: Optional[Decimal] = None            # 年低(%)
    pv_yy: Optional[Decimal] = None            # 最新(%)
    # 标的监测字段
    pv_el: Optional[Decimal] = None            # 极低(%)
    pv_l: Optional[Decimal] = None             # 低(%)
    pv_m: Optional[Decimal] = None             # 中(%)
    pv_h: Optional[Decimal] = None             # 高(%)
    pv_eh: Optional[Decimal] = None            # 极高(%)
    pv_el_y: Optional[Decimal] = None          # 极低(y%)
    pv_l_y: Optional[Decimal] = None           # 低(y%)
    pv_m_y: Optional[Decimal] = None           # 中(y%)
    pv_h_y: Optional[Decimal] = None           # 高(y%)
    pv_eh_y: Optional[Decimal] = None          # 极高(y%)
    # ---- v3 派生字段（列表页用，不落库；不声明会被 response_model 裁掉，铁律 4.10）----
    zone: Optional[str] = None                 # 当前价所在档位带（低于极低/极低~低/…/高于极高）
    pe_pct: Optional[float] = None             # 当前价对应 PE 分位（bds 只读；非指数标的为 None）
    pb_pct: Optional[float] = None             # 当前价对应 PB 分位（bds 只读；非指数标的为 None）
    pp_effective_date: Optional[date] = None   # 当前生效版日期
    pp_version_count: Optional[int] = None     # 版本数
    pp_pending_date: Optional[date] = None     # 最早的未来版日期（"待生效 X-XX"）
    research_date: Optional[date] = None       # 最新研究定稿日
    research_conclusion: Optional[str] = None  # 最新研究结论
    next_review: Optional[date] = None         # 下次复核
    plan_active: Optional[int] = None          # 生效剧本数
    id: int                                    # 主键
    create_time: datetime                      # 创建时间
    update_time: datetime                      # 更新时间


# =========================================================================
# 标的监测新增：请求 Schema
# =========================================================================
class UnderlyingMonitorCreate(BaseModel):
    """标的监测新增请求（对应 POST /irs/underlying-monitors）。

    v3 起档位版本唯一入口是详情抽屉的「档位」Tab，新增时 pp_* 可选，
    全空时只建主表行，档位后续再去 Tab 里 save_version。
    """

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                  # 代码
    name: str                                    # 名称
    pp_el: Optional[Decimal] = None             # 极低（可选）
    pp_l: Optional[Decimal] = None               # 低（可选）
    pp_m: Optional[Decimal] = None               # 中（可选）
    pp_h: Optional[Decimal] = None               # 高（可选）
    pp_eh: Optional[Decimal] = None              # 极高（可选）
    theme: Optional[str] = None                  # 主题
    effective_date: Optional[date] = None        # 首版生效日（空=今天）


# =========================================================================
# 标的监测修改：请求 Schema（symbol 不可改；pp_* 为空表示不动档位）
# =========================================================================
class UnderlyingMonitorUpdate(BaseModel):
    """标的监测修改请求（对应 PUT /irs/underlying-monitors/{id}）。

    v3 起 pp_* 不再直写主表：全部转发 save_version()（版本表 upsert + 回写主表）。
    pp_* 传 None 表示"不改档位"（只改 name/theme 时不必带五档）。
    """

    model_config = ConfigDict(from_attributes=True)

    name: str                                  # 名称
    pp_el: Optional[Decimal] = None            # 极低
    pp_l: Optional[Decimal] = None             # 低
    pp_m: Optional[Decimal] = None             # 中
    pp_h: Optional[Decimal] = None             # 高
    pp_eh: Optional[Decimal] = None            # 极高
    theme: Optional[str] = None                # 主题
    effective_date: Optional[date] = None      # 档位版本生效日（空=今天）
    # 行情字段（可选，None 表示不修改）
    py_close: Optional[Decimal] = None         # 上年末
    y_high: Optional[Decimal] = None           # 年高
    y_low: Optional[Decimal] = None            # 年低
    price: Optional[Decimal] = None            # 最新价


# =========================================================================
# v3：档位版本（irs_um_version）
# =========================================================================
class UnderlyingVersionOut(BaseModel):
    """档位版本响应：五档 + 现算 pv_* + 状态 + 相对上一版差值。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    effective_date: date
    note: Optional[str] = None
    pp_el: Optional[Decimal] = None
    pp_l: Optional[Decimal] = None
    pp_m: Optional[Decimal] = None
    pp_h: Optional[Decimal] = None
    pp_eh: Optional[Decimal] = None
    # 现算（按主表当前 price / py_close，不落库）
    pv_el: Optional[Decimal] = None
    pv_l: Optional[Decimal] = None
    pv_m: Optional[Decimal] = None
    pv_h: Optional[Decimal] = None
    pv_eh: Optional[Decimal] = None
    pv_el_y: Optional[Decimal] = None
    pv_l_y: Optional[Decimal] = None
    pv_m_y: Optional[Decimal] = None
    pv_h_y: Optional[Decimal] = None
    pv_eh_y: Optional[Decimal] = None
    # current = 当前生效 / history = 历史 / pending = 未来日（当前视图不生效）
    status: str
    pending_next: bool = False
    diff: dict = Field(default_factory=dict)
    create_time: datetime
    update_time: datetime


class UnderlyingVersionSave(BaseModel):
    """档位版本保存请求：选新日期=新建，选历史日期=改那一版。"""

    symbol: str
    effective_date: Optional[date] = None      # 空=今天
    pp_el: Decimal
    pp_l: Decimal
    pp_m: Decimal
    pp_h: Decimal
    pp_eh: Decimal
    note: Optional[str] = None


# =========================================================================
# v3：标的研究（irs_um_research，按 research_date 留版）
# =========================================================================
class UnderlyingResearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    research_date: date
    thesis: Optional[str] = None
    conclusion: Optional[str] = None
    max_risk: Optional[str] = None
    expectation_gap: Optional[str] = None
    pre_condition: Optional[str] = None
    next_review: Optional[date] = None
    research_note: Optional[str] = None
    research_brief: Optional[str] = None
    research_log: Optional[str] = None
    create_time: datetime
    update_time: datetime


class UnderlyingResearchSave(BaseModel):
    """研究保存请求：按 (symbol, research_date) upsert；未传的字段保持不变。"""

    research_date: date
    thesis: Optional[str] = None
    conclusion: Optional[str] = None
    max_risk: Optional[str] = None
    expectation_gap: Optional[str] = None
    pre_condition: Optional[str] = None
    next_review: Optional[date] = None
    research_note: Optional[str] = None
    research_brief: Optional[str] = None
    research_log: Optional[str] = None


# =========================================================================
# v3：剧本（irs_um_plan）
# =========================================================================
class UnderlyingPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    underlying_symbol: str
    trade_symbol: str
    seq: int
    b_s: str
    o_c: Optional[str] = None
    trigger_op: str
    trigger_price: Optional[Decimal] = None
    trigger_ref: Optional[str] = None
    trigger_price2: Optional[Decimal] = None
    qty: Optional[int] = None
    reason: Optional[str] = None
    is_cancelled: bool = False
    # 派生：暂停 / 生效
    status: str
    effective_trigger: Optional[float] = None   # trigger_price ?? 当前档位值
    trigger_ref_value: Optional[float] = None   # 引用档位的当前值
    create_time: datetime
    update_time: datetime


class UnderlyingPlanSave(BaseModel):
    """剧本保存请求：seq 定位（同标的同 seq 覆盖）。"""

    seq: int
    trade_symbol: str
    b_s: str
    o_c: Optional[str] = None
    trigger_op: str = "lte"
    trigger_price: Optional[Decimal] = None
    trigger_ref: Optional[str] = None
    trigger_price2: Optional[Decimal] = None
    qty: Optional[int] = None
    reason: Optional[str] = None
    is_cancelled: bool = False


# =========================================================================
# v3：标的复盘（锚定研究版本，同一版本可多次）
# =========================================================================
class UnderlyingReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    research_id: int
    content: str
    review_date: date
    create_time: datetime
    update_time: datetime


class UnderlyingReviewSave(BaseModel):
    """复盘保存请求：锚定研究版本；review_date 由后端填，前端不可传。"""

    research_id: int
    content: str


# =========================================================================
# v3：投资逻辑 / 证伪条件（批次二的功能，接口先通）
# =========================================================================
class UnderlyingLogicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    seq: int
    content: str
    pricing: Optional[str] = None
    state: str
    state_at: Optional[datetime] = None
    evidence: Optional[str] = None
    create_time: datetime
    update_time: datetime


class UnderlyingLogicSave(BaseModel):
    seq: int
    content: str
    pricing: Optional[str] = None
    state: str = "完好"
    evidence: Optional[str] = None


class UnderlyingInvalidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    logic_id: Optional[int] = None
    seq: int
    proposition: str
    signal: str
    source: Optional[str] = None
    action: Optional[str] = None
    judge: Optional[str] = None
    judge_expr: Optional[str] = None
    status: str
    triggered_at: Optional[date] = None
    create_time: datetime
    update_time: datetime
    # 派生：signal 里没有数字阈值 -> 前端标红（模板要求"阈值不可量化者视为未填"）
    quantified: bool = True
    # P2-2 自动判定候选触发（只读，不改 status）：可算时返回 dict{candidate, current_value, threshold, op, metric}，不可算返回 None
    auto_judged: Optional[dict] = None


class UnderlyingInvalidateSave(BaseModel):
    """证伪条件保存入参（按 seq upsert）。

    ⚠️ 这里**只能有入参字段**。曾误从 UnderlyingInvalidateOut 复制进来 `id` / `create_time` /
    `update_time` 三行，`id: int` 无默认值 ⇒ 任何保存都 422，证伪从来没写进去过。
    出参字段一律放 UnderlyingInvalidateOut，不要回抄。
    """

    seq: int
    proposition: str
    signal: str
    logic_id: Optional[int] = None
    source: Optional[str] = None
    action: Optional[str] = None
    judge: Optional[str] = None
    judge_expr: Optional[str] = None
    status: str = "未触发"
    triggered_at: Optional[date] = None


# =========================================================================
# 状态审计日志（irs_um_state_log，append-only）
# =========================================================================
class UnderlyingStateLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject_type: str
    subject_id: int
    symbol: str
    from_state: Optional[str] = None
    to_state: str
    changed_at: datetime
    note: Optional[str] = None


# =========================================================================
# 目标仓位（irs_um_target，决策层）
# =========================================================================
class UnderlyingTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    target_pct: Optional[Decimal] = None
    note: Optional[str] = None


class UnderlyingTargetSave(BaseModel):
    symbol: str
    target_pct: Optional[Decimal] = None
    note: Optional[str] = None


# =========================================================================
# 今日页（GET /irs/today）：跨标的到价清单，纯聚合、不落库
# =========================================================================
class TodayPlanHit(BaseModel):
    seq: int
    trade_symbol: str
    b_s: str
    o_c: Optional[str] = None
    reason: Optional[str] = None
    effective_trigger: Optional[float] = None


class TodayItem(BaseModel):
    symbol: str
    name: Optional[str] = None
    theme: Optional[str] = None
    price: Optional[float] = None
    zone: Optional[str] = None
    nearest_level_pct: Optional[float] = None   # 距最近档位 %
    pp: Optional[Dict[str, Optional[float]]] = None  # 五档价格 pp_el~pp_eh（今日页刻度条）
    pv: Optional[Dict[str, Optional[float]]] = None  # 距各档 % pv_el~pv_eh（负=档位在下方）
    pe_pct: Optional[float] = None              # 当前价对应 PE 分位（指数标的）
    pb_pct: Optional[float] = None              # 当前价对应 PB 分位（指数标的）
    hit_plans: List[TodayPlanHit] = Field(default_factory=list)
    logic_total: int = 0
    logic_weak: int = 0
    invalidate_triggered: int = 0              # 已触发证伪条数
    has_position: bool = False                 # 是否有归属持仓
    review_due: bool = False                   # 复核已到期
    naked: bool = False                        # 裸仓（有持仓但 logic=0 或 research=0）
    target_pct: Optional[float] = None
    current_pct: Optional[float] = None
    drift: Optional[float] = None              # 当前 − 目标
    priority: int = 5                          # 1 证伪置顶 / 2 剧本命中 / 3 裸仓 / 4 复核 / 5 其余


# =========================================================================
# 风险看板（GET /irs/risk-board，对应 risk_scenario.compute() 输出）
# 2026-11-08 自 bills/schemas.py 整体迁入
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
    detail: Optional[List[RiskBoardDetailOut]] = None  # 逐仓明细（仅 ALL 行且 detail=true）


class RiskBoardFirstCallOut(BaseModel):
    """追保临界点。"""

    account: str  # 账户
    drop: float  # 临界跌幅；负数=上涨侧触发
    direction: str  # down / up / now
    net: Optional[float] = None  # 临界点剩余净值
    margin: Optional[float] = None  # 临界点保证金


class BudgetScopeOut(BaseModel):
    """单个品种组的额度读数。"""

    scope: str  # index_fut / gold / opt_short / etf_short / equity / other
    name: str  # 中文名
    margin: float  # 该组当前保证金占用
    pct: Optional[float] = None  # 占账户净值比例
    cap: Optional[float] = None  # 上限；null=未设上限（只报不判）
    headroom: Optional[float] = None  # 额度 = net0 × cap − margin
    per_lot: Optional[float] = None  # 该组当前每手保证金（组内最大）
    lots: int = 0  # 还能开几手
    status: str = "ok"  # ok / over / unknown


class BudgetTotalOut(BaseModel):
    """账户总闸（四档读数）。"""

    cap: float  # 保证金占用上限（0.85）
    reserve: float  # 其中只留给预设加仓点的一块（0.15）
    usable: float  # 日常可开比例 = cap − reserve（0.70）
    gate_now: float  # net0 × usable − margin(0)
    gate_stress: float  # 判据：net(+stress) × usable − margin(+stress)
    gate_stress_max: float  # 含加仓包的极限，仅展示不参与取 min
    status: str  # ok / over / block / skip


class BudgetAccountOut(BaseModel):
    """单账户预算卡。"""

    net0: float  # 模型盯市净值
    book_net: float  # 账面净值（acc_aset − diff_br）
    book_gap: float  # net0 − 账面净，非 0 时必须提示（BS 重估差）
    margin: float
    total: BudgetTotalOut
    by_scope: List[BudgetScopeOut]
    headroom: Optional[float] = None  # 取 min 后的可开额度
    binding: str  # 取到 min 的约束：scope 名 / total@now / total@stress
    lots: int = 0  # 可开手数
    openable: bool = False  # 额度>0 且够一手才算能开
    per_lot_margin: Optional[float] = None
    verdict: str = "ok"  # block / over / ok / skip


class BudgetSummaryOut(BaseModel):
    """全账户汇总。"""

    verdict: str
    judged_accounts: int
    blocked_accounts: List[str]
    headroom_total: Optional[float] = None


class BudgetOut(BaseModel):
    """风险预算块（irs/service/budget.py）。"""

    stress_drop: float  # 压力档，0.15 = 标的再跌 15%
    mode: str  # 固定 sync
    accounts: Dict[str, BudgetAccountOut]
    symbols: Dict[str, Dict[str, str]]  # 合约 → {acct, scope}，加仓点查额度用
    summary: BudgetSummaryOut


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
    accounts: Dict[str, RiskBoardAccountOut]  # 分账户风险卡
    positions: List[RiskBoardPositionOut]  # 逐仓明细
    grid: Dict[str, Dict[str, Dict[str, RiskBoardScenarioOut]]]  # [mode][drop_pct][account]
    first_call: Dict[str, Optional[RiskBoardFirstCallOut]]  # [mode] 最近追保点
    first_call_all: Dict[str, Dict[str, Optional[RiskBoardFirstCallOut]]]  # [mode][account]
    budget: BudgetOut  # 风险预算与加仓额度
    mode_names: Dict[str, str]  # 情景模式中文名
    steps: List[float]  # 情景档位（负数=涨幅）
    upside_steps: List[float]  # 上行情景档位（负数=涨幅）
    warnings: List[str]  # 数据降级/兜底提示
