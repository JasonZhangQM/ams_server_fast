# -*- coding: utf-8 -*-
"""bills_group_symbol 加风险敞口列（2026-11-01）。

用法：
    conda run -n fast311 python scripts/ddl_group_symbol_risk.py           # dry-run（只打印）
    conda run -n fast311 python scripts/ddl_group_symbol_risk.py --apply   # 执行

要点：
- 幂等：加列前查 information_schema，已存在则跳过；
- risk_exposure 是人工维护列（与 vm_symbol 同类），不进 group_summary._DERIVED_COLS
  白名单，自动汇总（upsert_group_symbol_sql）永不覆盖；
- 位置放在 vm_symbol 之后，与 ORM 模型字段顺序一致。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql


def load_env() -> dict:
    env = {}
    for line in Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()

ADD_COLUMN_SQL = (
    "ALTER TABLE `bills_group_symbol` ADD COLUMN `risk_exposure` DECIMAL(12,2) NULL "
    "COMMENT '风险敞口（人工确认，重算不覆盖；NULL=未设置）' AFTER `vm_symbol`"
)


def run(apply: bool) -> None:
    conn = pymysql.connect(
        host=ENV.get("DB_QUZU", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    print("=" * 78)
    print("bills_group_symbol 加列 risk_exposure", "【APPLY】" if apply else "【DRY-RUN】")
    print("=" * 78)

    cur.execute("SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='bills_group_symbol' "
                "AND COLUMN_NAME='risk_exposure'")
    if cur.fetchone():
        print("  - bills_group_symbol.risk_exposure 已存在，跳过")
    else:
        print("  + bills_group_symbol.risk_exposure DECIMAL(12,2) NULL")
        if apply:
            cur.execute(ADD_COLUMN_SQL)

    # 复核：列出人工列，确认 vm_symbol / risk_exposure 都在
    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='bills_group_symbol' "
                "ORDER BY ORDINAL_POSITION")
    print("  复核列序:", [r[0] for r in cur.fetchall()])
    cur.close()
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    run(args.apply)
