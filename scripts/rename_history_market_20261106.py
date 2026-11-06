# -*- coding: utf-8 -*-
"""`bds_index_history` 重命名为 `bds_history_market`（2026-11-06）。

用法（conda 环境 em310，在 ams_trae 目录下）：
    conda run -n em310 python server_fast/scripts/rename_history_market_20261106.py            # dry-run
    conda run -n em310 python server_fast/scripts/rename_history_market_20261106.py --apply    # 执行

变更内容（IndexHistory → HistoryMarket 全链路重命名的数据库侧）：
1. RENAME TABLE `bds_index_history` → `bds_history_market`（元数据操作，4.4 万行瞬间完成）。
2. RENAME INDEX `uk_bds_index_history_symbol_date` → `uk_bds_history_market_symbol_date`
   （ORM __table_args__ 已同步改名，保持实体与库一致）。
3. `k_ih_td` 索引名保留不动（用户拍板）。

⚠️ 幂等：先查 information_schema，表 / 索引已是新名则跳过（可重复执行）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql

ENV_PATH = Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env")
OLD_TABLE = "bds_index_history"
NEW_TABLE = "bds_history_market"
OLD_UK = "uk_bds_index_history_symbol_date"
NEW_UK = "uk_bds_history_market_symbol_date"

RENAME_TABLE_SQL = f"RENAME TABLE `{OLD_TABLE}` TO `{NEW_TABLE}`"
RENAME_INDEX_SQL = f"ALTER TABLE `{NEW_TABLE}` RENAME INDEX `{OLD_UK}` TO `{NEW_UK}`"


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


def has_table(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
    return cur.fetchone() is not None


def has_index(cur, table: str, name: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
                (table, name))
    return cur.fetchone() is not None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行；不传则只打印 dry-run")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    print("=" * 78)
    print(f"重命名 {OLD_TABLE} → {NEW_TABLE}", "【APPLY】" if args.apply else "【DRY-RUN】")
    print("=" * 78)

    # ---- 1. 表改名（旧表存在且新表不存在才执行）----
    old_exists = has_table(cur, OLD_TABLE)
    new_exists = has_table(cur, NEW_TABLE)
    if new_exists:
        print(f"- 表 {NEW_TABLE} 已存在，跳过 RENAME TABLE（幂等）")
    elif not old_exists:
        print(f"✗ {OLD_TABLE} 与 {NEW_TABLE} 都不存在，请确认库连接")
    else:
        print(RENAME_TABLE_SQL)
        if args.apply:
            cur.execute(RENAME_TABLE_SQL)
            print(f"+ 表已重命名为 {NEW_TABLE}")

    # ---- 2. 唯一约束改名（在新表上操作）----
    if has_table(cur, NEW_TABLE):
        if has_index(cur, NEW_TABLE, NEW_UK):
            print(f"- 索引 {NEW_UK} 已存在，跳过 RENAME INDEX（幂等）")
        elif has_index(cur, NEW_TABLE, OLD_UK):
            print(RENAME_INDEX_SQL)
            if args.apply:
                cur.execute(RENAME_INDEX_SQL)
                print(f"+ 索引已重命名为 {NEW_UK}")
        else:
            print(f"! 新表上未找到 {OLD_UK}，请人工确认索引结构")

    # ---- 复核 ----
    print("\n[复核] 表与索引")
    for t in (OLD_TABLE, NEW_TABLE):
        print(f"  表 {t:<22} {'存在' if has_table(cur, t) else '不存在'}")
    if has_table(cur, NEW_TABLE):
        cur.execute("SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                    "ORDER BY INDEX_NAME, SEQ_IN_INDEX", (NEW_TABLE,))
        for r in cur.fetchall():
            print(f"    {r[0]:<36} {r[1]:<16} unique={0 if r[2] else 1}")
        cur.execute(f"SELECT COUNT(*) FROM `{NEW_TABLE}`")
        print(f"  行数 = {cur.fetchone()[0]}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
