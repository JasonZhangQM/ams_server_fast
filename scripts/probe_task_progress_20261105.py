# -*- coding: utf-8 -*-
"""看 running 任务的细节：是否真的在推进，还是重启残留的僵尸（只读）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.common.db import SessionLocal


def snap(db, tid: int):
    return db.execute(text(
        "SELECT status, progress, done_count, total_count, success_count, "
        "       error_count, update_time, scope_params "
        "FROM bds_sync_task WHERE id=:i"), {"i": tid}).mappings().first()


def main() -> None:
    with SessionLocal() as db:
        tid = int(db.execute(text(
            "SELECT id FROM bds_sync_task WHERE status='running' "
            "ORDER BY id DESC LIMIT 1")).scalar() or 0)
        if not tid:
            print("无 running 任务")
            return
        a = snap(db, tid)
        print(f"#{tid} 第一次取样：progress={a['progress']} "
              f"done={a['done_count']}/{a['total_count']} "
              f"success={a['success_count']} error={a['error_count']} "
              f"update={a['update_time']}")
        print(f"  scope_params={a['scope_params']}")
    print("等 60s 再取一次…")
    time.sleep(60)
    with SessionLocal() as db:
        b = snap(db, tid)
        print(f"#{tid} 第二次取样：progress={b['progress']} "
              f"done={b['done_count']}/{b['total_count']} "
              f"success={b['success_count']} error={b['error_count']} "
              f"update={b['update_time']}")
    same = (a["progress"] == b["progress"] and a["update_time"] == b["update_time"])
    print("结论：", "⚠️ 60s 无任何推进 ⇒ 疑似僵尸/卡死" if same else "✅ 仍在推进")


if __name__ == "__main__":
    main()
