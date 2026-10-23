# -*- coding: utf-8 -*-
"""删除 `irs_value_monitor.object_type`（幂等）

用法：
    conda run -n fast311 python scripts/ddl_drop_object_type.py            # dry-run（只打印现状与待执行 SQL）
    conda run -n fast311 python scripts/ddl_drop_object_type.py --apply    # 执行

为什么单独一个脚本而不是塞回 ddl_vm_v3.py：
`ddl_vm_v3.py` 是那次改造的执行凭证（阶段一加列 + 阶段二删列都已跑过，留档可复核）。
事后删列如果再写进去，重跑那个脚本会把 object_type 当成阶段二的一部分反复删，
也让「它当初加过什么」这条记录无从对照 —— 所以新删列单独成文，并顺手把
`ddl_vm_v3.py` 里加这一列的语句注掉（防止有人重跑阶段一把它加回来）。

删列前数据已备份：`backup/vm_v3_20261023/irs_value_monitor_object_type.json`
（9 行：指数 3 / 商品 1 / 股权 4 / NULL 1）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql

TABLE = "irs_value_monitor"
COLUMN = "object_type"


def load_env() -> dict:
    env = {}
    for line in Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()


def connect():
    return pymysql.connect(
        host=ENV.get("DB_QUZU", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=True)


def column_exists(cur) -> bool:
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s
    """, (TABLE, COLUMN))
    return cur.fetchone()[0] > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行 DROP COLUMN")
    args = ap.parse_args()

    conn = connect()
    try:
        cur = conn.cursor()
        if not column_exists(cur):
            print(f"[skip] {TABLE}.{COLUMN} 已不存在，无需处理")
            return 0

        cur.execute(f"SELECT COUNT(*) FROM `{TABLE}`")
        total = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM `{TABLE}` WHERE `{COLUMN}` IS NOT NULL")
        filled = cur.fetchone()[0]
        print(f"[现状] {TABLE} 共 {total} 行，{COLUMN} 非空 {filled} 行")

        sql = f"ALTER TABLE `{TABLE}` DROP COLUMN `{COLUMN}`"
        print(f"[SQL ] {sql}")

        if not args.apply:
            print("\n[dry-run] 未执行。确认无误后加 --apply（数据已备份到 backup/vm_v3_20261023/）")
            return 0

        cur.execute(sql)
        ok = not column_exists(cur)
        print(f"\n[执行] {'成功，列已消失' if ok else '失败，列仍在'}")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
