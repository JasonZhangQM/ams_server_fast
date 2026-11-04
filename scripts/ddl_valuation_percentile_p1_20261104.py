# -*- coding: utf-8 -*-
"""`bds_valuation_percentile` 增量 DDL：新增 `windows_undercovered`（P1-4，2026-11-04）

用法（conda 环境 fast311）：
    conda run -n fast311 python server_fast/scripts/ddl_valuation_percentile_p1_20261104.py            # dry-run
    conda run -n fast311 python server_fast/scripts/ddl_valuation_percentile_p1_20261104.py --apply   # 执行

背景：窗口是「日历对日」3/5/10 年，但标的上市时间短于窗口年限时（如 300750 首日
2018-06-11、as_of=2026-11-03 ⇒ 名义 10y 实际只覆盖 8.4y），原表只有 `windows_short`
（n<250 的极短窗口）能提示，`read_percentiles()` 又完全不读它 ⇒ 列表页把 8.4 年的数
显示成「10 年分位」而毫无提示。

采用方案 A（docs/估值分位落盘问题优化方案_2026-11-04.md §2.1）：**只加标记列，数值一律不动**。
- 不改 `windows_short` 的结构（影子探针与哨兵都在读它）；
- 判据 `window_cuts(as_of)[w] < 该 metric 有效值首日` ⇒ `windows_undercovered` 记
  `{metric: [window, ...]}`；无覆盖不足则 NULL；
- 🔒4.4 DDL + ORM 两步：`models.py` 同步声明，比对列名/类型/可空，不比列序；
- 🔒4.9 该列须进 `percentile_store.fields_pct_update` 白名单才会被 upsert 更新。

⚠️ JSON 列不能写 `DEFAULT NULL`（MySQL 9.3 实测现有 JSON 列默认值均为 None），用 `json NULL`。
⚠️ 幂等：先查 information_schema，列已存在则跳过 ALTER。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql

ENV_PATH = Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env")
TABLE = "bds_valuation_percentile"
COLUMN = "windows_undercovered"
ALTER_SQL = (
    f"ALTER TABLE `{TABLE}` ADD COLUMN `{COLUMN}` json NULL "
    f"COMMENT '按 metric 分组的覆盖不足窗口（左界早于数据首日）' AFTER `windows_short`"
)


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


def has_column(cur) -> bool:
    cur.execute("SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (TABLE, COLUMN))
    return cur.fetchone() is not None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行；不传则只打印 dry-run")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    print("=" * 78)
    print(f"加列 {TABLE}.{COLUMN}", "【APPLY】" if args.apply else "【DRY-RUN】")
    print("=" * 78)
    if not has_table(cur):
        print(f"✗ {TABLE} 不存在，请先跑 ddl_valuation_percentile_20261104.py --apply")
    elif has_column(cur):
        print(f"- 列已存在，跳过 ALTER（幂等）")
    else:
        print(ALTER_SQL)
        if args.apply:
            cur.execute(ALTER_SQL)
            print(f"+ 已添加 {COLUMN}")

    print("\n[复核] 列名 / 类型 / 可空 / 列数")
    if has_table(cur):
        cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                    "ORDER BY ORDINAL_POSITION", (TABLE,))
        cols = cur.fetchall()
        print(f"  共 {len(cols)} 列")
        for c in cols:
            if c[0] in ("windows_short", "window_cut", COLUMN, "note", "as_of"):
                print(f"    {c[0]:<22} {c[1]:<14} null={c[2]:<3} default={c[3]}")
        print(f"  {COLUMN} 存在 = {has_column(cur)}")
        if has_column(cur):
            cur.execute(f"SELECT COUNT(*) FROM `{TABLE}` WHERE `{COLUMN}` IS NOT NULL")
            print(f"  已有非空标记行数 = {cur.fetchone()[0]}")
    else:
        print("  （表不存在，无复核结果）")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
