# -*- coding: utf-8 -*-
"""vm → um 全量改名 DDL（2026-11-02，方案 C）

动作：
  1. RENAME TABLE ×9（InnoDB FK 引用自动跟随新表名）
  2. vm_symbol → underlying_symbol（irs_um_plan / bills_group_symbol，FK 列：
     先 DROP FK → CHANGE COLUMN → ADD FK）
  3. FK 约束名 vm→um 缩写对称改名（fk_vmv_symbol → fk_umv_symbol 等）
  4. 索引名 vm→um 对称改名（含 ORM 已改的 k_irs_underlying_monitor_symbol、uk_ump_symbol_seq）
幂等：每步先查 information_schema 存在性再执行。

用法：
    conda run -n fast311 python scripts/ddl_rename_um_20261102.py            # dry-run
    conda run -n fast311 python scripts/ddl_rename_um_20261102.py --apply    # 执行
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

# 1) 表改名
RENAME_TABLES = [
    ("irs_value_monitor", "irs_underlying_monitor"),
    ("irs_vm_version", "irs_um_version"),
    ("irs_vm_research", "irs_um_research"),
    ("irs_vm_logic", "irs_um_logic"),
    ("irs_vm_invalidate", "irs_um_invalidate"),
    ("irs_vm_plan", "irs_um_plan"),
    ("irs_vm_review", "irs_um_review"),
    ("irs_vm_state_log", "irs_um_state_log"),
    ("irs_vm_target", "irs_um_target"),
]

# 2) FK 列改名（DROP FK → CHANGE → ADD FK）
#    (表, 旧列, 新列, 列定义, 旧FK名, 新FK名)
COLUMN_RENAMES = [
    ("irs_vm_plan", "vm_symbol", "underlying_symbol",
     "varchar(32) NOT NULL COMMENT '标的代码（irs_underlying_monitor.symbol）'",
     "fk_vmp_vm", "fk_ump_um"),
    ("bills_group_symbol", "vm_symbol", "underlying_symbol",
     "varchar(32) DEFAULT NULL COMMENT '归属标的（irs_underlying_monitor.symbol）；融资融券与未归属为 NULL'",
     "fk_bgs_vm", "fk_bgs_um"),
]

# 3) 其余 FK 约束名改名（列不变，仅换名）(表, 旧名, 新名, 列, 引用表, 引用列)
FK_RENAMES = [
    ("irs_vm_version", "fk_vmv_symbol", "fk_umv_symbol", "symbol", "irs_value_monitor", "symbol"),
    ("irs_vm_research", "fk_vmr_symbol", "fk_umr_symbol", "symbol", "irs_value_monitor", "symbol"),
    ("irs_vm_logic", "fk_vml_symbol", "fk_uml_symbol", "symbol", "irs_value_monitor", "symbol"),
    ("irs_vm_invalidate", "fk_vmi_symbol", "fk_umi_symbol", "symbol", "irs_value_monitor", "symbol"),
    ("irs_vm_review", "fk_vmr_vm", "fk_umr_um", "symbol", "irs_value_monitor", "symbol"),
    ("irs_vm_review", "fk_vmr_research", "fk_umr_research", "research_id", "irs_vm_research", "id"),
]

# 4) 索引名改名（表用新名）(表, 旧索引, 新索引)
INDEX_RENAMES = [
    ("irs_underlying_monitor", "k_irs_value_monitor_symbol", "k_irs_underlying_monitor_symbol"),
    ("irs_um_version", "k_vmv_date", "k_umv_date"),
    ("irs_um_version", "uk_vmv_symbol_date", "uk_umv_symbol_date"),
    ("irs_um_research", "k_vmr_date", "k_umr_date"),
    ("irs_um_research", "uk_vmr_symbol_date", "uk_umr_symbol_date"),
    ("irs_um_logic", "k_vml_state", "k_uml_state"),
    ("irs_um_logic", "uk_vml_symbol_seq", "uk_uml_symbol_seq"),
    ("irs_um_invalidate", "k_vmi_status", "k_umi_status"),
    ("irs_um_invalidate", "uk_vmi_symbol_seq", "uk_umi_symbol_seq"),
    ("irs_um_plan", "uk_vmp_symbol_seq", "uk_ump_symbol_seq"),
    ("irs_um_plan", "k_vmp_bill", "k_ump_bill"),
    ("irs_um_plan", "k_vmp_trade", "k_ump_trade"),
    ("irs_um_review", "k_vmr_research_date", "k_umr_research_date"),
    ("irs_um_review", "k_vmr_symbol_date", "k_umr_symbol_date"),
    ("bills_group_symbol", "fk_bgs_vm", "fk_bgs_um"),   # FK 自动索引，随 FK 名
]


def table_exists(cur, name):
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (DB, name))
    return cur.fetchone() is not None


def fk_exists(cur, table, name):
    cur.execute("""SELECT 1 FROM information_schema.table_constraints
                   WHERE table_schema=%s AND table_name=%s AND constraint_name=%s AND constraint_type='FOREIGN KEY'""",
                (DB, table, name))
    return cur.fetchone() is not None


def index_exists(cur, table, name):
    cur.execute("SELECT 1 FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s AND index_name=%s",
                (DB, table, name))
    return cur.fetchone() is not None


def column_exists(cur, table, name):
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                (DB, table, name))
    return cur.fetchone() is not None


def run(cur, sql, apply: bool, note: str):
    print(f"  [{'APPLY' if apply else 'DRY'}] {note}")
    print(f"         {sql}")
    if apply:
        cur.execute(sql)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = connect()
    cur = conn.cursor()

    # ---- 1. RENAME TABLE ----
    print("== 1. RENAME TABLE ==")
    pairs = [(o, n) for o, n in RENAME_TABLES if table_exists(cur, o)]
    if pairs:
        sql = "RENAME TABLE " + ", ".join(f"`{o}` TO `{n}`" for o, n in pairs)
        run(cur, sql, args.apply, f"{len(pairs)} 张表原子改名")
    else:
        print("  [SKIP] 表均已改名")

    # ---- 2. FK 列改名 ----
    print("== 2. vm_symbol → underlying_symbol（FK 列）==")
    for table, old_col, new_col, col_def, old_fk, new_fk in COLUMN_RENAMES:
        t = table if table_exists(cur, table) else dict(RENAME_TABLES).get(table, table)
        if not column_exists(cur, t, old_col):
            print(f"  [SKIP] {t}.{old_col} 不存在（可能已改）")
            continue
        if fk_exists(cur, t, old_fk):
            run(cur, f"ALTER TABLE `{t}` DROP FOREIGN KEY `{old_fk}`", args.apply, f"{t} 删 FK {old_fk}")
        run(cur, f"ALTER TABLE `{t}` CHANGE COLUMN `{old_col}` `{new_col}` {col_def}", args.apply,
            f"{t}.{old_col} → {new_col}")
        if not fk_exists(cur, t, new_fk):
            run(cur, f"ALTER TABLE `{t}` ADD CONSTRAINT `{new_fk}` FOREIGN KEY (`{new_col}`) "
                     f"REFERENCES `irs_underlying_monitor`(`symbol`) ON DELETE RESTRICT", args.apply,
                f"{t} 重建 FK {new_fk}")

    # ---- 3. FK 约束名改名 ----
    print("== 3. FK 约束名 vm→um ==")
    for table, old_fk, new_fk, col, ref_t, ref_c in FK_RENAMES:
        t = table if table_exists(cur, table) else dict(RENAME_TABLES).get(table, table)
        rt = ref_t if table_exists(cur, ref_t) else dict(RENAME_TABLES).get(ref_t, ref_t)
        if fk_exists(cur, t, old_fk):
            run(cur, f"ALTER TABLE `{t}` DROP FOREIGN KEY `{old_fk}`", args.apply, f"{t} 删 FK {old_fk}")
        if not fk_exists(cur, t, new_fk):
            run(cur, f"ALTER TABLE `{t}` ADD CONSTRAINT `{new_fk}` FOREIGN KEY (`{col}`) "
                     f"REFERENCES `{rt}`(`{ref_c}`) ON DELETE RESTRICT", args.apply, f"{t} 建 FK {new_fk}")

    # ---- 4. 索引名改名 ----
    print("== 4. 索引名 vm→um ==")
    for table, old_idx, new_idx in INDEX_RENAMES:
        if not table_exists(cur, table):
            print(f"  [SKIP] 表 {table} 不存在")
            continue
        if index_exists(cur, table, old_idx):
            run(cur, f"ALTER TABLE `{table}` RENAME INDEX `{old_idx}` TO `{new_idx}`", args.apply,
                f"{table}: {old_idx} → {new_idx}")
        elif index_exists(cur, table, new_idx):
            print(f"  [SKIP] {table}.{new_idx} 已存在")
        else:
            print(f"  [WARN] {table}.{old_idx} 与 {new_idx} 均不存在，需人工检查")

    # ---- 验证 ----
    print("== 5. 验证 ==")
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema=%s AND table_name LIKE 'irs\\_vm\\_%%'""", (DB,))
    leftover = [r[0] for r in cur.fetchall()]
    print(f"  旧表残留: {leftover or '无'}")
    for t in ["irs_underlying_monitor", "irs_um_version", "irs_um_research", "irs_um_logic",
              "irs_um_invalidate", "irs_um_plan", "irs_um_review", "irs_um_state_log", "irs_um_target"]:
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        print(f"  {t}: {cur.fetchone()[0]} 行")
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema=%s AND column_name='vm_symbol'""", (DB,))
    print(f"  vm_symbol 列残留: {cur.fetchall() or '无'}")

    conn.close()
    if not args.apply:
        print("\ndry-run 完成，未执行任何 DDL。确认无误后加 --apply 执行。")


if __name__ == "__main__":
    main()
