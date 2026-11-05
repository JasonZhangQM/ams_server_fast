# -*- coding: utf-8 -*-
"""`irs_um_percentile` 建表 DDL（2026-11-05，估值分位最新截面表 A 阶段）。

用法（conda 环境 fast311）：
    conda run -n fast311 python server_fast/scripts/ddl_irs_um_percentile_20261105.py            # dry-run
    conda run -n fast311 python server_fast/scripts/ddl_irs_um_percentile_20261105.py --apply    # 执行

设计要点（docs/估值分位最新截面表方案_2026-11-05.md §2）：
- **一标的一行**（唯一键 subject_type + symbol），只存最新分位，不存历史。
- 列名 / 类型**逐列照抄** `bds_valuation_percentile`，让新表与历史表的同名行可逐值 `==`
  验收；差别只有：去掉 `as_of` 的唯一性、加 `source_as_of`（本标的上游末日）。
- 归属 irs 域：subject 集合由 `irs_underlying_monitor` 定义，消费者全在 irs（🔒4.12）。
- `note` 是哨兵列（不在更新白名单），专供 🔒6.6 验证「重算不覆盖人工值」。
- 全部幂等：`CREATE TABLE IF NOT EXISTS` + information_schema 复核。
- 显式 `COLLATE=utf8mb4_0900_ai_ci`，避免 errno 3780（ddl_vm_v3.py 同款约定）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql

ENV_PATH = Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env")
TABLE = "irs_um_percentile"
TAIL = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"


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


CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE}` (
  `id`             bigint        NOT NULL AUTO_INCREMENT,
  `subject_type`   varchar(8)    NOT NULL COMMENT 'index / stock',
  `symbol`         varchar(32)   NOT NULL COMMENT 'gm 格式代码',
  `as_of`          date          NOT NULL COMMENT '锚点：该标的上游估值序列最后一个交易日',
  `source_as_of`   date          DEFAULT NULL COMMENT '该标的所属上游估值表末日（指数/个股各自）',
  `first_date`     date          DEFAULT NULL,
  `raw_n`          int           DEFAULT NULL,
  -- 锚点当日原始值（个股未清洗，可为负 / NULL）
  `current_pe`     decimal(20,4) DEFAULT NULL COMMENT 'PE(TTM)',
  `current_pb`     decimal(20,4) DEFAULT NULL COMMENT 'PB(MRQ)',
  `current_ps`     decimal(20,4) DEFAULT NULL COMMENT 'PS(TTM)',
  `current_dy`     decimal(20,4) DEFAULT NULL COMMENT '股息率(%)，百分数（4.7224=4.72%），非小数',
  -- 12 个分位列：4 指标 × 3 窗口（不落 pct_all）
  `pe_pct_3y`      decimal(5,2)  DEFAULT NULL, `pe_pct_5y`  decimal(5,2) DEFAULT NULL, `pe_pct_10y` decimal(5,2) DEFAULT NULL,
  `pb_pct_3y`      decimal(5,2)  DEFAULT NULL, `pb_pct_5y`  decimal(5,2) DEFAULT NULL, `pb_pct_10y` decimal(5,2) DEFAULT NULL,
  `ps_pct_3y`      decimal(5,2)  DEFAULT NULL, `ps_pct_5y`  decimal(5,2) DEFAULT NULL, `ps_pct_10y` decimal(5,2) DEFAULT NULL,
  `dy_pct_3y`      decimal(5,2)  DEFAULT NULL, `dy_pct_5y`  decimal(5,2) DEFAULT NULL, `dy_pct_10y` decimal(5,2) DEFAULT NULL,
  -- 每指标 3 窗口的 分位/极值/中位数/z/样本数
  `pe_stat`        json          NULL, `pb_stat`     json NULL,
  `ps_stat`        json          NULL, `dy_stat`     json NULL,
  -- 每指标独立状态：个股可能仅 dy 无有效值，其余三个照常出分位
  `status_pe`      varchar(16)   NOT NULL DEFAULT 'ok' COMMENT 'ok/loss/no_data',
  `status_pb`      varchar(16)   NOT NULL DEFAULT 'ok',
  `status_ps`      varchar(16)   NOT NULL DEFAULT 'ok',
  `status_dy`      varchar(16)   NOT NULL DEFAULT 'ok',
  `status_note`    varchar(255)  DEFAULT NULL,
  `windows_short`  json          NULL COMMENT '按 metric 分组的短窗口标记（n<250）',
  `windows_undercovered` json    NULL COMMENT '按 metric 分组的覆盖不足窗口（左界早于数据首日）',
  `window_cut`     json          NULL COMMENT '锚点日的 3/5/10y 实际左界，口径审计用',
  `cleaned_n_pe`   int           DEFAULT NULL, `cleaned_n_pb`  int DEFAULT NULL,
  `cleaned_n_ps`   int           DEFAULT NULL, `cleaned_n_dy`  int DEFAULT NULL,
  `input_fp`       varchar(64)   DEFAULT NULL COMMENT '4 指标源序列指纹（Python 侧 sha256）',
  `calc_at`        datetime      NOT NULL COMMENT '本次计算时刻',
  `create_time`    datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`    datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  -- 哨兵列：不在写库白名单内，重算时不得覆盖人工值
  `note`           varchar(64)   DEFAULT NULL COMMENT '人工标注（哨兵列，重算不覆盖）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_irsum` (`subject_type`,`symbol`),
  KEY `k_irsum_calc` (`calc_at`)
) {TAIL} COMMENT='标的监测最新估值分位（一标的一行，供 /today 与 /irs/underlying-monitors）';
"""


def has_table(cur) -> bool:
    cur.execute("SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (TABLE,))
    return cur.fetchone() is not None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行；不传则只打印 dry-run")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    print("=" * 78)
    print(f"建表 {TABLE}", "【APPLY】" if args.apply else "【DRY-RUN】")
    print("=" * 78)
    if has_table(cur):
        print(f"- {TABLE} 已存在，跳过 CREATE（幂等）")
    else:
        print(CREATE_SQL)
        if args.apply:
            cur.execute(CREATE_SQL)
            print(f"+ 已创建 {TABLE}")

    print("\n[复核] 列名 / 类型 / 可空")
    if has_table(cur):
        cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                    "ORDER BY ORDINAL_POSITION", (TABLE,))
        cols = cur.fetchall()
        print(f"  共 {len(cols)} 列：")
        for c in cols:
            print(f"    {c[0]:<22} {c[1]:<14} null={c[2]:<3} default={c[3]}")
        cur.execute("SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                    "ORDER BY INDEX_NAME, SEQ_IN_INDEX", (TABLE,))
        print("  索引：")
        for r in cur.fetchall():
            print(f"    {r[0]:<16} {r[1]:<16} unique={0 if r[2] else 1}")
    else:
        print("  （dry-run 未建表，无复核结果）")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
