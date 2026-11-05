# -*- coding: utf-8 -*-
"""`irs_um_percentile` 哨兵实验（🔒6.6，方案 §7.2）。

三个必须验证的行为：
1. **重算不覆盖人工值**：手写 `note` 后跑 refresh，`note` 必须原样保留；
2. **纯镜像**：手插一条不在监测表的行，refresh 后必须被删（`rows_deleted=1`）；
3. **upsert 而不是 insert**：把某行 `as_of` 改成 2000-01-01，refresh 后必须被纠正，
   行数不变（证明走的是更新路径，不是又插一行）。

脚本自带清理（finally 恢复 note、删测试行），可重复跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.app.irs.service import um_percentile
from server_fast.common.db import SessionLocal

FAKE = "FAKE.NOT_IN_MONITOR"
ok_all = True


def mark(cond: bool, msg: str) -> None:
    global ok_all
    ok_all = ok_all and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


def main() -> None:
    with SessionLocal() as db:
        subject = db.execute(text(
            "SELECT symbol FROM irs_um_percentile ORDER BY symbol LIMIT 1")).scalar()
        if not subject:
            print("表为空，先跑 probe_um_pct_latest_20261105.py")
            return
        before_rows = db.execute(text("SELECT COUNT(*) FROM irs_um_percentile")).scalar()
        before_as_of = db.execute(text(
            "SELECT as_of FROM irs_um_percentile WHERE symbol=:s"), {"s": subject}).scalar()
    print(f"基线：{before_rows} 行，哨兵标的 {subject}（as_of={before_as_of}）")

    try:
        # ---- 1. 人工列不被覆盖 ----
        print("\n== 1. note 不被重算覆盖 ==")
        with SessionLocal() as db:
            db.execute(text("UPDATE irs_um_percentile SET note='sentinel' WHERE symbol=:s"),
                       {"s": subject})
            db.commit()
        um_percentile.refresh()
        with SessionLocal() as db:
            note = db.execute(text(
                "SELECT note FROM irs_um_percentile WHERE symbol=:s"), {"s": subject}).scalar()
        mark(note == "sentinel", f"note 保留为 {note!r}（期望 'sentinel'）")

        # ---- 2. 范围外行被删除 ----
        print("\n== 2. 非监测表标的被清理（纯镜像）==")
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO irs_um_percentile (subject_type, symbol, as_of, calc_at) "
                "VALUES ('stock', :s, :d, NOW())"), {"s": FAKE, "d": before_as_of})
            db.commit()
        r = um_percentile.refresh()
        with SessionLocal() as db:
            left = db.execute(text(
                "SELECT COUNT(*) FROM irs_um_percentile WHERE symbol=:s"), {"s": FAKE}).scalar()
        mark(r["rows_deleted"] == 1 and left == 0,
             f"rows_deleted={r['rows_deleted']}，残留 {left} 行")

        # ---- 3. upsert 而非 insert ----
        print("\n== 3. 脏 as_of 被就地纠正（upsert）==")
        with SessionLocal() as db:
            db.execute(text(
                "UPDATE irs_um_percentile SET as_of='2000-01-01' WHERE symbol=:s"),
                {"s": subject})
            db.commit()
        n_before = 0
        with SessionLocal() as db:
            n_before = db.execute(text("SELECT COUNT(*) FROM irs_um_percentile")).scalar()
        r = um_percentile.refresh()
        with SessionLocal() as db:
            after_as_of, n_after = db.execute(text(
                "SELECT as_of, (SELECT COUNT(*) FROM irs_um_percentile) "
                "FROM irs_um_percentile WHERE symbol=:s"), {"s": subject}).first()
        mark(after_as_of == before_as_of and n_after == n_before,
             f"as_of 纠正为 {after_as_of}，行数 {n_before} → {n_after}，"
             f"本次新增 {r['rows_appeared']}（期望 0）")

    finally:
        with SessionLocal() as db:
            db.execute(text("UPDATE irs_um_percentile SET note=NULL WHERE note='sentinel'"))
            db.execute(text("DELETE FROM irs_um_percentile WHERE symbol=:s"), {"s": FAKE})
            db.commit()
        print("\n清理完成（note 复位、测试行删除）")

    print("\n" + ("✅ 哨兵全部通过" if ok_all else "❌ 有哨兵失败"))


if __name__ == "__main__":
    main()
