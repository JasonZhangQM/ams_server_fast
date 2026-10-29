# -*- coding: utf-8 -*-
"""标的 ↔ 成交的归属查询（映射存在 bills_group_symbol.vm_symbol）。

一条硬规则（v3 §4.5.1 规则一 + 2026-10-23 用户补充）：**资金流水行整体不参与归属**。
`RZRQ`（融资融券）/ `CRJ`（出入金）/ `SYFY`（收益费用）/ `cash` 以及理财逆回购，
symbol 只是流水占位代码而非证券，给它们挂标的不可能产生任何「该标的的成交」。
所有反查都排除这些行——否则 285 笔融资融券会淹掉真正漏配的那几笔。

口径唯一来源：`app/bills/constants.py::attachable_sql`。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.app.bills.constants import attachable_sql, non_attachable_sql


def _trade_filters(
    plan_id: int | None = None,
    account: str | None = None,
    symbol: str | None = None,
) -> tuple[str, dict]:
    """剧本 / 账户 / 合约三个筛选条件的公共拼装（列表与计数必须共用，避免口径漂移）。"""
    conds = ""
    params: dict = {}
    if plan_id is not None:
        conds += " AND n.kind = '想法' AND n.plan_id = :plan"
        params["plan"] = plan_id
    if account:
        conds += " AND b.account = :acc"
        params["acc"] = account
    if symbol:
        conds += " AND b.symbol LIKE :sym"
        params["sym"] = f"%{symbol}%"
    return conds, params


def trades_of(
    db: Session,
    vm_symbol: str,
    limit: int = 500,
    plan_id: int | None = None,
    account: str | None = None,
    symbol: str | None = None,
    offset: int = 0,
) -> list[dict]:
    """标的页成交分页：一次 JOIN，没有 OR、没有名称匹配。

    ⚠️ 笔记按 bill_id 左连（只含「想法」一条）。若直接 JOIN 再 LIMIT，
    多笔记成交可能挤出别的成交上限；这里条件聚合压成两列，保证「一行 = 一笔成交」。
    plan_id / account / symbol 为前端筛选用：剧本精确、账户精确、合约模糊。
    """
    conds, params = _trade_filters(plan_id, account, symbol)
    params["vm"] = vm_symbol
    params["lim"] = limit
    params["off"] = offset
    sql = text(f"""
        SELECT b.id, b.trade_time, b.account, b.category, b.symbol, b.name,
               b.b_s, b.o_c, b.price, b.vol, b.amount,
               g.vm_symbol,
               MAX(CASE WHEN n.kind = '想法' THEN n.content END) AS note_idea,
               MAX(CASE WHEN n.kind = '想法' THEN n.content_at END) AS note_idea_at,
               MAX(CASE WHEN n.kind = '想法' THEN n.plan_id END) AS plan_id
        FROM bills_bill b
        JOIN bills_group_symbol g
          ON g.category = b.category AND g.symbol = b.symbol
        LEFT JOIN bills_bill_note n ON n.bill_id = b.id
        WHERE g.vm_symbol = :vm
          AND {attachable_sql('b')}{conds}
        GROUP BY b.id, b.trade_time, b.account, b.category, b.symbol, b.name,
                 b.b_s, b.o_c, b.price, b.vol, b.amount, g.vm_symbol
        ORDER BY b.trade_time DESC, b.id DESC
        LIMIT :lim OFFSET :off
    """)
    rows = db.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


def count_trades(
    db: Session,
    vm_symbol: str,
    plan_id: int | None = None,
    account: str | None = None,
    symbol: str | None = None,
) -> int:
    """与 trades_of 同口径的总数，供分页器算页数。"""
    conds, params = _trade_filters(plan_id, account, symbol)
    params["vm"] = vm_symbol
    sql = text(f"""
        SELECT COUNT(DISTINCT b.id)
        FROM bills_bill b
        JOIN bills_group_symbol g
          ON g.category = b.category AND g.symbol = b.symbol
        LEFT JOIN bills_bill_note n ON n.bill_id = b.id
        WHERE g.vm_symbol = :vm
          AND {attachable_sql('b')}{conds}
    """)
    return int(db.execute(sql, params).scalar_one())


def _group_filters(category: str | None, symbol: str | None) -> tuple[str, dict]:
    conds = ""
    params: dict = {}
    if category:
        conds += " AND g.category = :cat"
        params["cat"] = category
    if symbol:
        conds += " AND g.symbol LIKE :sym"
        params["sym"] = f"%{symbol}%"
    return conds, params


def groups_of(
    db: Session,
    vm_symbol: str,
    limit: int = 10,
    offset: int = 0,
    category: str | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """该标的的合约行分页（id 倒序）；分类精确、合约模糊。"""
    conds, params = _group_filters(category, symbol)
    params.update({"s": vm_symbol, "lim": limit, "off": offset})
    sql = text(f"""
        SELECT g.id, g.category, g.symbol, g.count, g.pf_total,
               (SELECT COUNT(*) FROM bills_bill b
                 WHERE b.category = g.category AND b.symbol = g.symbol) AS bill_rows
        FROM bills_group_symbol g
        WHERE g.vm_symbol = :s{conds}
        ORDER BY g.id DESC
        LIMIT :lim OFFSET :off
    """)
    return [dict(r) for r in db.execute(sql, params).mappings().all()]


def count_groups(
    db: Session,
    vm_symbol: str,
    category: str | None = None,
    symbol: str | None = None,
) -> int:
    """与 groups_of 同口径的合约行总数。"""
    conds, params = _group_filters(category, symbol)
    params["s"] = vm_symbol
    sql = text(f"""
        SELECT COUNT(*) FROM bills_group_symbol g
        WHERE g.vm_symbol = :s{conds}
    """)
    return int(db.execute(sql, params).scalar_one())


def coverage(db: Session) -> list[dict]:
    """每个标的的归属覆盖：合约行数 + 成交笔数（同代码命中 + 显式映射）。"""
    sql = text(f"""
        SELECT v.symbol AS vm_symbol, v.name,
               COUNT(DISTINCT g.id) AS group_rows,
               COUNT(DISTINCT b.id) AS bill_rows
        FROM irs_value_monitor v
        LEFT JOIN bills_group_symbol g
               ON g.vm_symbol = v.symbol OR g.symbol = v.symbol
        LEFT JOIN bills_bill b
               ON b.category = g.category AND b.symbol = g.symbol
              AND {attachable_sql('b')}
        GROUP BY v.symbol, v.name
        ORDER BY bill_rows DESC, v.symbol
    """)
    return [dict(r) for r in db.execute(sql).mappings().all()]


def unmapped(db: Session, limit: int = 300) -> list[dict]:
    """未归属的合约行（**排除资金流水行**，也排除"同代码自动命中"的行）。

    三态语义（v3 §6.4）：
    - 资金流水行（RZRQ/CRJ/SYFY/cash/理财）-> '—'（本来就不归属，不是漏配）
    - 同代码（g.symbol 就是某个标的）-> 自动命中，不需要填 vm_symbol
    - 其余且 vm_symbol IS NULL -> 真正未归属，这是可操作清单
    """
    sql = text(f"""
        SELECT g.id, g.category, g.symbol, g.count, g.pf_total,
               (SELECT COUNT(*) FROM bills_bill b
                 WHERE b.category = g.category AND b.symbol = g.symbol) AS bill_rows
        FROM bills_group_symbol g
        WHERE g.vm_symbol IS NULL
          AND {attachable_sql('g')}
          AND g.symbol NOT IN (SELECT symbol FROM irs_value_monitor)
        ORDER BY bill_rows DESC, g.symbol
        LIMIT :lim
    """)
    return [dict(r) for r in db.execute(sql, {"lim": limit}).mappings().all()]


def unmapped_count(db: Session) -> dict:
    """三态计数，加起来等于 total（与 /bills/group-symbols/unmapped-count 同口径）。"""
    sql = text(f"""
        SELECT
          SUM(CASE WHEN {non_attachable_sql()} THEN 1 ELSE 0 END) AS excluded,
          SUM(CASE WHEN {attachable_sql()}
                    AND (vm_symbol IS NOT NULL
                         OR symbol IN (SELECT symbol FROM irs_value_monitor))
                   THEN 1 ELSE 0 END) AS mapped,
          SUM(CASE WHEN {attachable_sql()}
                    AND vm_symbol IS NULL
                    AND symbol NOT IN (SELECT symbol FROM irs_value_monitor)
                   THEN 1 ELSE 0 END) AS unmapped,
          COUNT(*) AS total
        FROM bills_group_symbol
    """)
    r = db.execute(sql).mappings().first()
    return {"mapped": int(r["mapped"] or 0), "unmapped": int(r["unmapped"] or 0),
            "excluded": int(r["excluded"] or 0), "total": int(r["total"] or 0)}
