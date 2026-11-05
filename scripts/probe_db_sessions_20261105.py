# -*- coding: utf-8 -*-
"""只读：dump MySQL 当前会话（判 running 任务是否还活着）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.common.db import SessionLocal


def main() -> None:
    with SessionLocal() as db:
        rows = db.execute(text("SHOW FULL PROCESSLIST")).all()
        print(f"会话数 = {len(rows)}")
        for r in rows:
            # Id, User, Host, db, Command, Time, State, Info
            print(f"  id={r[0]} user={r[1]} host={r[2]} cmd={r[4]} "
                  f"time={r[5]} state={r[6]} info={str(r[7])[:90] if len(r) > 7 else ''}")


if __name__ == "__main__":
    main()
