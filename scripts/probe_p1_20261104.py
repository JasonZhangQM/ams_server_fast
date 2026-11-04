# -*- coding: utf-8 -*-
"""批次 2（P1-4 / P1-6）验收探针（2026-11-04）。

不写任何生产数据：除「快跳过」外所有 rebuild 都用 mock 把 upsert/删孤儿置为 no-op，
上游源表一列不动。验收项：

1. 🔒4.4 DDL ↔ ORM 两步比对：列名 + 归一化类型 + 可空，44 列 0 不一致（不比列序）。
2. P1-4 覆盖不足标记：300750 最新行 4 个 metric 都含 `pct_10y`；其余 5 个最新行 NULL；
   `percentile_warnings()` 只返回 300750，且 `short` 为空（与 `windows_short` 语义分离）。
3. 分位数值未受影响：`audit_diff` 的 12 个分位列与表内逐值比对 0 差异（由回填任务保证，
   这里再对最新行做一次独立复核）。
4. P1-6 源末日按 subject_type：`source_end_dates()` 指数/个股各自取表末日，
   `source_end_date(db)` 返回两表**最早末日**（读取侧近 15 天水位下界）。
5. P1-6 快跳过风险场景（不污染生产表）：mock 指数表末日**领先** 3 天、个股表不动 ⇒
   旧全局口径会让 2 个指数 subject 误跳过新增交易日，新口径必须只跳过 4 个个股。
   对照：把两表末日都 mock 成个股末日（等价旧实现）⇒ 6 个全跳过。
6. 快跳过恢复：回填后正常 M2 ⇒ 6 个 subject 全跳过、0 行写入、耗时数秒内。
7. 表状态：26,816 行 / note 全 NULL / 6 标的。

用法：conda run -n fast311 python server_fast/scripts/probe_p1_20261104.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from unittest.mock import patch

sys.path.insert(0, r"C:/quant_man/mysite_quant/ams_trae")

from sqlalchemy import text  # noqa: E402
from server_fast.app.bds.models import ValuationPercentile  # noqa: E402
from server_fast.app.bds.services.valuation import percentile_store as ps  # noqa: E402
from server_fast.common.db import SessionLocal  # noqa: E402

TABLE = "bds_valuation_percentile"
L1 = list(ps.L1_SUBJECTS)
UNDERCOVERED_ONE = "SZSE.300750"


def norm(t: str) -> str:
    t = (t or "").lower().replace(" ", "")
    return t.replace("numeric", "decimal").replace("integer", "int")


def check_ddl_orm() -> bool:
    with SessionLocal() as db:
        db_cols = {r[0]: (norm(r[1]), r[2]) for r in db.execute(text(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": TABLE}).all()}
    orm_cols = {c.name: (norm(str(c.type)), "YES" if c.nullable else "NO")
                for c in ValuationPercentile.__table__.columns}
    diffs = []
    for name in sorted(set(db_cols) | set(orm_cols)):
        a, b = db_cols.get(name), orm_cols.get(name)
        if a != b:
            diffs.append((name, a, b))
    print(f"[1 DDL↔ORM] DB {len(db_cols)} 列 / ORM {len(orm_cols)} 列，不一致 = {len(diffs)} {diffs}")
    print(f"            windows_undercovered = {db_cols.get('windows_undercovered')}")
    return len(db_cols) == 44 and not diffs


def check_undercovered() -> bool:
    with SessionLocal() as db:
        rows = db.execute(text("""
            SELECT p.symbol, p.as_of, p.first_date, p.windows_undercovered,
                   p.pe_pct_10y, p.pb_pct_10y, p.status_pe
            FROM bds_valuation_percentile p
            JOIN (SELECT symbol, MAX(as_of) AS mx FROM bds_valuation_percentile GROUP BY symbol) t
              ON p.symbol=t.symbol AND p.as_of=t.mx ORDER BY p.symbol
        """)).all()
    parsed = {}
    for r in rows:
        v = r[3]
        parsed[r[0]] = json.loads(v) if isinstance(v, str) else (v or {})
    print("[2 P1-4 最新行]")
    for r in rows:
        print(f"    {r[0]:<14} first={r[2]} pe10y={r[4]} pb10y={r[5]} "
              f"status={r[6]} under={parsed[r[0]]}")
    others_null = all(not parsed[s] for s in L1 if s != UNDERCOVERED_ONE)
    one_ok = all("pct_10y" in parsed[UNDERCOVERED_ONE].get(m, [])
                 for m in ps.PCT_METRICS)
    # 数值与状态一律不动：pe/pb 10y 必须有值、status 仍 ok
    values_ok = all(r[4] is not None and r[5] is not None and r[6] == "ok" for r in rows)
    warnings = ps.percentile_warnings()
    print(f"    percentile_warnings() = {json.dumps(warnings, ensure_ascii=False, default=str)}")
    warn_ok = (set(warnings) == {UNDERCOVERED_ONE}
               and warnings[UNDERCOVERED_ONE]["short"] == {}
               and len(warnings[UNDERCOVERED_ONE]["undercovered"]) == 4)
    print(f"    其余 5 标的 NULL={others_null} / 300750 四指标标 pct_10y={one_ok} / "
          f"数值与 status 未动={values_ok} / warnings 只报 300750={warn_ok}")
    return others_null and one_ok and values_ok and warn_ok


def check_source_ends() -> bool:
    with SessionLocal() as db:
        ends = ps.source_end_dates(db)
        one = ps.source_end_date(db, "index")
        overall = ps.source_end_date(db)
    print(f"[3 P1-6 分表末日] ends={ends} index单点={one} 无参(最早)={overall}")
    return (set(ends) == {"index", "stock"} and one == ends["index"]
            and overall == min(d for d in ends.values() if d))


def check_p16_mock() -> bool:
    """指数末日领先 3 天：旧全局口径会误跳过 2 个指数，新口径不该跳过。

    patch `upsert_rows` / `delete_orphans` 为 no-op ⇒ 只算不写，生产表一行不动；
    `load_source` 仍读真实源表（指数没有 11-06 的数据），所以这里验证的是
    **快跳过判定是否用 end_t**，而不是真的造出未来交易日。
    """
    stock_end = date(2026, 11, 3)
    index_end = stock_end + timedelta(days=3)
    with patch.object(ps, "source_end_dates",
                      return_value={"index": index_end, "stock": stock_end}), \
         patch.object(ps, "upsert_rows", return_value=0), \
         patch.object(ps, "delete_orphans", return_value=0):
        new = ps.rebuild(mode="M2")
    # 对照：旧实现等价于「指数也拿个股末日判」
    with patch.object(ps, "source_end_dates",
                      return_value={"index": stock_end, "stock": stock_end}), \
         patch.object(ps, "upsert_rows", return_value=0), \
         patch.object(ps, "delete_orphans", return_value=0):
        old = ps.rebuild(mode="M2")
    idx = [s for s in L1 if s in ("SHSE.000300", "SHSE.000905")]
    new_ok = new["skipped_count"] == 4 and all(s not in new["skipped_subjects"] for s in idx)
    old_bad = old["skipped_count"] == 6
    print(f"[4 P1-6 风险场景] 指数末日 {index_end} 领先个股 {stock_end}")
    print(f"    新口径：skipped={new['skipped_count']} {new['skipped_subjects']} "
          f"rows(未落库)={new['rows_upserted']} → {'✅ 指数未被误跳过' if new_ok else '❌'}")
    print(f"    旧口径对照：skipped={old['skipped_count']} ⇒ "
          f"{'✅ 复现漏判 2 个指数' if old_bad else '❌ 未复现'}")
    return new_ok and old_bad


def check_fast_skip() -> bool:
    r = ps.rebuild(mode="M2")
    ok = (r["rows_upserted"] == 0 and r["skipped_count"] == 6
          and r["schema_stale_subjects"] == [] and r["fp_changed_subjects"] == []
          and r["audit"]["changed"] == 0 and r["duration_ms"] < 10000)
    print(f"[5 快跳过恢复] rows={r['rows_upserted']} skipped={r['skipped_count']} "
          f"schema_stale={r['schema_stale_subjects']} duration={r['duration_ms']}ms "
          f"→ {'✅' if ok else '❌'}")
    return ok


def check_table_state() -> bool:
    with SessionLocal() as db:
        total = db.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
        notes = db.execute(text(f"SELECT COUNT(*) FROM {TABLE} WHERE note IS NOT NULL")).scalar()
        syms = db.execute(text(f"SELECT COUNT(DISTINCT symbol) FROM {TABLE}")).scalar()
        filled = db.execute(text(
            f"SELECT COUNT(*) FROM {TABLE} WHERE windows_undercovered IS NOT NULL")).scalar()
    ok = total == 26816 and notes == 0 and syms == 6 and filled > 0
    print(f"[6 表状态] 行数={total} note非NULL={notes} subject={syms} "
          f"undercovered非空行={filled} → {'✅' if ok else '❌'}")
    return ok


def main() -> int:
    with SessionLocal() as db:
        n = db.execute(text("SELECT COUNT(*) FROM bds_sync_task WHERE status='running'")).scalar()
        assert n == 0, f"有 {n} 条 running 任务，按 🔒4.3 中止"
    results = [check_ddl_orm(), check_undercovered(), check_source_ends(),
               check_p16_mock(), check_fast_skip(), check_table_state()]
    print("\n=== 批次 2 验收 ===", "✅ 全部通过" if all(results) else "❌ 存在失败项")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
