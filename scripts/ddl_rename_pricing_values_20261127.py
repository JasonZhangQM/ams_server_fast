# -*- coding: utf-8 -*-
"""定价情况档位改名：部分→部分定价、已充分→充分定价（2026-11-27）

背景：定价情况选项由「未定价/部分/已充分」改为「未定价/部分定价/充分定价」，
主表 irs_um_logic.pricing 与演化链 irs_um_state_log（subject_type='logic_pricing'
的 from_state/to_state）中的旧值需同步改写，否则旧节点会掉进默认配色。

动作（幂等，重复运行无匹配行即空转）：
  UPDATE irs_um_logic      SET pricing   部分→部分定价、已充分→充分定价
  UPDATE irs_um_state_log  SET from_state/to_state 同名改写（仅 logic_pricing 行）

用法：
    conda run -n fast311 python scripts/ddl_rename_pricing_values_20261127.py            # dry-run
    conda run -n fast311 python scripts/ddl_rename_pricing_values_20261127.py --apply    # 执行
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
        charset="utf8mb4", autocommit=False)


UPDATES = [
    ("irs_um_logic", "pricing", "部分", "部分定价"),
    ("irs_um_logic", "pricing", "已充分", "充分定价"),
    ("irs_um_state_log", "from_state", "部分", "部分定价"),
    ("irs_um_state_log", "from_state", "已充分", "充分定价"),
    ("irs_um_state_log", "to_state", "部分", "部分定价"),
    ("irs_um_state_log", "to_state", "已充分", "充分定价"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行 UPDATE；缺省只做预检")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    try:
        total = 0
        for table, col, old, new in UPDATES:
            where = f"`{col}` = %s" if table == "irs_um_logic" else f"subject_type = 'logic_pricing' AND `{col}` = %s"
            cur.execute(f"SELECT id FROM `{table}` WHERE {where}", (old,))
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                print(f"[plan] {table}.{col}: {old} → {new}，命中 {len(ids)} 行 id={ids}")
                total += len(ids)
                if args.apply:
                    cur.execute(f"UPDATE `{table}` SET `{col}` = %s WHERE {where}", (new, old))
            elif args.apply:
                print(f"[skip] {table}.{col}: 无 {old} 旧值")
        if not args.apply:
            print(f"[dry-run] 共 {total} 行待改。加 --apply 执行 UPDATE")
            return
        conn.commit()
        print(f"[done] 共改写 {total} 行")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
