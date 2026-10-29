# -*- coding: utf-8 -*-
"""今日页（GET /irs/today）：跨标的到价清单，纯聚合、不落库。

把「8 个标的 × 5 个 Tab」摊开的「该干什么」压成一张按优先级排序的清单：
    1 证伪已触发（红）→ 2 剧本命中（橙）→ 3 裸仓且有持仓（黄）→ 4 复核到期（蓝）→ 5 其余。

口径只在后端这一处算（铁律 4.5）：
- zone：`vm_version.compute_zone`（带语义、左闭右开）
- 命中：现价满足剧本/委托的 trigger_op（`effective_trigger` 现算，不落库）
- 裸仓：有归属持仓 且（logic=0 或 research=0）
- 漂移：target_pct（irs 决策层）− current_pct（bills 事实层），前端派生不落库

依赖方向：irs → bills（GroupSymbol/GroupAcc 只读）、irs → bds（分位只读），均属允许方向。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from server_fast.app.bills.models import GroupAcc, GroupSymbol
from server_fast.app.irs.models import (
    ValueMonitor,
    VmInvalidate,
    VmLogic,
    VmPlan,
    VmTarget,
)
from server_fast.app.irs.service.vm_plan import effective_trigger
from server_fast.app.irs.service.vm_research import latest
from server_fast.app.irs.service.vm_version import compute_zone, effective_version

# 能取到估值分位的指数标的（方案 P0-4 / P2-1），其余显示 None
INDEX_PCT_SYMBOLS = {"SHSE.000300", "SHSE.000905", "SPX"}

# 距最近档位 %：pv_* 字段即 (档位/现价-1)*100，取绝对值最小者
PV_FIELDS = ("pv_el", "pv_l", "pv_m", "pv_h", "pv_eh")


def percentiles() -> dict:
    """{(index_code, metric): pct_10y}，失败返回空 dict（分位是纠偏，不是今日页主链）。

    列表页 / 详情抽屉同样复用本函数补 pe_pct/pb_pct（口径唯一，铁律 4.5）。

    ⚠️ SPX（标普500）在 irs_value_monitor 里有一条，但 index_valuation 体系
    只覆盖国内 A 股宽基指数 —— 这里按 dbsCfg.INDEX_CODE 白名单过滤，避免
    get_percentiles("未知指数代码：SPX") 抛 ValueError 被吞掉。
    """
    try:
        from server_fast.app.bds.services import index_valuation
        from server_fast.app.bds.config import Config as dbsCfg
        known = set(dbsCfg.INDEX_CODE.keys())
        # 只向 bds 层传递它认识的指数代码（SPX 不在其中，跳过不报错）
        valid = [c for c in INDEX_PCT_SYMBOLS if c in known]
        out = {}
        for metric in ("pe_ttm", "pb_mrq"):
            data = index_valuation.get_percentiles(index_codes=valid, metric=metric)
            for item in data.get("items", []):
                p10 = item.get("pct_10y") or {}
                out[(item["index_code"], metric)] = p10.get("pct")
        return out
    except Exception:
        return {}


def _is_hit(p: VmPlan, pp: dict, price) -> bool:
    """现价是否满足触发条件（命中 = 到价）。"""
    if price is None or p.is_cancelled:
        return False
    trig = effective_trigger(p, pp)
    if trig is None:
        return False
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    op = p.trigger_op or "lte"
    if op == "lte":
        return px <= trig
    if op == "gte":
        return px >= trig
    if op == "range":
        p2 = float(p.trigger_price2) if p.trigger_price2 is not None else None
        if p2 is None:
            return px >= trig
        return trig <= px <= p2
    return False


def _nearest_level_pct(vm: ValueMonitor) -> Optional[float]:
    vals = []
    for f in PV_FIELDS:
        v = getattr(vm, f, None)
        if v is not None:
            vals.append((abs(float(v)), float(v)))
    return min(vals, key=lambda x: x[0])[1] if vals else None


def _unfinished_plans(db: Session, symbol: str, pp: dict) -> list[VmPlan]:
    """未作废、且「值得今日关注」的计划行：剧本全部（长期有效）、委托未执行未过期。"""
    today = date.today()
    rows = db.query(VmPlan).filter(VmPlan.vm_symbol == symbol).order_by(VmPlan.seq).all()
    out = []
    for p in rows:
        if p.is_cancelled:
            continue
        if p.kind == "剧本":
            out.append(p)
            continue
        # 委托：已执行（bill_id 有值）或已过期的不再是「今日待办」
        if p.bill_id is not None:
            continue
        if p.valid_until is not None and p.valid_until < today:
            continue
        out.append(p)
    return out


def today_items(db: Session) -> list[dict]:
    """聚合全部标的的今日信息并按优先级排序。"""
    today = date.today()
    vms = db.query(ValueMonitor).order_by(ValueMonitor.symbol).all()
    pct_map = percentiles()

    # 目标仓位 + 当前市值占比（只查一次，8 个标的规模下 JOIN 反而重）
    targets = {t.symbol: t for t in db.query(VmTarget).all()}
    total_asset = db.query(func.coalesce(func.sum(GroupAcc.acc_aset), 0)).scalar() or 0
    current = {}
    has_pos = set()
    gs_rows = db.query(GroupSymbol.value_total, GroupSymbol.p_total, GroupSymbol.vm_symbol).all()
    for value_total, p_total, vm_symbol in gs_rows:
        if not vm_symbol:
            continue
        if p_total or (value_total is not None and value_total != 0):
            has_pos.add(vm_symbol)
        if value_total is not None:
            current[vm_symbol] = current.get(vm_symbol, 0) + float(value_total)

    # 逻辑 / 证伪计数，按标的聚成 dict（一次查询）
    logic_total = dict(db.query(VmLogic.symbol, func.count(VmLogic.id))
                       .group_by(VmLogic.symbol).all())
    logic_weak = dict(db.query(VmLogic.symbol, func.count(VmLogic.id))
                      .filter(VmLogic.state == "减弱").group_by(VmLogic.symbol).all())
    inv_triggered = dict(db.query(VmInvalidate.symbol, func.count(VmInvalidate.id))
                         .filter(VmInvalidate.status == "已触发")
                         .group_by(VmInvalidate.symbol).all())

    items = []
    for vm in vms:
        sym = vm.symbol
        pp = {}
        ver = effective_version(db, sym)
        if ver is not None:
            pp = {"pp_el": ver.pp_el, "pp_l": ver.pp_l, "pp_m": ver.pp_m,
                  "pp_h": ver.pp_h, "pp_eh": ver.pp_eh}

        plans = _unfinished_plans(db, sym, pp)
        hit_plans = [{
            "seq": p.seq, "kind": p.kind, "trade_symbol": p.trade_symbol,
            "b_s": p.b_s, "o_c": p.o_c, "reason": p.reason,
            "effective_trigger": effective_trigger(p, pp),
        } for p in plans if _is_hit(p, pp, vm.price)]

        research = latest(db, sym) or {}
        next_review = research.get("next_review")
        review_due = bool(next_review and next_review < today)

        n_logic = int(logic_total.get(sym, 0))
        n_research = 1 if research else 0
        positioned = sym in has_pos
        naked = positioned and (n_logic == 0 or n_research == 0)

        t = targets.get(sym)
        target_pct = float(t.target_pct) if t and t.target_pct is not None else None
        cur_amt = current.get(sym)
        current_pct = (round(cur_amt / float(total_asset) * 100, 4)
                       if total_asset and cur_amt is not None else None)
        drift = (round(current_pct - target_pct, 4)
                 if current_pct is not None and target_pct is not None else None)

        if int(inv_triggered.get(sym, 0)) > 0:
            priority = 1
        elif hit_plans:
            priority = 2
        elif naked:
            priority = 3
        elif review_due:
            priority = 4
        else:
            priority = 5

        items.append({
            "symbol": sym,
            "name": vm.name,
            "theme": vm.theme,
            "price": float(vm.price) if vm.price is not None else None,
            "zone": compute_zone(pp, vm.price),
            "nearest_level_pct": _nearest_level_pct(vm),
            "pe_pct": pct_map.get((sym, "pe_ttm")),
            "pb_pct": pct_map.get((sym, "pb_mrq")),
            "hit_plans": hit_plans,
            "logic_total": n_logic,
            "logic_weak": int(logic_weak.get(sym, 0)),
            "invalidate_triggered": int(inv_triggered.get(sym, 0)),
            "has_position": positioned,
            "review_due": review_due,
            "naked": naked,
            "target_pct": target_pct,
            "current_pct": current_pct,
            "drift": drift,
            "priority": priority,
        })

    items.sort(key=lambda x: (x["priority"], x["symbol"]))
    return items


# =========================================================================
# P2-2 证伪自动判定（只读，候选不改 status）
# =========================================================================

import re
from server_fast.app.irs.models import VmInvalidate, ValueMonitor

# judge_expr 格式约定：{metric}{op}{value}，如 price<=3900 / pb_pct>=0.7
_OP_RE = re.compile(r"^(.*?)\s*(<=|>=|==|=|<|>|≤|≥)\s*([0-9]*\.?[0-9]+)\s*$")


def judge_one(db: Session, inv: VmInvalidate,
              pct_map: Optional[dict] = None) -> Optional[dict]:
    """对一条证伪做候选触发判定（只读，**不改 status**）。

    三类分治（方案 P2-2）：
    - price       → 当前价 ValueMonitor.price
    - percentile  → 指数标的 pe_pct / pb_pct 10y 分位（vm_today.percentiles()）
    - manual      → 不做
    - discount    → 暂不做（贴水率需要跨模块查）

    返回格式（可算的才有值，不可算返回 None）：
        {"candidate": True/False, "current_value": float,
         "threshold": float, "op": str, "metric": str}

    关键约束：**只写候选、不改 VmInvalidate.status**。机器给提示，人做决定。
    """
    if inv.judge in (None, "manual"):
        return None
    if not inv.judge_expr:
        return None
    m = _OP_RE.match(inv.judge_expr.strip())
    if not m:
        return None
    metric = m.group(1).strip()
    op_raw = m.group(2).strip()
    try:
        threshold = float(m.group(3))
    except (TypeError, ValueError):
        return None

    # 统一比较符（Unicode 符号归一并去掉等号前的多余空格）
    op = {"≤": "<=", "≥": ">=", "=": "=="}.get(op_raw, op_raw)
    if op not in ("<", ">", "<=", ">=", "=="):
        return None

    # 数据源解析
    current_value: Optional[float] = None
    if inv.judge == "price" and metric == "price":
        vm = (db.query(ValueMonitor.price)
              .filter(ValueMonitor.symbol == inv.symbol).scalar())
        current_value = float(vm) if vm is not None else None
    elif inv.judge == "percentile" and metric in ("pe_pct", "pb_pct"):
        pct = (pct_map or percentiles()).get((inv.symbol, metric))
        current_value = float(pct) if pct is not None else None
    else:
        return None  # discount / 其他未实现

    if current_value is None:
        return None

    # 执行比较
    ops = {
        "<=": lambda a, b: a <= b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        ">": lambda a, b: a > b,
        "==": lambda a, b: abs(a - b) < 1e-9,
    }
    return {
        "candidate": bool(ops[op](current_value, threshold)),
        "current_value": current_value,
        "threshold": threshold,
        "op": op,
        "metric": metric,
    }


def judge_list(db: Session, symbol: str) -> dict[int, Optional[dict]]:
    """批量判定一个标的的全部证伪（复用一次 percentiles() 调用）。"""
    invs = (db.query(VmInvalidate)
            .filter(VmInvalidate.symbol == symbol).all())
    if not invs:
        return {}
    pct_map = percentiles()
    return {inv.id: judge_one(db, inv, pct_map) for inv in invs}