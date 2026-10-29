# -*- coding: utf-8 -*-
"""标的复盘（irs_vm_review）业务函数。

口径：
- 复盘锚定「研究版本」：一次复盘回答「这版研究的结论兑现了没有」；
- 同一版本不限条数（持仓中、平仓后、季度末都可复盘），故不设唯一键；
- review_date 只在这里写 = 落笔当天，前端不可传，防止"当时写的"被伪造；
- 修改复盘不改 review_date（保留落笔时间），修改痕迹看 update_time；
- 复盘不挂剧本、不参与盈亏归因（归因走笔记的 plan_id）。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from server_fast.app.irs.models import VmResearch, VmReview


def list_by_symbol(db: Session, symbol: str) -> list[VmReview]:
    """按落笔日期倒序：最新复盘在前，同一日期按 id 倒序兜底。"""
    return (db.query(VmReview)
            .filter(VmReview.symbol == symbol)
            .order_by(VmReview.review_date.desc(), VmReview.id.desc())
            .all())


def list_by_research(db: Session, research_id: int) -> list[VmReview]:
    return (db.query(VmReview)
            .filter(VmReview.research_id == research_id)
            .order_by(VmReview.review_date.desc(), VmReview.id.desc())
            .all())


def _validate(db: Session, symbol: str, research_id: int, content: Optional[str]) -> str:
    research = db.get(VmResearch, research_id)
    if research is None:
        raise ValueError(f"研究版本不存在：{research_id}")
    if research.symbol != symbol:
        raise ValueError("复盘所选研究版本不属于该标的")
    text = (content or "").strip()
    if not text:
        raise ValueError("复盘正文不能为空")
    return text


def create(db: Session, symbol: str, research_id: int, content: Optional[str]) -> VmReview:
    text = _validate(db, symbol, research_id, content)
    row = VmReview(symbol=symbol, research_id=research_id, content=text,
                   review_date=date.today())
    db.add(row)
    db.flush()
    return row


def update(db: Session, review_id: int, research_id: int,
           content: Optional[str]) -> Optional[VmReview]:
    row = db.get(VmReview, review_id)
    if row is None:
        return None
    text = _validate(db, row.symbol, research_id, content)
    row.research_id = research_id
    row.content = text
    db.flush()
    return row


def delete(db: Session, review_id: int) -> bool:
    row = db.get(VmReview, review_id)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
