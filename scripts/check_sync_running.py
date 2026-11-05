# -*- coding: utf-8 -*-
"""查 `bds_sync_task` 是否有 running 任务（🔒4.3：改后端 .py / 落笔前必查）。

用法：
    conda run -n fast311 python server_fast/scripts/check_sync_running.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.common.db import SessionLocal


def main() -> None:
    with SessionLocal() as db:
        running = db.execute(text(
            "SELECT id, scope, status, start_time, progress, update_time "
            "FROM bds_sync_task WHERE status='running' ORDER BY id")).all()
        if not running:
            print("running = 0，可以安全改后端代码")
        else:
            print(f"⚠️ running = {len(running)}，暂勿改后端 .py：")
            for r in running:
                print(f"  #{r[0]} scope={r[1]} progress={r[4]} "
                      f"start={r[3]} update={r[5]}")
        print("\n最近 3 条任务：")
        for r in db.execute(text(
                "SELECT id, scope, status, start_time, end_time "
                "FROM bds_sync_task ORDER BY id DESC LIMIT 3")).all():
            print(f"  #{r[0]} {r[1]:14s} {r[2]:8s} {r[3]} → {r[4]}")


if __name__ == "__main__":
    main()
