# -*- coding: utf-8 -*-
"""成交笔记找回（v3 §8.1）：给 `bills_bill_note.bill_id IS NULL` 的孤儿重新挂上成交。

为什么需要它：既有的「删账户末笔 → 重新导入」流程里，被删的那笔会以**新 id** 回来
（导入按 `trade_time > MAX` 增量过滤），而笔记外键是 `ON DELETE SET NULL`。
不做找回的话，那笔成交的想法会静默消失——没有任何地方会报错。

三条硬约束：
1. `biz_key` 必须与 `Bill.unique_keys` 同口径，**同一个函数** `bill_biz_key()` 生成
   （铁律 6.9：集合指纹禁 `MD5(GROUP_CONCAT())`，一律 Python 侧算）；
2. **命中多条不写**，只报告——宁可人工也不猜；
3. 默认 dry-run，`--apply` 才落库。

用法：
    conda run -n fast311 python scripts/relink_bill_notes.py           # 只看
    conda run -n fast311 python scripts/relink_bill_notes.py --apply   # 落库
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

sys.path.insert(0, r"C:\quant_man\mysite_quant\ams_trae")

from server_fast.common.db import SessionLocal
from server_fast.app.bills.models import Bill, BillNote
from server_fast.app.bills.services.bill_note import bill_biz_key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        orphans = (db.query(BillNote).filter(BillNote.bill_id.is_(None))
                   .order_by(BillNote.id).all())
        print(f"[1] 孤儿笔记（bill_id IS NULL）：{len(orphans)} 条")
        if not orphans:
            print("    无需找回。")
            return 0

        # 全量成交的 biz_key -> [bill.id]（Python 侧算，不走 SQL 指纹）
        index: dict[str, list[int]] = defaultdict(list)
        n_bills = 0
        for b in db.query(Bill).all():
            index[bill_biz_key(b)].append(b.id)
            n_bills += 1
        print(f"[2] 已为 {n_bills} 笔成交建索引，唯一 biz_key {len(index)} 个")

        linked = ambiguous = missing = 0
        for n in orphans:
            hits = index.get(n.biz_key, [])
            if len(hits) == 1:
                print(f"    笔记 {n.id}（{n.kind}）biz_key={n.biz_key[:8]}… "
                      f"-> 成交 {hits[0]}  「{n.content[:20]}…」")
                if args.apply:
                    n.bill_id = hits[0]
                linked += 1
            elif len(hits) > 1:
                print(f"    笔记 {n.id}（{n.kind}）命中 {len(hits)} 笔成交 {hits} -> **只报告不写**")
                ambiguous += 1
            else:
                print(f"    笔记 {n.id}（{n.kind}）biz_key={n.biz_key[:8]}… 无对应成交"
                      f"（原成交可能被永久删除）-> 保留孤儿")
                missing += 1

        if args.apply and linked:
            db.commit()
            print(f"[3] 已提交：{linked} 条找回")
        elif linked:
            print(f"[3] dry-run：可找回 {linked} 条（加 --apply 才落库）")

        print(f"汇总：可找回 {linked} / 歧义 {ambiguous} / 无对应 {missing}")
        return 0 if ambiguous == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
