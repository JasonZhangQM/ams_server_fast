# -*- coding: utf-8 -*-
"""剧本语义收口 DDL —— 删除委托专属列（2026-10-29）。

背景：产品决策「只保留剧本」，不再区分「剧本 / 委托」。后端已停止读写以下三列：
- irs_vm_plan.kind         剧本=长期有效 / 委托=一次性
- irs_vm_plan.valid_until  委托有效期
- irs_vm_plan.bill_id      委托「已执行」回填（剧本多次执行改走 bills_bill_note.plan_id）

保留 qty，语义改为「每次执行手数」。

脚本幂等：列不存在则跳过；删除 bill_id 前先删依赖索引 k_vmp_bill。
⚠️ 删列不可逆，务必先跑 dry-run 确认影响行数，再 --apply。

用法：
    conda run -n fast311 python scripts/ddl_drop_plan_order_20261029.py            # dry-run
    conda run -n fast311 python scripts/ddl_drop_plan_order_20261029.py --apply     # 执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TABLE = "irs_vm_plan"
# (列名, 依赖该列的索引名)
DROP_COLUMNS = [
    ("kind", None),
    ("valid_until", None),
    ("bill_id", "k_vmp_bill"),
]


def load_env() -> dict:
    env = {}
    env_path = Path(__file__).resolve().parents[1] / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()


def connect():
    return pymysql.connect(
        host=ENV.get("DB_HOST", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=True)


def column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column))
    return cur.fetchone() is not None


def index_exists(cur, table: str, index: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
        (table, index))
    return cur.fetchone() is not None


def count_non_null(cur, column: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM `{TABLE}` WHERE `{column}` IS NOT NULL")
    return int(cur.fetchone()[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行；缺省仅打印计划")
    args = ap.parse_args()

    conn = connect()
    try:
        with conn.cursor() as cur:
            if not column_exists(cur, TABLE, "kind"):
                print(f"[skip] {TABLE}.kind 已不存在，无需处理")
            else:
                cur.execute(f"SELECT COUNT(*) FROM `{TABLE}`")
                total = int(cur.fetchone()[0])
                kind_cnt = {}
                for col in ("kind", "valid_until", "bill_id"):
                    if column_exists(cur, TABLE, col):
                        kind_cnt[col] = count_non_null(cur, col)
                print(f"[info] {TABLE} 共 {total} 行；非空分布：{kind_cnt}")

                for col, idx in DROP_COLUMNS:
                    if not column_exists(cur, TABLE, col):
                        print(f"[skip] {TABLE}.{col} 已不存在")
                        continue
                    if idx and index_exists(cur, TABLE, idx):
                        sql = f"ALTER TABLE `{TABLE}` DROP INDEX `{idx}`"
                        print(f"[{'apply' if args.apply else 'dry'}] {sql}")
                        if args.apply:
                            cur.execute(sql)
                    sql = f"ALTER TABLE `{TABLE}` DROP COLUMN `{col}`"
                    print(f"[{'apply' if args.apply else 'dry'}] {sql}")
                    if args.apply:
                        cur.execute(sql)
    finally:
        conn.close()

    if not args.apply:
        print("dry-run 结束；确认后加 --apply 执行（删列不可逆）")
    else:
        print("DDL 执行完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
