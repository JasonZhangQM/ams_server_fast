# -*- coding: utf-8 -*-
"""标的研究定稿（irs_vm_research）：按 research_date 留版，1:N。

语义与档位版本同构：选新日期 = 新建，选历史日期 = 改那一版。
区分两种改动的责任在前端（"录错"该改、"观点变了"该新建），后端只按日期 upsert。

长度硬约束（对应知识库 0101 模板的边界）：thesis/max_risk/pre_condition <= 200 字，
research_brief <= 800 字，research_log <= 2000 字 —— 超了说明该写报告，不是写系统。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from server_fast.app.irs.models import ValueMonitor, VmResearch

FIELDS = ("thesis", "conclusion", "max_risk", "expectation_gap", "pre_condition",
          "next_review", "research_note", "research_brief", "research_log")
LIMITS = {"thesis": 200, "max_risk": 200, "pre_condition": 200,
          "research_brief": 800, "research_log": 2000,
          "expectation_gap": 2000, "research_note": 128}


def _validate(payload: dict) -> None:
    for f, limit in LIMITS.items():
        v = payload.get(f)
        if isinstance(v, str) and len(v) > limit:
            raise ValueError(f"{f} 超过 {limit} 字（当前 {len(v)} 字）：系统只存结论摘要，正文写进知识库报告")


def upsert(db: Session, symbol: str, research_date: date, payload: dict) -> dict:
    if db.query(ValueMonitor.symbol).filter(ValueMonitor.symbol == symbol).first() is None:
        raise ValueError(f"标的 {symbol} 不在估值监测里")
    _validate(payload)

    row = (db.query(VmResearch)
           .filter(VmResearch.symbol == symbol, VmResearch.research_date == research_date)
           .one_or_none())
    action = "created" if row is None else "updated"
    if row is None:
        row = VmResearch(symbol=symbol, research_date=research_date)
        db.add(row)
    old_conclusion = row.conclusion if action == "updated" else None
    for f in FIELDS:
        if f in payload:
            setattr(row, f, payload[f])
    db.flush()
    return {
        "action": action,
        "id": row.id,
        "research_date": research_date,
        "conclusion_old": old_conclusion,
        "conclusion_new": row.conclusion,
    }


def list_by_symbol(db: Session, symbol: str) -> list[dict]:
    rows = (db.query(VmResearch).filter(VmResearch.symbol == symbol)
            .order_by(VmResearch.research_date.desc()).all())
    return [_to_dict(r) for r in rows]


def latest(db: Session, symbol: str, as_of: Optional[date] = None) -> Optional[dict]:
    """<= as_of 的最新一版（未来日不生效，与档位同一口径）。"""
    as_of = as_of or date.today()
    row = (db.query(VmResearch)
           .filter(VmResearch.symbol == symbol, VmResearch.research_date <= as_of)
           .order_by(VmResearch.research_date.desc()).first())
    return _to_dict(row) if row else None


def latest_map(db: Session, symbols: list[str]) -> dict[str, dict]:
    """批量取每标的的最新研究（8 行数据，逐标的查比写相关子查询更清楚）。"""
    return {s: latest(db, s) for s in symbols}


def delete(db: Session, symbol: str, research_date: date) -> bool:
    n = (db.query(VmResearch)
         .filter(VmResearch.symbol == symbol, VmResearch.research_date == research_date)
         .delete(synchronize_session=False))
    return n > 0


def _to_dict(r: VmResearch) -> dict:
    d = {f: getattr(r, f) for f in FIELDS}
    d.update({"id": r.id, "symbol": r.symbol, "research_date": r.research_date,
              "create_time": r.create_time, "update_time": r.update_time})
    return d
