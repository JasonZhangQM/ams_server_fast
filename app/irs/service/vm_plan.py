# -*- coding: utf-8 -*-
"""剧本（irs_vm_plan）：CRUD + 派生状态 + trigger_ref 解析。

状态**不落库**（存了必然漂移）：is_cancelled=1 -> 暂停；其余 -> 生效。
剧本长期有效、可反复执行；执行次数与盈亏归因走 `bills_bill_note.plan_id`，
本表不记「做没做」。

trigger_ref 让"剧本点位引用档位"成为可能：档位改版后剧本自动跟随，
不用每次调档位再手改 8 条剧本。trigger_price 显式填了则以显式值为准。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from server_fast.app.bills.constants import attachable_sql
from server_fast.app.irs.models import ValueMonitor, VmPlan
from server_fast.app.irs.service.vm_version import effective_version

FIELDS = ("trade_symbol", "seq", "b_s", "o_c", "trigger_op", "trigger_price",
          "trigger_ref", "trigger_price2", "qty", "reason", "is_cancelled")


def derive_status(p: VmPlan) -> str:
    """剧本只有两态：暂停（is_cancelled） / 生效。"""
    return "暂停" if p.is_cancelled else "生效"


def effective_trigger(p: VmPlan, pp: dict) -> Optional[float]:
    """有效触发价：显式 trigger_price 优先，否则取档位当前值。"""
    if p.trigger_price is not None:
        return float(p.trigger_price)
    if p.trigger_ref and p.trigger_ref in pp and pp[p.trigger_ref] is not None:
        return float(pp[p.trigger_ref])
    return None


def _pp_of(db: Session, symbol: str) -> dict:
    """当前生效档位（用于解析 trigger_ref）。"""
    ver = effective_version(db, symbol)
    if ver is None:
        return {}
    return {"pp_el": ver.pp_el, "pp_l": ver.pp_l, "pp_m": ver.pp_m,
            "pp_h": ver.pp_h, "pp_eh": ver.pp_eh}


def list_by_symbol(db: Session, symbol: str) -> list[dict]:
    pp = _pp_of(db, symbol)
    rows = (db.query(VmPlan).filter(VmPlan.vm_symbol == symbol)
            .order_by(VmPlan.seq.asc()).all())
    return [_to_dict(r, pp) for r in rows]


def list_all(db: Session) -> list[dict]:
    """全量剧本（跨标的），供前端归因表 plan_id → 剧本名/标的映射。

    bills 侧归因只返回 plan_id（铁律 4.0：bills 不依赖 irs），
    由本接口补 plan_id 对应的 (symbol, trigger_ref, trade_symbol)，
    前端据此把「剧本盈亏归因」的行名拼出来。8 个标的规模下逐标的缓存档位即可。
    """
    rows = db.query(VmPlan).order_by(VmPlan.vm_symbol.asc(), VmPlan.seq.asc()).all()
    pp_cache: dict = {}
    out = []
    for p in rows:
        if p.vm_symbol not in pp_cache:
            pp_cache[p.vm_symbol] = _pp_of(db, p.vm_symbol)
        out.append(_to_dict(p, pp_cache[p.vm_symbol]))
    return out


def upsert(db: Session, symbol: str, payload: dict) -> dict:
    if db.query(ValueMonitor.symbol).filter(ValueMonitor.symbol == symbol).first() is None:
        raise ValueError(f"标的 {symbol} 不在估值监测里")
    seq = payload.get("seq")
    if seq is None:
        raise ValueError("seq 必填")
    if payload.get("trigger_price") is None and not payload.get("trigger_ref"):
        raise ValueError("trigger_price 与 trigger_ref 必须至少填一个")
    if payload.get("trigger_op") == "range" and payload.get("trigger_price2") is None:
        raise ValueError("区间触发必须填 trigger_price2")

    row = (db.query(VmPlan)
           .filter(VmPlan.vm_symbol == symbol, VmPlan.seq == seq)
           .one_or_none())
    action = "created" if row is None else "updated"
    if row is None:
        row = VmPlan(vm_symbol=symbol, seq=seq,
                     trade_symbol=payload.get("trade_symbol") or symbol,
                     b_s=payload.get("b_s") or "买")
        db.add(row)
    for f in FIELDS:
        if f in payload and f != "seq":
            setattr(row, f, payload[f])
    db.flush()
    return {"action": action, "id": row.id, "seq": seq}


def delete(db: Session, symbol: str, seq: int) -> bool:
    n = (db.query(VmPlan)
         .filter(VmPlan.vm_symbol == symbol, VmPlan.seq == seq)
         .delete(synchronize_session=False))
    return n > 0


def unplanned_bills_sql(symbol: str) -> str:
    """无剧本成交反查（供 router 直接用 text() 执行）。

    判据：该标的归属的成交里，成交时间早于**任何**剧本建立时间 -> 无剧本成交。
    这是"先斩后奏 or 事后补剧本"的唯一可算证据，文本永远算不出来。

    口径：`attachable_sql('b')` 与归属计数同源 —— 资金流水行（RZRQ/CRJ/SYFY/cash）
    即便将来被误挂 vm_symbol，也不会算进「该标的的成交」。
    """
    return f"""
        SELECT b.id, b.trade_time, b.category, b.symbol, b.name, b.b_s, b.o_c, b.price, b.vol,
               MIN(p.create_time) AS first_plan_at
        FROM bills_bill b
        JOIN bills_group_symbol g ON g.category = b.category AND g.symbol = b.symbol
        LEFT JOIN irs_vm_plan p ON p.vm_symbol = g.vm_symbol
        WHERE g.vm_symbol = :symbol
          AND {attachable_sql('b')}
        GROUP BY b.id
        HAVING first_plan_at IS NULL OR b.trade_time < DATE(first_plan_at)
        ORDER BY b.trade_time DESC
    """


def _to_dict(p: VmPlan, pp: dict) -> dict:
    d = {f: getattr(p, f) for f in FIELDS}
    d.update({
        "id": p.id, "vm_symbol": p.vm_symbol,
        "create_time": p.create_time, "update_time": p.update_time,
        "status": derive_status(p),
        "effective_trigger": effective_trigger(p, pp),
        "trigger_ref_value": (float(pp[p.trigger_ref])
                              if p.trigger_ref and pp.get(p.trigger_ref) is not None else None),
    })
    return d

