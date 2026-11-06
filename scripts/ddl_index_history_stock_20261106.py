# -*- coding: utf-8 -*-
"""`bds_index_history` 升级为「个股 + 指数行情」的增量 DDL（2026-11-06，P1）。

用法（conda 环境 fast311）：
    conda run -n fast311 python server_fast/scripts/ddl_index_history_stock_20261106.py            # dry-run
    conda run -n fast311 python server_fast/scripts/ddl_index_history_stock_20261106.py --apply    # 执行

变更内容（只加列 / 加索引，**不改任何已有行**）：
1. `sec_type`：标的类型 `stock` / `index`。2026-11-06 之前写入的行保持 NULL，
   语义上按 `index` 解读（升级前本表只存过指数）——列表接口据此做兼容，
   不对存量数据做 UPDATE。
2. `adjust`：复权口径 `post`（个股后复权）/ `none`（指数不复权），历史行同样为 NULL。
3. `k_ih_td`：`trade_date` 单列索引。列表页按日期倒序 + 跨 symbol 分页需要它；
   现有 `uk(symbol, trade_date)` 的最左前缀是 symbol，无法支撑跨标的按日期排序。

表名与 `Numeric(12,4)` 精度维持不变（用户 2026-11-06 拍板）。
⚠️ 幂等：列 / 索引先查 information_schema，已存在则跳过（可重复执行）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql

ENV_PATH = Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env")
TABLE = "bds_index_history"

# (列名, ALTER 语句)；顺序即列序，adjust 必须排在 sec_type 之后
COLUMNS: list[tuple[str, str]] = [
    (
        "sec_type",
        f"ALTER TABLE `{TABLE}` ADD COLUMN `sec_type` varchar(8) NULL "
        f"COMMENT '标的类型: stock=个股/index=指数；2026-11-06 前的历史行为 NULL（按 index 解读）' "
        f"AFTER `symbol`",
    ),
    (
        "adjust",
        f"ALTER TABLE `{TABLE}` ADD COLUMN `adjust` varchar(8) NULL "
        f"COMMENT '复权口径: post=后复权(个股)/none=不复权(指数)；2026-11-06 前的历史行为 NULL' "
        f"AFTER `sec_type`",
    ),
]

INDEX_NAME = "k_ih_td"
INDEX_SQL = f"ALTER TABLE `{TABLE}` ADD INDEX `{INDEX_NAME}` (`trade_date`)"


def load_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
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


def has_table(cur) -> bool:
    cur.execute("SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (TABLE,))
    return cur.fetchone() is not None


def has_column(cur, column: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (TABLE, column))
    return cur.fetchone() is not None


def has_index(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
                (TABLE, name))
    return cur.fetchone() is not None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行；不传则只打印 dry-run")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    print("=" * 78)
    print(f"升级 {TABLE} 为个股+指数行情", "【APPLY】" if args.apply else "【DRY-RUN】")
    print("=" * 78)

    if not has_table(cur):
        print(f"✗ {TABLE} 不存在，请先确认库连接 / 表名")
    else:
        for column, sql in COLUMNS:
            if has_column(cur, column):
                print(f"- 列 {column} 已存在，跳过 ALTER（幂等）")
                continue
            print(sql)
            if args.apply:
                cur.execute(sql)
                print(f"+ 已添加列 {column}")

        if has_index(cur, INDEX_NAME):
            print(f"- 索引 {INDEX_NAME} 已存在，跳过 ALTER（幂等）")
        else:
            print(INDEX_SQL)
            if args.apply:
                cur.execute(INDEX_SQL)
                print(f"+ 已添加索引 {INDEX_NAME}")

    print("\n[复核] 列名 / 类型 / 可空 / 默认值")
    if has_table(cur):
        cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                    "ORDER BY ORDINAL_POSITION", (TABLE,))
        cols = cur.fetchall()
        print(f"  共 {len(cols)} 列（仅显示关键列）：")
        for c in cols:
            if c[0] in ("id", "symbol", "sec_type", "adjust", "trade_date", "close"):
                print(f"    {c[0]:<12} {c[1]:<14} null={c[2]:<3} default={c[3]}")

        cur.execute("SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                    "ORDER BY INDEX_NAME, SEQ_IN_INDEX", (TABLE,))
        print("  索引：")
        for r in cur.fetchall():
            print(f"    {r[0]:<36} {r[1]:<16} unique={0 if r[2] else 1}")

        # 存量行保持 NULL：这里只统计、不修改（dry-run 未加列时跳过）
        if has_column(cur, "sec_type"):
            cur.execute(f"SELECT COUNT(*) FROM `{TABLE}` WHERE `sec_type` IS NULL")
            print(f"  sec_type 为 NULL 的存量行 = {cur.fetchone()[0]}（按 index 解读，不做 UPDATE）")
        else:
            print("  sec_type 列尚未创建，无存量统计")
    else:
        print("  （表不存在，无复核结果）")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
