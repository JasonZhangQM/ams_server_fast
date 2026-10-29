# -*- coding: utf-8 -*-
"""irs 应用 Pydantic v2 响应 Schema 定义。

对应 router.py 中 GET 路由的返回结构：
- DiscountMonitorOut  /irs/discounts-monitor    贴水监测全字段（合并配置+监测）
- ValueMonitorOut     /irs/value-monitors       估值监测全字段

所有 Schema 均启用 from_attributes=True 以支持从 ORM 实例直接构造。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

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
# 估值监测：对应 ValueMonitor 模型全字段（25 字段）
# =========================================================================
class ValueMonitorOut(BaseModel):
    """估值监测响应（对应 /irs/value-monitors，独立表含估值区间+行情+监测字段）。"""

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
    # 估值监测字段
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
# 估值监测新增：请求 Schema（7 个必填字段）
# =========================================================================
class ValueMonitorCreate(BaseModel):
    """估值监测新增请求（对应 POST /irs/value-monitors）。"""

    model_config = ConfigDict(from_attributes=True)

    symbol: str                                # 代码
    name: str                                  # 名称
    pp_el: Decimal                             # 极低
    pp_l: Decimal                              # 低
    pp_m: Decimal                              # 中
    pp_h: Decimal                              # 高
    pp_eh: Decimal                             # 极高
    theme: Optional[str] = None                # 主题
    effective_date: Optional[date] = None      # 首版生效日（空=今天）


# =========================================================================
# 估值监测修改：请求 Schema（symbol 不可改；pp_* 为空表示不动档位）
# =========================================================================
class ValueMonitorUpdate(BaseModel):
    """估值监测修改请求（对应 PUT /irs/value-monitors/{id}）。

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
# v3：档位版本（irs_vm_version）
# =========================================================================
class VmVersionOut(BaseModel):
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


class VmVersionSave(BaseModel):
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
# v3：标的研究（irs_vm_research，按 research_date 留版）
# =========================================================================
class VmResearchOut(BaseModel):
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


class VmResearchSave(BaseModel):
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
# v3：剧本（irs_vm_plan）
# =========================================================================
class VmPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vm_symbol: str
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


class VmPlanSave(BaseModel):
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
# v3：投资逻辑 / 证伪条件（批次二的功能，接口先通）
# =========================================================================
class VmLogicOut(BaseModel):
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


class VmLogicSave(BaseModel):
    seq: int
    content: str
    pricing: Optional[str] = None
    state: str = "完好"
    evidence: Optional[str] = None


class VmInvalidateOut(BaseModel):
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


class VmInvalidateSave(BaseModel):
    """证伪条件保存入参（按 seq upsert）。

    ⚠️ 这里**只能有入参字段**。曾误从 VmInvalidateOut 复制进来 `id` / `create_time` /
    `update_time` 三行，`id: int` 无默认值 ⇒ 任何保存都 422，证伪从来没写进去过。
    出参字段一律放 VmInvalidateOut，不要回抄。
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
# 状态审计日志（irs_vm_state_log，append-only）
# =========================================================================
class VmStateLogOut(BaseModel):
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
# 目标仓位（irs_vm_target，决策层）
# =========================================================================
class VmTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    target_pct: Optional[Decimal] = None
    note: Optional[str] = None


class VmTargetSave(BaseModel):
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
