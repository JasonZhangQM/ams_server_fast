# -*- coding: utf-8 -*-
"""irs_um_state_log 删除 changed_at 列的 DDL（2026-11-27）

背景：append-only 审计日志「行创建即变更」，changed_at 与 create_time 语义重复，
模型已改为直接用 BaseModel.create_time（前端只展示日期）。

动作：
  1. 预检：changed_at 列是否存在
  2. 留档：导出 (id, changed_at, create_time) 对照到 scripts/archive/
  3. DROP INDEX idx_subject（旧索引含 changed_at）→ DROP COLUMN changed_at
     → 重建 idx_subject (subject_type, subject_id, create_time)
幂等：列不存在且新索引已在位时直接 skip，重复运行安全。

用法：
    conda run -n fast311 python scripts/ddl_drop_um_state_log_changed_at_20261127.py            # dry-run
    conda run -n fast311 python scripts/ddl_drop_um_state_log_changed_at_20261127.py --apply    # 执行
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
TABLE = "irs_um_state_log"
INDEX = "idx_subject"


def column_exists(cur, table: str, column: str) -> bool:
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
    """, (DB, table, column))
    return cur.fetchone()[0] > 0


def index_cols(cur, name: str) -> list[str]:
    cur.execute("""
        SELECT column_name FROM information_schema.statistics
        WHERE table_schema = %s AND table_name = %s AND index_name = %s
        ORDER BY seq_in_index
    """, (DB, TABLE, name))
    return [r[0] for r in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行删列与重建索引；缺省只做预检")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    try:
        if not column_exists(cur, TABLE, "changed_at"):
            if index_cols(cur, INDEX) == ["subject_type", "subject_id", "create_time"]:
                print(f"[skip] {DB}.{TABLE}.changed_at 不存在且 {INDEX} 已指向 create_time（已执行过）")
            else:
                print(f"[skip] changed_at 列不存在；但 {INDEX} 现为 {index_cols(cur, INDEX)}，请人工确认")
            return

        # 留档：changed_at 与 create_time 对照（两列本应一致，差异行值得看一眼）
        cur.execute(f"SELECT id, changed_at, create_time FROM `{TABLE}` ORDER BY id")
        rows = cur.fetchall()
        out = Path(__file__).parent / "archive"
        out.mkdir(exist_ok=True)
        csv_path = out / f"{TABLE}_changed_at_backup_20261127.csv"
        import csv
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "changed_at", "create_time"])
            w.writerows(rows)
        print(f"[backup] 已导出 {len(rows)} 行到 {csv_path}")

        if not args.apply:
            print(f"[dry-run] changed_at 存在（{len(rows)} 行）。加 --apply 执行删列并重建 {INDEX}")
            return

        if index_cols(cur, INDEX):
            cur.execute(f"ALTER TABLE `{TABLE}` DROP INDEX `{INDEX}`")
            print(f"[done] DROP INDEX {INDEX}")
        cur.execute(f"ALTER TABLE `{TABLE}` DROP COLUMN `changed_at`")
        print("[done] DROP COLUMN changed_at")
        cur.execute(
            f"ALTER TABLE `{TABLE}` ADD INDEX `{INDEX}` (`subject_type`, `subject_id`, `create_time`)")
        print(f"[done] ADD INDEX {INDEX} (subject_type, subject_id, create_time)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
