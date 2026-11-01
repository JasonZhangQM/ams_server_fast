# -*- coding: utf-8 -*-
"""风险敞口字段上移：bills_group / bills_group_acc 加列（2026-11-01）。

用法：
    conda run -n fast311 python scripts/ddl_group_risk.py           # dry-run（只打印）
    conda run -n fast311 python scripts/ddl_group_risk.py --apply   # 执行

背景：人工设置风险敞口从 GroupSymbol（分类×代码，跨账户）上移到
Group（账户×分类×代码）。Group 为唯一人工入口，GroupAcc / GroupSymbol
的同名列由 Group 编辑联动汇总维护（router.update_group）。

要点：
- 幂等：加列前查 information_schema，已存在则跳过；
- 存量迁移：GroupSymbol.risk_exposure 已有值时下发到 Group——仅当该
  (category, symbol) 只有一个账户持有时可无歧义迁移；多账户持有则跳过并
  提示人工到 Group 页逐账户设置（跨账户合计无法拆分）；
- 迁移后联动刷新两张汇总表（与 router.update_group 同口径：按 account、
  按 category+symbol 求和）。
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

SQL_ADD_GROUP = (
    "ALTER TABLE `bills_group` ADD COLUMN `risk_exposure` DECIMAL(12,2) NULL "
    "COMMENT '风险敞口（人工确认；编辑时联动刷新账户/标的汇总；NULL=未设置）' AFTER `diff_dwt`"
)
SQL_ADD_GROUP_ACC = (
    "ALTER TABLE `bills_group_acc` ADD COLUMN `risk_exposure` DECIMAL(12,2) NULL "
    "COMMENT '风险敞口（账户内 Group 汇总，编辑联动刷新；NULL=未设置）' AFTER `status`"
)


def run(apply: bool) -> None:
    conn = pymysql.connect(
        host=ENV.get("DB_QUZU", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    print("=" * 78)
    print("bills_group / bills_group_acc 加列 risk_exposure", "【APPLY】" if apply else "【DRY-RUN】")
    print("=" * 78)

    # 1) 加列（幂等）
    for table, sql in (("bills_group", SQL_ADD_GROUP), ("bills_group_acc", SQL_ADD_GROUP_ACC)):
        cur.execute("SELECT 1 FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME='risk_exposure'", (table,))
        if cur.fetchone():
            print(f"  - {table}.risk_exposure 已存在，跳过")
        else:
            print(f"  + {table}.risk_exposure DECIMAL(12,2) NULL")
            if apply:
                cur.execute(sql)

    # 2) 存量迁移：GroupSymbol.risk_exposure 下发到 Group（仅单账户持有）
    cur.execute("SELECT category, symbol, risk_exposure FROM bills_group_symbol "
                "WHERE risk_exposure IS NOT NULL")
    legacy = cur.fetchall()
    migrated, skipped = 0, 0
    for category, symbol, risk in legacy:
        cur.execute("SELECT id FROM bills_group WHERE category=%s AND symbol=%s", (category, symbol))
        rows = cur.fetchall()
        if len(rows) == 1:
            print(f"  ~ 迁移 {category}/{symbol}: {risk} -> Group#{rows[0][0]}")
            if apply:
                cur.execute("UPDATE bills_group SET risk_exposure=%s WHERE id=%s", (risk, rows[0][0]))
            migrated += 1
        else:
            print(f"  ! 跳过 {category}/{symbol}: {risk}（{len(rows)} 个账户持有，无法无歧义拆分，"
                  f"请到 Group 页逐账户设置）")
            skipped += 1
    print(f"  存量迁移：{migrated} 行下发 / {skipped} 行跳过")

    # 3) 联动刷新汇总表（与 router.update_group 同口径）
    if apply:
        cur.execute("""
            UPDATE bills_group_symbol gs
            JOIN (SELECT category, symbol, SUM(risk_exposure) AS s
                  FROM bills_group GROUP BY category, symbol) t
              ON gs.category = t.category AND gs.symbol = t.symbol
            SET gs.risk_exposure = t.s""")
        cur.execute("""
            UPDATE bills_group_acc ga
            JOIN (SELECT account, SUM(risk_exposure) AS s
                  FROM bills_group GROUP BY account) t
              ON ga.account = t.account
            SET ga.risk_exposure = t.s""")
        # 全 NULL 的行保持 NULL（SUM 全 NULL = NULL，无需回填 0）
        print("  已按 account / category+symbol 联动刷新汇总表")

    cur.close()
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    run(args.apply)
