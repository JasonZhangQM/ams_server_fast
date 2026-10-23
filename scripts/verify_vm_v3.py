# -*- coding: utf-8 -*-
"""v3 综合哨兵：表结构 / 档位回写 / 笔记孤儿 / 重算不动 vm_symbol

用法：
    conda run -n fast311 python scripts/verify_vm_v3.py                 # 只读检查
    conda run -n fast311 python scripts/verify_vm_v3.py --with-recalc   # 追加"跑 3 次重算"验证

`--with-recalc` 会真的执行 upsert_group_symbol_sql()（纯 SQL 汇总，不联网），
用来实证「手动列不会被派生重算覆盖」（方案验收 4 / 17）。
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, r"C:\quant_man\mysite_quant\ams_trae")

from sqlalchemy import text

from server_fast.common.db import SessionLocal

fails = []


def check(ok: bool, msg: str, extra: str = "") -> None:
    print(f"  {'OK ' if ok else '!! '} {msg}{(' — ' + extra) if extra else ''}")
    if not ok:
        fails.append(msg)


def main(with_recalc: bool) -> None:
    with SessionLocal() as db:
        print("=" * 74)
        print("v3 综合哨兵")
        print("=" * 74)

        # ---------------- 1. 表结构
        print("\n[1] 表结构")
        cols = [r[0] for r in db.execute(text("""
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='bills_group_symbol'
            ORDER BY ORDINAL_POSITION""")).all()]
        gone = [c for c in ("logic", "plan", "invalidate", "review", "theme", "note_at") if c in cols]
        check(not gone, "bills_group_symbol 的 6 个笔记列全消失", f"残留={gone}")
        check("vm_symbol" in cols, "vm_symbol 存在")
        tbl = db.execute(text("SELECT COUNT(*) FROM information_schema.TABLES "
                              "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='bills_group_symbol_note_log'")).scalar()
        check(tbl == 0, "bills_group_symbol_note_log 已删除")

        # ---------------- 2. 档位：主表 pp_* ≡ 当前生效版
        print("\n[2] 主表 pp_* ≡ 当前生效档位版")
        rows = db.execute(text("""
            SELECT v.symbol, v.pp_el, v.pp_l, v.pp_m, v.pp_h, v.pp_eh,
                   (SELECT x.pp_el FROM irs_vm_version x WHERE x.symbol=v.symbol
                     AND x.effective_date <= CURDATE()
                     ORDER BY x.effective_date DESC LIMIT 1) ve,
                   (SELECT x.pp_l FROM irs_vm_version x WHERE x.symbol=v.symbol
                     AND x.effective_date <= CURDATE()
                     ORDER BY x.effective_date DESC LIMIT 1) vl,
                   (SELECT x.pp_m FROM irs_vm_version x WHERE x.symbol=v.symbol
                     AND x.effective_date <= CURDATE()
                     ORDER BY x.effective_date DESC LIMIT 1) vm,
                   (SELECT x.pp_h FROM irs_vm_version x WHERE x.symbol=v.symbol
                     AND x.effective_date <= CURDATE()
                     ORDER BY x.effective_date DESC LIMIT 1) vh,
                   (SELECT x.pp_eh FROM irs_vm_version x WHERE x.symbol=v.symbol
                     AND x.effective_date <= CURDATE()
                     ORDER BY x.effective_date DESC LIMIT 1) vhe
            FROM irs_value_monitor v ORDER BY v.symbol""")).mappings().all()
        mism = [r["symbol"] for r in rows
                if (r["pp_el"], r["pp_l"], r["pp_m"], r["pp_h"], r["pp_eh"])
                != (r["ve"], r["vl"], r["vm"], r["vh"], r["vhe"])]
        check(not mism, f"{len(rows)} 个标的的档位缓存与生效版一致", f"不一致={mism}")

        # ---------------- 3. 笔记孤儿
        print("\n[3] 交易笔记孤儿")
        orphan = db.execute(text("""
            SELECT n.id, n.bill_id, n.kind FROM bills_bill_note n
            LEFT JOIN bills_bill b ON b.id = n.bill_id
            WHERE n.bill_id IS NULL OR b.id IS NULL""")).mappings().all()
        total_note = db.execute(text("SELECT COUNT(*) FROM bills_bill_note")).scalar()
        check(len(orphan) == 0, f"笔记 {total_note} 条，孤儿 {len(orphan)} 条",
              str([dict(o) for o in orphan]) if orphan else "")

        # ---------------- 4. 研究无未来空洞
        print("\n[4] 研究 / 档位版本日期")
        fut = db.execute(text("""
            SELECT symbol, research_date FROM irs_vm_research WHERE research_date > CURDATE()
            UNION ALL
            SELECT symbol, effective_date FROM irs_vm_version WHERE effective_date > CURDATE()
        """)).mappings().all()
        print(f"     未来生效的版本/研究 {len(fut)} 条（允许，但列表视图不生效）")

        # ---------------- 5. 重算不动 vm_symbol
        if with_recalc:
            print("\n[5] 跑 3 次 upsert_group_symbol_sql()，验证 vm_symbol 不被覆盖")
            from server_fast.app.bills.services.group_summary import upsert_group_symbol_sql
            before = db.execute(text(
                "SELECT id, vm_symbol FROM bills_group_symbol ORDER BY id")).all()
            before_d = {r[0]: r[1] for r in before}
            for i in range(3):
                upsert_group_symbol_sql()
                print(f"     第 {i + 1} 次完成")
            db.expire_all()
            after = db.execute(text(
                "SELECT id, vm_symbol FROM bills_group_symbol ORDER BY id")).all()
            after_d = {r[0]: r[1] for r in after}
            diff = {k: (before_d.get(k), after_d.get(k)) for k in set(before_d) | set(after_d)
                    if before_d.get(k) != after_d.get(k)}
            check(not diff, f"vm_symbol 逐行不变（{len(before_d)} → {len(after_d)} 行）", str(diff))
            new_rows = set(after_d) - set(before_d)
            check(all(after_d[k] is None for k in new_rows),
                  f"新插入行 vm_symbol 为 NULL（{len(new_rows)} 行）")
        else:
            print("\n[5] 跳过重算验证（加 --with-recalc 才跑）")

    print("\n" + "=" * 74)
    print("结果:", "全部通过" if not fails else f"{len(fails)} 项失败：{fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-recalc", action="store_true")
    main(ap.parse_args().with_recalc)
