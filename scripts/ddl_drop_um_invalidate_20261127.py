# -*- coding: utf-8 -*-
"""删除证伪表 irs_um_invalidate 的 DDL（2026-11-27）

背景：证伪功能已从前端与后端 API 全量移除（证伪只在研报文件中维护），
本脚本删除其物理表。表无被其他表外键引用（自身持有的 FK 随表删除）。

动作：
  1. 预检：表是否存在、行数（供删除前留档）
  2. DROP TABLE IF EXISTS `irs_um_invalidate`
幂等：先查 information_schema 存在性再执行，重复运行安全。

用法：
    conda run -n fast311 python scripts/ddl_drop_um_invalidate_20261127.py            # dry-run
    conda run -n fast311 python scripts/ddl_drop_um_invalidate_20261127.py --apply    # 执行
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql

ENV = {}
for line in Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip()


def connect():
    return pymysql.connect(
        host=ENV.get("DB_QUZU", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=True)


DB = ENV["DB_NAME"]
TABLE = "irs_um_invalidate"

TABLE_EXISTS_SQL = """
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema = %s AND table_name = %s
"""

# 删除前留档：导出全表到脚本同目录的 csv（dry-run 与 --apply 都会生成/覆盖）
EXPORT_SQL = f"""
SELECT id, symbol, logic_id, seq, proposition, signal, `source`, `action`,
       judge, judge_expr, status, triggered_at, create_time, update_time
FROM `{TABLE}` ORDER BY symbol, seq
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行 DROP；缺省只做预检")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(TABLE_EXISTS_SQL, (DB, TABLE))
        exists = cur.fetchone()[0] > 0
        if not exists:
            print(f"[skip] {DB}.{TABLE} 不存在，无需删除（可能已执行过）")
            return

        # 删除前留档导出（与表同数据的 csv 放在 scripts/archive/ 下）
        cur.execute(EXPORT_SQL)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        out = Path(__file__).parent / "archive"
        out.mkdir(exist_ok=True)
        csv_path = out / f"{TABLE}_backup_20261127.csv"
        import csv
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        print(f"[backup] 已导出 {len(rows)} 行到 {csv_path}")

        if not args.apply:
            print(f"[dry-run] 表 {DB}.{TABLE} 存在（{len(rows)} 行）。加 --apply 执行 DROP TABLE")
            return

        cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
        print(f"[done] DROP TABLE {DB}.{TABLE}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
