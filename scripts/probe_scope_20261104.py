# -*- coding: utf-8 -*-
"""只读探针：估值分位「同步标的范围」方案规模盘点（2026-11-04）。

只查数据、不写任何表，用于评估 industry / index / all 三种范围的量级与风险。
运行：conda run -n fast311 python server_fast/scripts/probe_scope_20261104.py
（在 C:/quant_man/mysite_quant/ams_trae/ 下执行）
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.common.db import SessionLocal


def q(db, sql, **params):
    return db.execute(text(sql), params).all()


def main():
    with SessionLocal() as db:
        print("=== 1. bds_symbol_info 中 market IS NULL 的行（非 A 股：指数 / 外盘 / 商品） ===")
        rows = q(db, "SELECT symbol, name, industry, delisted_date "
                     "FROM bds_symbol_info WHERE market IS NULL ORDER BY symbol")
        for r in rows:
            print(f"  {r[0]:<14} {str(r[1] or ''):<12} industry={r[2] or '-'} delisted={r[3] or '-'}")
        print(f"  小计: {len(rows)}")

        print("\n=== 2. 这些代码在 bds_index_valuation 里有没有估值序列 ===")
        idx_rows = q(db, "SELECT index_code, COUNT(*), MIN(trade_date), MAX(trade_date) "
                         "FROM bds_index_valuation GROUP BY index_code ORDER BY index_code")
        idx_codes = {r[0] for r in idx_rows}
        for r in idx_rows:
            print(f"  {r[0]:<14} rows={r[1]:<7} {r[2]} ~ {r[3]}")
        print(f"  指数估值表 distinct index_code: {len(idx_codes)}")
        print("  表内非A股行 ∩ 指数估值表:", sorted({r[0] for r in rows} & idx_codes))
        print("  表内非A股行 − 指数估值表:", sorted({r[0] for r in rows} - idx_codes))
        print("  指数估值表 − 表内非A股行:", sorted(idx_codes - {r[0] for r in rows}))

        print("\n=== 3. A 股个股口径（market 非空） ===")
        tot = q(db, "SELECT COUNT(*) FROM bds_symbol_info WHERE market IS NOT NULL")[0][0]
        trading = q(db, "SELECT COUNT(*) FROM bds_symbol_info WHERE market IS NOT NULL "
                        "AND (delisted_date IS NULL OR delisted_date > CURDATE())")[0][0]
        print(f"  全部 A 股个股: {tot}；在市: {trading}；已退市: {tot - trading}")
        inds = q(db, "SELECT industry, COUNT(*) c FROM bds_symbol_info "
                     "WHERE market IS NOT NULL GROUP BY industry ORDER BY c DESC")
        print(f"  industry 取值数（含 NULL/空）: {len(inds)}")
        print("  Top 12 行业:")
        for r in inds[:12]:
            print(f"    {str(r[0] or '(NULL)'):<12} {r[1]}")
        print(f"  最大行业标的数: {inds[0][1] if inds else 0}；"
              f"行业标的数中位数≈{inds[len(inds)//2][1] if inds else 0}")

        print("\n=== 4. 指数成份股最新快照规模 ===")
        cons = q(db, "SELECT index_code, MAX(trade_date) FROM bds_index_constituent "
                     "GROUP BY index_code ORDER BY index_code")
        all_cons = set()
        for code, td in cons:
            n = q(db, "SELECT COUNT(*) FROM bds_index_constituent "
                      "WHERE index_code=:c AND trade_date=:d", c=code, d=td)[0][0]
            syms = [r[0] for r in q(db, "SELECT symbol FROM bds_index_constituent "
                                        "WHERE index_code=:c AND trade_date=:d", c=code, d=td)]
            all_cons.update(syms)
            print(f"  {code:<14} 最新快照 {td}  成分 {n} 只")
        print(f"  全部指数成分并集去重: {len(all_cons)} 只")

        print("\n=== 5. 落表现状 ===")
        r = q(db, "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(as_of), MAX(as_of) "
                  "FROM bds_valuation_percentile")[0]
        print(f"  bds_valuation_percentile: {r[0]} 行 / {r[1]} 标的 / {r[2]} ~ {r[3]}")
        print("  落表标的:", [x[0] for x in q(
            db, "SELECT DISTINCT symbol FROM bds_valuation_percentile ORDER BY symbol")])

        print("\n=== 6. 监测表现状 ===")
        print("  ", [x[0] for x in q(
            db, "SELECT symbol FROM irs_underlying_monitor ORDER BY symbol")])

        print("\n=== 7. 上游个股估值表规模（information_schema 估算，避免 COUNT 全表扫） ===")
        est = q(db, "SELECT table_name, table_rows FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name IN ('bds_daily_valuation','bds_index_valuation',"
                    "'bds_valuation_percentile','bds_symbol_info','bds_index_constituent')")
        for r in est:
            print(f"  {r[0]:<28} ≈ {r[1]:,} 行")


if __name__ == "__main__":
    main()
