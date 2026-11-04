# -*- coding: utf-8 -*-
"""估值分位默认范围动态化验收探针（2026-11-04，方案 §5）。

用 monkeypatch 替换 `percentile_store.monitor_subjects()` 模拟标的监测表增删，
**不修改生产监测表**；只在哨兵 9a 里真写一个新标的的落表行，并在 finally 清理。

覆盖：基线 / 9a 新增有数据标的 / 9b 无数据过滤 / 9c 移除不删历史 /
      9d 读取失败回退 / 恢复核查。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.app.bds.services.valuation import percentile_store as ps
from server_fast.common.db import SessionLocal

ORIG = ps.monitor_subjects
BASE_TABLE_ROWS = 26816
NEW_INDEX = "SHSE.000688"   # 指数源表有、监测表没有 ⇒ 模拟「新增有数据标的」
REMOVED = "SZSE.300750"     # 监测表已有的个股 ⇒ 模拟「移除标的」
FAKE = "FAKE.NO_DATA"       # 两张源表都查不到 ⇒ 模拟「新增无数据标的」


def mock(symbols):
    ps.monitor_subjects = lambda db: list(symbols)


def restore():
    ps.monitor_subjects = ORIG


def diag():
    with SessionLocal() as db:
        return ps.subject_diagnostics(db)


def row_count(symbol):
    with SessionLocal() as db:
        return int(db.execute(text(
            "SELECT COUNT(*) FROM bds_valuation_percentile WHERE symbol=:s"),
            {"s": symbol}).scalar())


def cleanup(symbol):
    with SessionLocal() as db:
        db.execute(text("DELETE FROM bds_valuation_percentile WHERE symbol=:s"),
                   {"s": symbol})
        db.commit()


def main():
    print("=" * 72)
    print("估值分位默认范围动态化 · 验收探针")
    print("=" * 72)

    # ---- 基线：动态推导结果与旧硬编码一致，但来源不同 ----
    d0 = diag()
    print("\n[基线] monitor =", d0["monitor"])
    print("       default =", d0["default"])
    print("       excluded_no_data =", d0["excluded_no_data"],
          " orphan =", d0["orphan_in_store"], " fallback =", d0["fallback"])
    assert d0["fallback"] is False
    assert len(d0["default"]) == 6
    assert set(d0["excluded_no_data"]) == {"SPX", "GC00Y"}
    assert d0["orphan_in_store"] == []
    p0 = ps.plan(mode="M2")
    assert p0["total_count"] == BASE_TABLE_ROWS, p0["total_count"]
    assert p0["monitor_fallback"] is False
    print("       plan.total_count =", p0["total_count"], "→ ✅")

    # ---- 哨兵 9a：监测表新增有数据标的 ⇒ 自动纳入 + 全历史落盘 ----
    print("\n[哨兵 9a] 监测表新增有数据标的", NEW_INDEX)
    try:
        mock(d0["monitor"] + [NEW_INDEX])
        d1 = diag()
        p1 = ps.plan(mode="M2")
        print("       default_n =", len(d1["default"]), " 含新标的 =", NEW_INDEX in d1["default"])
        print("       plan.total_count =", p1["total_count"], "(基线", BASE_TABLE_ROWS, ")")
        assert NEW_INDEX in d1["default"] and len(d1["default"]) == 7
        assert p1["total_count"] > BASE_TABLE_ROWS
        assert ("index", NEW_INDEX) in p1["subject_pairs"]

        r = ps.rebuild(mode="M1")
        n = row_count(NEW_INDEX)
        print(f"       rebuild M1 ⇒ rows_upserted={r['rows_upserted']} "
              f"该标的行数={n} subjects_explicit={r['subjects_explicit']} "
              f"fallback={r['monitor_fallback']} orphans_deleted={r['orphans_deleted']}")
        assert n > 0, "新增标的没有自动全历史落盘"
        assert r["subjects_explicit"] is False, "展开默认清单不能算显式 subjects"
        assert r["monitor_fallback"] is False
        assert r["orphans_deleted"] == 0
        assert NEW_INDEX in r["default_subjects"]
        assert r["subject_source"] == "irs_underlying_monitor"
        print("       ✅ 新增标的自动纳入并全历史落盘（prev=None ⇒ 全量）")
    finally:
        cleanup(NEW_INDEX)
        restore()
    assert row_count(NEW_INDEX) == 0, "9a 清理失败"

    # ---- 哨兵 9b：监测表新增无估值数据标的 ⇒ 过滤、不写行、不报错 ----
    print("\n[哨兵 9b] 监测表新增无估值数据标的", FAKE)
    try:
        mock(d0["monitor"] + [FAKE])
        d2 = diag()
        p2 = ps.plan(mode="M2")
        print("       excluded =", d2["excluded_no_data"],
              " default_n =", len(d2["default"]),
              " total_count =", p2["total_count"])
        assert FAKE in d2["excluded_no_data"]
        assert FAKE not in d2["default"] and FAKE not in p2["subjects"]
        assert p2["total_count"] == BASE_TABLE_ROWS
        assert len(d2["default"]) == 6
        print("       ✅ 被过滤、不写行、total_count 不变")
    finally:
        restore()

    # ---- 哨兵 9c：监测表移除标的 ⇒ 默认清单收窄，历史行保留只提示 ----
    print("\n[哨兵 9c] 监测表移除", REMOVED)
    before = row_count(REMOVED)
    try:
        mock([s for s in d0["monitor"] if s != REMOVED])
        d3 = diag()
        p3 = ps.plan(mode="M2")
        r3 = ps.rebuild(mode="M2")
        after = row_count(REMOVED)
        print("       default_n =", len(d3["default"]),
              " orphan_in_store =", d3["orphan_in_store"],
              " plan.total_count =", p3["total_count"])
        print(f"       rebuild M2 ⇒ rows={r3['rows_upserted']} "
              f"skipped={r3['skipped_count']} orphans_deleted={r3['orphans_deleted']} "
              f"result.orphan_in_store={r3['orphan_in_store']} "
              f"subjects_explicit={r3['subjects_explicit']}")
        print(f"       {REMOVED} 历史行 {before} → {after}")
        assert REMOVED not in d3["default"] and len(d3["default"]) == 5
        assert REMOVED in d3["orphan_in_store"]
        assert REMOVED in r3["orphan_in_store"]
        assert r3["orphans_deleted"] == 0 and after == before, "默认不该删历史行"
        assert r3["subjects_explicit"] is False
        assert r3["prune_orphans"] is False
        print("       ✅ 默认清单收窄、历史行保留、仅提示，prune 未开启")
    finally:
        restore()

    # ---- 哨兵 9d：监测表读取失败 ⇒ 回退兜底常量，任务仍可跑 ----
    print("\n[哨兵 9d] 监测表读取异常 ⇒ 回退兜底常量")
    try:
        def boom(db):
            raise RuntimeError("mock: irs_underlying_monitor unavailable")

        ps.monitor_subjects = boom
        d4 = diag()
        p4 = ps.plan(mode="M2")
        print("       fallback =", d4["fallback"], " default =", d4["default"],
              " total_count =", p4["total_count"])
        assert d4["fallback"] is True
        assert d4["default"] == list(ps.L1_SUBJECTS)
        assert p4["total_count"] == BASE_TABLE_ROWS
        assert p4["monitor_fallback"] is True
        print("       ✅ 回退成功、任务仍可跑")
    finally:
        restore()

    # ---- 恢复核查：表与函数都回到生产态 ----
    d5 = diag()
    with SessionLocal() as db:
        total = db.execute(text("SELECT COUNT(*) FROM bds_valuation_percentile")).scalar()
        notes = db.execute(text(
            "SELECT COUNT(*) FROM bds_valuation_percentile WHERE note IS NOT NULL")).scalar()
        syms = db.execute(text(
            "SELECT COUNT(DISTINCT symbol) FROM bds_valuation_percentile")).scalar()
    print("\n[恢复核查] 总行数 =", total, " note非NULL =", notes,
          " subject数 =", syms, " fallback =", d5["fallback"])
    assert total == BASE_TABLE_ROWS and notes == 0 and syms == 6
    assert d5["fallback"] is False and d5["default"] == d0["default"]
    assert ps.monitor_subjects is ORIG

    print("\n" + "=" * 72)
    print("=== 默认范围动态化验收：✅ 全部通过 ===")
    print("=" * 72)


if __name__ == "__main__":
    main()
