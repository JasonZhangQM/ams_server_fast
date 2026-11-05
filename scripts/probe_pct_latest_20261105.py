# -*- coding: utf-8 -*-
"""最新截面分位表（irs_um_percentile）落地前的只读盘点（2026-11-05）。

只查不写：
1. 目标表名是否已被占用（irs_um_percentile / irs_valuation_pct_latest）；
2. 标的监测表当前标的清单；
3. 历史表 `bds_valuation_percentile` 每标的最新一行（新表的验收基线）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.common.db import SessionLocal

CANDIDATES = ("irs_um_percentile", "irs_valuation_pct_latest",
              "irs_valuation_percentile", "irs_um_pct_latest")


def main() -> None:
    with SessionLocal() as db:
        print("== 1. 目标表名占用 ==")
        for name in CANDIDATES:
            n = db.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=:n"), {"n": name}).scalar()
            print(f"  {name:28s} {'已存在' if n else '不存在'}")

        print("\n== 2. 标的监测表 ==")
        rows = db.execute(text(
            "SELECT symbol, name, price FROM irs_underlying_monitor ORDER BY symbol")).all()
        for r in rows:
            print(f"  {r[0]:14s} {str(r[1] or ''):10s} price={r[2]}")
        print(f"  合计 {len(rows)} 行")

        print("\n== 3. 历史表每标的最新一行（验收基线）==")
        rows = db.execute(text(
            "SELECT p.subject_type, p.symbol, p.as_of, p.current_pe, p.current_pb, "
            "       p.pe_pct_3y, p.pe_pct_5y, p.pe_pct_10y, "
            "       p.pb_pct_3y, p.pb_pct_5y, p.pb_pct_10y, "
            "       p.status_pe, p.status_pb, p.calc_at "
            "FROM bds_valuation_percentile p "
            "JOIN (SELECT subject_type, symbol, MAX(as_of) mx "
            "      FROM bds_valuation_percentile GROUP BY subject_type, symbol) m "
            "  ON m.subject_type=p.subject_type AND m.symbol=p.symbol AND m.mx=p.as_of "
            "ORDER BY p.subject_type, p.symbol")).all()
        for r in rows:
            print(f"  {r[0]:6s} {r[1]:14s} as_of={r[2]} pe={r[3]} pb={r[4]} "
                  f"pe(3/5/10)={r[5]}/{r[6]}/{r[7]} pb(3/5/10)={r[8]}/{r[9]}/{r[10]} "
                  f"st={r[11]}/{r[12]} calc_at={r[13]}")
        print(f"  合计 {len(rows)} 行")

        print("\n== 4. 上游源末日 ==")
        for t in ("bds_index_valuation", "bds_daily_valuation"):
            d = db.execute(text(
                f"SELECT trade_date FROM {t} ORDER BY trade_date DESC LIMIT 1")).scalar()
            print(f"  {t:24s} {d}")


if __name__ == "__main__":
    main()
