# -*- coding: utf-8 -*-
"""清理僵尸任务（复用 bds.sync_task_utils.cleanup_zombies 的官方口径）。

2026-11-05：任务 #272（scope=all，00:00 启动）因 uvicorn --reload 重载被杀，
停在 progress=36.16%。本脚本按项目既有规则（running 超 5 分钟未更新 ⇒ failed）
把它置为 failed，避免继续占用全库任务锁。

用法：
    conda run -n fast311 python server_fast/scripts/fix_zombie_20261105.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.app.bds.sync_task_utils import cleanup_zombies
from server_fast.common.db import SessionLocal


def main() -> None:
    with SessionLocal() as db:
        print("清理前：")
        for r in db.execute(text(
                "SELECT id, scope, status, progress, update_time "
                "FROM bds_sync_task WHERE status='running'")).all():
            print(f"  #{r[0]} {r[1]} {r[2]} progress={r[3]} update={r[4]}")
        n = cleanup_zombies(db, minutes=5)
        print(f"cleanup_zombies 处理 {n} 条")
        left = db.execute(text(
            "SELECT COUNT(*) FROM bds_sync_task WHERE status='running'")).scalar()
        print(f"清理后 running = {left}")


if __name__ == "__main__":
    main()
