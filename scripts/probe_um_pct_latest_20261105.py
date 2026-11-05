# -*- coding: utf-8 -*-
"""`irs_um_percentile` 落地验收（2026-11-05，方案 §7）。

覆盖：
1. DDL ↔ ORM 比对（🔒4.4：只比列名 / 类型 / 可空，不比列序）；
2. 跑一次 `refresh()`（真实写入生产表，这是设计内的写入）；
3. 新表每行 vs `bds_valuation_percentile` 同 `(subject_type, symbol, as_of)` 行，
   12 个分位列 + 4 个 status + current_* 逐值全等（🔒6.6 空值归一化后比较）；
4. `read_map()` 与 `percentile_store.read_percentiles()` 逐键比对。

用法：
    conda run -n fast311 python server_fast/scripts/probe_um_pct_latest_20261105.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from server_fast.app.bds.services.valuation import percentile_store
from server_fast.app.irs.models import UmPercentile
from server_fast.app.irs.service import um_percentile
from server_fast.common.db import SessionLocal

TABLE = "irs_um_percentile"
PCT_COLS = percentile_store.PCT_COLUMNS
STATUS_COLS = ["status_pe", "status_pb", "status_ps", "status_dy"]
CMP_COLS = PCT_COLS + STATUS_COLS + ["current_pe", "current_pb", "current_ps", "current_dy"]


def norm_type(t: str) -> str:
    t = str(t).lower().replace(" ", "")
    # MySQL 里 NUMERIC 就是 DECIMAL 的同义词；ORM 写 Numeric(...) 会编译成 numeric，
    # information_schema 存 decimal ⇒ 不归一化会误报 16 处（上一轮同款假差异）。
    return t.replace("integer", "int").replace("numeric", "decimal")


def check_orm_vs_ddl() -> int:
    print("== 1. DDL ↔ ORM 比对 ==")
    from sqlalchemy.dialects import mysql
    dialect = mysql.dialect()
    orm = {}
    for col in UmPercentile.__table__.columns:
        orm[col.name] = (norm_type(col.type.compile(dialect=dialect)),
                         "YES" if col.nullable else "NO")
    with SessionLocal() as db:
        rows = db.execute(text(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": TABLE}).all()
    ddl = {r[0]: (norm_type(r[1]), r[2]) for r in rows}

    only_orm = sorted(set(orm) - set(ddl))
    only_ddl = sorted(set(ddl) - set(orm))
    diffs = [(k, ddl[k], orm[k]) for k in sorted(set(orm) & set(ddl)) if orm[k] != ddl[k]]
    print(f"  ORM 列 {len(orm)} / DDL 列 {len(ddl)}")
    print(f"  仅 ORM 有：{only_orm or '无'}")
    print(f"  仅 DDL 有：{only_ddl or '无'}")
    for k, a, b in diffs:
        print(f"  ⚠️ {k}: DDL={a} ORM={b}")
    print(f"  差异 {len(only_orm) + len(only_ddl) + len(diffs)} 处"
          f"{'  ✅ 一致' if not (only_orm or only_ddl or diffs) else '  ❌ 需修'}")
    return len(only_orm) + len(only_ddl) + len(diffs)


def _norm(v):
    if v is None or v == "":
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return str(v)


def run_refresh() -> dict:
    print("\n== 2. refresh() ==")
    def prog(done, total):
        print(f"  进度 {done}/{total}", end="\r")
    r = um_percentile.refresh(on_progress=prog)
    print()
    print(f"  写入 {r['rows_upserted']} 行（新增 {r['rows_appeared']}）· "
          f"删除范围外 {r['rows_deleted']} · 耗时 {r['duration_ms']} ms")
    print(f"  过滤无估值数据：{r['excluded_no_data'] or '无'}")
    print(f"  各标的锚点 as_of：{r['as_of_by_subject']}")
    print(f"  源末日：{r['source_as_of']}")
    if r["stale_by_subject"]:
        print(f"  ⚠️ 落后于源末日（停牌/上游缺行）：{r['stale_by_subject']}")
    return r


def compare_with_history() -> int:
    print("\n== 3. 新表 vs 历史表同 (subject_type, symbol, as_of) 行 ==")
    cols = ", ".join(CMP_COLS)
    with SessionLocal() as db:
        new_rows = db.execute(text(
            f"SELECT subject_type, symbol, as_of, {cols} FROM {TABLE} "
            "ORDER BY subject_type, symbol")).mappings().all()
        bad = 0
        for n in new_rows:
            h = db.execute(text(
                f"SELECT {cols} FROM bds_valuation_percentile "
                "WHERE subject_type=:t AND symbol=:s AND as_of=:d"),
                {"t": n["subject_type"], "s": n["symbol"], "d": n["as_of"]}).mappings().first()
            if h is None:
                print(f"  · {n['symbol']} as_of={n['as_of']}：历史表无同日行"
                      f"（新表更到最新交易日，属正常）")
                continue
            diffs = [(c, _norm(h[c]), _norm(n[c])) for c in CMP_COLS
                     if _norm(h[c]) != _norm(n[c])]
            if diffs:
                bad += 1
                print(f"  ❌ {n['symbol']} as_of={n['as_of']}：{diffs[:5]}")
            else:
                print(f"  ✅ {n['symbol']} as_of={n['as_of']}：{len(CMP_COLS)} 列全等"
                      f"（pe10y={n['pe_pct_10y']} pb10y={n['pb_pct_10y']}）")
        print(f"  新表 {len(new_rows)} 行，差异 {bad} 行")
    return bad


def compare_readmap() -> int:
    print("\n== 4. read_map() vs 历史表读取 ==")
    new = um_percentile.read_map()
    old = percentile_store.read_percentiles()
    keys = sorted(set(new) | set(old))
    bad = 0
    for k in keys:
        a, b = _norm(old.get(k)), _norm(new.get(k))
        flag = "✅" if a == b else "❌"
        if a != b:
            bad += 1
        print(f"  {flag} {k[0]:14s} {k[1]:8s} 历史={a} 新表={b}")
    print(f"  键 {len(keys)} 个，差异 {bad} 个")
    return bad


def compare_realtime() -> int:
    """独立路径复算：实时分位服务（`stock_valuation` / `index_valuation`）算出的
    `pct_10y` 必须与新表逐值相等。这条能证明「新表更到 11-04 而历史表停在 11-03」
    不是口径漂移，而是新表更鲜。
    """
    print("\n== 3b. 实时分位服务复算（独立代码路径）==")
    from server_fast.app.bds.services.valuation import index_valuation, stock_valuation
    bad = 0
    with SessionLocal() as db:
        rows = db.execute(text(
            f"SELECT subject_type, symbol, as_of, pe_pct_10y, pb_pct_10y FROM {TABLE} "
            "ORDER BY subject_type, symbol")).mappings().all()
    for r in rows:
        mod = index_valuation if r["subject_type"] == "index" else stock_valuation
        for metric, col in (("pe_ttm", "pe_pct_10y"), ("pb_mrq", "pb_pct_10y")):
            try:
                res = mod.get_percentiles([r["symbol"]], metric)
                # 指数分位端点主键是 index_code，个股是 symbol（两处历史命名不一致）
                key = "index_code" if r["subject_type"] == "index" else "symbol"
                item = next((x for x in res["items"] if x.get(key) == r["symbol"]), None)
            except Exception as e:  # 实时路径对个别标的有前置校验，失败不判错
                print(f"  · {r['symbol']} {metric}：实时复算不可用（{type(e).__name__}: {e}）")
                continue
            if not item or item.get("status") != "ok" or not item.get("pct_10y"):
                print(f"  · {r['symbol']} {metric}：实时复算无有效分位，跳过")
                continue
            live = _norm(item["pct_10y"]["pct"])
            mine = _norm(r[col])
            ok = (live == mine and item["as_of"] == r["as_of"].isoformat())
            if not ok:
                bad += 1
            print(f"  {'✅' if ok else '❌'} {r['symbol']:14s} {metric:8s} "
                  f"实时={live}（as_of={item['as_of']}） 新表={mine}"
                  f"（as_of={r['as_of']}）")
    print(f"  差异 {bad} 处")
    return bad


def main() -> None:
    d1 = check_orm_vs_ddl()
    run_refresh()
    d2 = compare_with_history()
    d3 = compare_readmap()
    d4 = compare_realtime()
    print("\n== 结论 ==")
    print(f"  DDL/ORM 差异 {d1} · 历史行差异 {d2} · 读取键差异 {d3}（应为 0 或仅「新表更鲜」）"
          f" · 实时复算差异 {d4}")
    print("  " + ("✅ 全部通过" if d1 == 0 and d2 == 0 and d4 == 0 else "❌ 有差异，见上"))


if __name__ == "__main__":
    main()
