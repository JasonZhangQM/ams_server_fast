# -*- coding: utf-8 -*-
"""成交笔记（bills_bill_note）业务函数。

只承载「想法」（下单当时的决策笔记）。复盘自 2026-10-29 起上移到标的层
（irs_vm_review），不再逐笔记。

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


def _derive_source(bill: Bill) -> str:
    """按「落笔时间 vs 成交时间」判定笔记来源。

    > 3 天 = 事后补录（backfill），否则 = 成交当日或次日写（manual）。
    """
    if bill.trade_time is None:
        return "manual"
    return "backfill" if (datetime.now() - bill.trade_time).days > 3 else "manual"


def upsert_note(db: Session, bill: Bill, kind: str,
                content: Optional[str], plan_id: Optional[int] = None) -> dict:
    """整体保存一条笔记（想法 + 剧本关联）。

    语义（前端「记一笔」弹窗一次性提交两项字段）：
    - 正文与剧本都为空 → 删除该条（不留空壳）；
    - 正文为空但选了剧本 → 允许落库（content=''），只记「执行了哪条剧本」；
    - 正文非空 → 同时覆盖 plan_id（含传 None=解除关联，这是唯一的手动修改入口）。
    """
    if kind != BillNote.KIND_IDEA:
        raise ValueError(f"成交笔记只支持「{BillNote.KIND_IDEA}」；复盘请到标的层")
    row = (db.query(BillNote)
           .filter(BillNote.bill_id == bill.id, BillNote.kind == kind)
           .one_or_none())
    text = (content or "").strip()

    # 两项都空 = 这条笔记没有存在意义
    if not text and plan_id is None:
        if row is None:
            return {"action": "noop", "kind": kind}
        db.delete(row)
        db.flush()
        return {"action": "deleted", "kind": kind}

    if row is None:
        row = BillNote(bill_id=bill.id, biz_key=bill_biz_key(bill),
                       kind=kind, content=text, content_at=datetime.now(),
                       source=_derive_source(bill), plan_id=plan_id)
        db.add(row)
        db.flush()
        return {"action": "created", "kind": kind, "id": row.id, "content_at": row.content_at,
                "source": row.source, "plan_id": row.plan_id}

    row.content = text
    row.biz_key = bill_biz_key(bill)  # 成交行若被修正过，顺手对齐
    row.plan_id = plan_id  # 整体保存：允许清空关联，不保留旧值
    db.flush()
    return {"action": "updated", "kind": kind, "id": row.id, "content_at": row.content_at,
            "source": row.source, "plan_id": row.plan_id}
