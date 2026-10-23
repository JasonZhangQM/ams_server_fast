# -*- coding: utf-8 -*-
"""成交笔记（bills_bill_note）业务函数。

两条硬约束：

1. **biz_key 必须与导入去重同一把尺子**（`Bill.unique_keys`）：
   `account|trade_time|symbol|exec_type|amount_act|id_agree|cash`，金额 quantize 到 0.01。
   两侧（写入时、relink 找回时）都必须走这个函数，否则找回会错配。
2. **content_at 只在这里写，前端不可传**：否则"当时写的"可以被伪造，
   "落笔时间 vs 成交时间"这个反自欺的唯一证据就失效了。
   首次插入 = now()；后续更新**不改 content_at**（保留"当时"，修改痕迹看 update_time）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from server_fast.app.bills.models import Bill, BillNote

BIZ_KEY_FIELDS = ("account", "trade_time", "symbol", "exec_type", "amount_act", "id_agree", "cash")


def bill_biz_key(bill: Bill) -> str:
    """成交自然键 md5 —— 与 bills_bill 唯一键（uk_bills_bill）同口径。"""
    def f(v):
        if v is None:
            return ""
        if isinstance(v, Decimal):
            return str(v.quantize(Decimal("0.01")))
        return str(v)

    raw = "|".join(f(getattr(bill, k)) for k in BIZ_KEY_FIELDS)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def get_notes(db: Session, bill_id: int) -> dict[str, BillNote]:
    return {n.kind: n for n in db.query(BillNote).filter(BillNote.bill_id == bill_id).all()}


def upsert_note(db: Session, bill: Bill, kind: str,
                content: Optional[str]) -> dict:
    """写一条笔记。content 为空串 = 删除该条（不留空壳）。"""
    if kind not in (BillNote.KIND_IDEA, BillNote.KIND_REVIEW):
        raise ValueError(f"kind 只能是 {BillNote.KIND_IDEA}/{BillNote.KIND_REVIEW}")
    row = (db.query(BillNote)
           .filter(BillNote.bill_id == bill.id, BillNote.kind == kind)
           .one_or_none())
    text = (content or "").strip()

    if not text:
        if row is None:
            return {"action": "noop", "kind": kind}
        db.delete(row)
        db.flush()
        return {"action": "deleted", "kind": kind}

    if row is None:
        row = BillNote(bill_id=bill.id, biz_key=bill_biz_key(bill),
                       kind=kind, content=text, content_at=datetime.now())
        db.add(row)
        db.flush()
        return {"action": "created", "kind": kind, "id": row.id, "content_at": row.content_at}

    row.content = text
    row.biz_key = bill_biz_key(bill)  # 成交行若被修正过，顺手对齐
    db.flush()
    return {"action": "updated", "kind": kind, "id": row.id, "content_at": row.content_at}
