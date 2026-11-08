# -*- coding: utf-8 -*-
"""风险看板表 bills → irs 改名 DDL（2026-11-08）

风险看板整体搬迁至 irs 模块，两张表同步改名（RENAME 原子操作，数据全保留）：
  1. bills_risk_param    → irs_risk_param
  2. bills_scenario_grid → irs_scenario_grid

幂等：先查 information_schema 存在性再执行；新表已存在则跳过。

用法：
    conda run -n fast311 python scripts/ddl_risk_board_to_irs_20261108.py            # dry-run
    conda run -n fast311 python scripts/ddl_risk_board_to_irs_20261108.py --apply    # 执行
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

RENAME_TABLES = [
    ("bills_risk_param", "irs_risk_param"),
    ("bills_scenario_grid", "irs_scenario_grid"),
]


def table_exists(cur, name):
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (DB, name))
    return cur.fetchone() is not None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = connect()
    cur = conn.cursor()

    print("== 1. 改名前行数 ==")
    for old, _new in RENAME_TABLES:
        if table_exists(cur, old):
            cur.execute(f"SELECT COUNT(*) FROM `{old}`")
            print(f"  {old}: {cur.fetchone()[0]} 行")
        else:
            print(f"  [SKIP] {old} 不存在")

    print("== 2. RENAME TABLE ==")
    pairs = [(o, n) for o, n in RENAME_TABLES if table_exists(cur, o)]
    if pairs:
        sql = "RENAME TABLE " + ", ".join(f"`{o}` TO `{n}`" for o, n in pairs)
        print(f"  [{'APPLY' if args.apply else 'DRY'}] {sql}")
        if args.apply:
            cur.execute(sql)
    else:
        print("  [SKIP] 表均已改名")

    print("== 3. 验证 ==")
    for old, new in RENAME_TABLES:
        print(f"  旧表 {old} 存在: {table_exists(cur, old)}")
        if table_exists(cur, new):
            cur.execute(f"SELECT COUNT(*) FROM `{new}`")
            print(f"  新表 {new}: {cur.fetchone()[0]} 行")
        else:
            print(f"  新表 {new}: 尚不存在（dry-run 正常）")

    conn.close()
    if not args.apply:
        print("\ndry-run 完成，未执行任何 DDL。确认无误后加 --apply 执行。")


if __name__ == "__main__":
    main()
