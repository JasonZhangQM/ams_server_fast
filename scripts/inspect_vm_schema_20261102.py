# -*- coding: utf-8 -*-
"""查询 vm 改名涉及的 FK / 索引 / 列定义（DDL 前置侦察，只读）"""
from pathlib import Path

import pymysql

ENV = {}
for line in Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip()

conn = pymysql.connect(
    host=ENV.get("DB_QUZU", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
    user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
    charset="utf8mb4", autocommit=True)
cur = conn.cursor()

TABLES = [
    "irs_value_monitor", "irs_vm_version", "irs_vm_research", "irs_vm_logic",
    "irs_vm_invalidate", "irs_vm_plan", "irs_vm_review", "irs_vm_state_log",
    "irs_vm_target", "bills_group_symbol",
]

print("=== 外键约束（含列与引用） ===")
cur.execute("""
    SELECT tc.table_name, tc.constraint_name, kcu.column_name,
           kcu.referenced_table_name, kcu.referenced_column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
      AND tc.table_name IN %s
""", (ENV["DB_NAME"], tuple(TABLES)))
for row in cur.fetchall():
    print("  ", row)

print("=== 索引 ===")
cur.execute("""
    SELECT table_name, index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index)
    FROM information_schema.statistics
    WHERE table_schema = %s AND table_name IN %s
    GROUP BY table_name, index_name
""", (ENV["DB_NAME"], tuple(TABLES)))
for row in cur.fetchall():
    print("  ", row)

print("=== vm_symbol 列定义 ===")
cur.execute("""
    SELECT table_name, column_type, is_nullable, column_comment
    FROM information_schema.columns
    WHERE table_schema = %s AND column_name = 'vm_symbol'
""", (ENV["DB_NAME"],))
for row in cur.fetchall():
    print("  ", row)

print("=== 行数 ===")
for t in TABLES:
    cur.execute(f"SELECT COUNT(*) FROM `{t}`")
    print(f"   {t}: {cur.fetchone()[0]}")

conn.close()
