# -*- coding: utf-8 -*-
"""`bds_valuation_percentile` 建表 DDL（2026-11-04，P2 估值分位按日落盘 A 阶段）

用法（conda 环境 fast311）：
    conda run -n fast311 python server_fast/scripts/ddl_valuation_percentile_20261104.py            # dry-run
    conda run -n fast311 python server_fast/scripts/ddl_valuation_percentile_20261104.py --apply   # 执行

设计要点（docs/估值分位按日落盘方案_2026-11-04.md §4）：
- **一行 = 一个 subject 的一个 `as_of`**（观察日），不是截面表；分位是标的×日期的纯函数。
- 宽表：4 指标 × 3 窗口 = 12 个分位列；`pct_all` 由分位专页实时算、不落库。
- 🔴 触发条件写死：若 L3 再纳入 `pcf_ttm_oper` / `pe_median`，**必须转长表**，
  不要继续横向加列。
- `note` 是哨兵列（不在 `fields_pct_update` 白名单），专供 §10 哨兵实验验证
  「重算不覆盖人工值」；生产上也可作人工标注列。
- 全部幂等：`CREATE TABLE IF NOT EXISTS` + information_schema 复核。
- 显式 `COLLATE=utf8mb4_0900_ai_ci`，避免 errno 3780（ddl_vm_v3.py 同款约定）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql

ENV_PATH = Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env")
TABLE = "bds_valuation_percentile"
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
  `as_of`          date          NOT NULL COMMENT '观察日：本行分位所属交易日，窗口右端锚点',
  -- 当日原始值（个股未清洗，可为负 / NULL）
  `current_pe`     decimal(20,4) DEFAULT NULL COMMENT 'PE(TTM)',
  `current_pb`     decimal(20,4) DEFAULT NULL COMMENT 'PB(MRQ)',
  `current_ps`     decimal(20,4) DEFAULT NULL COMMENT 'PS(TTM)',
  `current_dy`     decimal(20,4) DEFAULT NULL COMMENT '股息率(%)，百分数（4.7224=4.72%），非小数',
  -- 12 个分位列：4 指标 × 3 窗口（不落 pct_all）
  `pe_pct_3y`      decimal(5,2)  DEFAULT NULL, `pe_pct_5y`  decimal(5,2) DEFAULT NULL, `pe_pct_10y` decimal(5,2) DEFAULT NULL,
  `pb_pct_3y`      decimal(5,2)  DEFAULT NULL, `pb_pct_5y`  decimal(5,2) DEFAULT NULL, `pb_pct_10y` decimal(5,2) DEFAULT NULL,
  `ps_pct_3y`      decimal(5,2)  DEFAULT NULL, `ps_pct_5y`  decimal(5,2) DEFAULT NULL, `ps_pct_10y` decimal(5,2) DEFAULT NULL,
  `dy_pct_3y`      decimal(5,2)  DEFAULT NULL, `dy_pct_5y`  decimal(5,2) DEFAULT NULL, `dy_pct_10y` decimal(5,2) DEFAULT NULL,
  -- 每指标 3 窗口的 分位/极值/中位数/z/样本数，供将来读表出曲线
  `pe_stat`        json          NULL, `pb_stat`     json NULL,
  `ps_stat`        json          NULL, `dy_stat`     json NULL,
  -- 每指标独立状态：个股可能仅 dy 无有效值，其余三个照常出分位
  `status_pe`     varchar(16)   NOT NULL DEFAULT 'ok' COMMENT 'ok/loss/no_data',
  `status_pb`     varchar(16)   NOT NULL DEFAULT 'ok',
  `status_ps`      varchar(16)   NOT NULL DEFAULT 'ok',
  `status_dy`      varchar(16)   NOT NULL DEFAULT 'ok',
  `status_note`    varchar(255)  DEFAULT NULL,
  `windows_short`  json          NULL COMMENT '按 metric 分组的短窗口标记（n<250）',
  `window_cut`     json          NULL COMMENT '本行 3/5/10y 实际左界，口径审计用',
  -- P1（2026-11-04）：窗口左界早于该 metric 有效值首日 ⇒ 名义年限 > 实际覆盖。
  -- 只做标记，分位数值、status 三态一律不动（docs/估值分位落盘问题优化方案 §2.1 方案 A）。
  `windows_undercovered` json NULL COMMENT '按 metric 分组的覆盖不足窗口（左界早于数据首日）',
  `first_date`     date          DEFAULT NULL,
  `raw_n`          int           DEFAULT NULL,
  `cleaned_n_pe`   int           DEFAULT NULL, `cleaned_n_pb`  int DEFAULT NULL,
  `cleaned_n_ps`   int           DEFAULT NULL, `cleaned_n_dy`  int DEFAULT NULL,
  `source_as_of`   date          DEFAULT NULL COMMENT '写入时上游源末日，读取侧判新鲜度',
  `input_fp`       varchar(64)   DEFAULT NULL COMMENT '4 指标源序列指纹（Python 侧 sha256）',
  `calc_at`        datetime      NOT NULL COMMENT '本行计算时刻',
  `create_time`    datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`    datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  -- 哨兵列：不在写库白名单内，重算时不得覆盖人工值
  `note`           varchar(64)  DEFAULT NULL COMMENT '人工标注（哨兵列，重算不覆盖）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_bvp` (`subject_type`,`symbol`,`as_of`),
  KEY `k_bvp_asof` (`as_of`),
  KEY `k_bvp_calc` (`calc_at`)
) {TAIL} COMMENT='估值分位按日落盘（4指标×3/5/10年，日历对日窗口）';
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
            print(f"    {c[0]:<18} {c[1]:<14} null={c[2]:<3} default={c[3]}")
        cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (TABLE,))
        print(f"  索引条目数 = {cur.fetchone()[0]}")
    else:
        print("  （dry-run 未建表，无复核结果）")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
