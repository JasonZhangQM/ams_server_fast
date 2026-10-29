# -*- coding: utf-8 -*-
"""全链路整合方案 P1/P2/P3 DDL —— 加列 / 建表（2026-10-28）。

覆盖（方案文档「分期与验收」表改动列）：
- P1  3 列：irs_vm_plan.kind、bills_bill_note.source、bills_bill_note.plan_id
- P2  2 列 + 1 表：irs_vm_invalidate.judge / judge_expr、irs_vm_state_log
- P3  1 表：irs_vm_target

全部幂等：加列/建表前查 information_schema，已存在则跳过。
数据回填（note.source 判定、state_log 首次登记）不在此脚本，见
scripts/migrate_full_link_20261028.py。

用法：
    conda run -n fast311 python scripts/ddl_full_link_20261028.py            # dry-run
    conda run -n fast311 python scripts/ddl_full_link_20261028.py --apply     # 执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def load_env() -> dict:
    env = {}
    env_path = Path(__file__).resolve().parents[1] / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
TAIL = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"


def connect():
    return pymysql.connect(
        host=ENV.get("DB_HOST", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=True)


ADD_COLUMNS = [
    # 注：irs_vm_plan.kind 已于 2026-10-29 随「只保留剧本」决策删除
    # （见 ddl_drop_plan_order_20261029.py），此处不再回加。
    ("bills_bill_note", "source",
     "ALTER TABLE `bills_bill_note` ADD COLUMN `source` VARCHAR(8) NOT NULL DEFAULT 'manual' "
     "COMMENT 'manual=成交当日或次日写；backfill=事后补录（>3 天）' AFTER `content_at`"),
    ("bills_bill_note", "plan_id",
     "ALTER TABLE `bills_bill_note` ADD COLUMN `plan_id` INT NULL "
     "COMMENT 'irs_vm_plan.id（裸 ID，无外键）' AFTER `source`"),
    ("irs_vm_invalidate", "judge",
     "ALTER TABLE `irs_vm_invalidate` ADD COLUMN `judge` VARCHAR(16) NULL "
     "COMMENT '判定类别：price/percentile/discount/manual' AFTER `action`"),
    ("irs_vm_invalidate", "judge_expr",
     "ALTER TABLE `irs_vm_invalidate` ADD COLUMN `judge_expr` VARCHAR(64) NULL "
     "COMMENT '判据表达式（如 price<=3900）' AFTER `judge`"),
]

CREATE_TABLES = {
    "irs_vm_state_log": f"""
CREATE TABLE IF NOT EXISTS `irs_vm_state_log` (
  `id`           bigint       NOT NULL AUTO_INCREMENT,
  `subject_type` varchar(16)  NOT NULL COMMENT 'logic=逻辑；invalidate=证伪',
  `subject_id`   int          NOT NULL COMMENT '对应 irs_vm_logic.id / irs_vm_invalidate.id',
  `symbol`       varchar(32)  NOT NULL COMMENT '标的代码',
  `from_state`   varchar(16)  NULL COMMENT '变更前（首次登记为 NULL）',
  `to_state`     varchar(16)  NOT NULL COMMENT '变更后',
  `changed_at`   datetime     NOT NULL COMMENT '变更时间',
  `note`         varchar(200) NULL COMMENT '变更理由/证据（可选）',
  `create_time`  datetime     NOT NULL DEFAULT (now()),
  `update_time`  datetime     NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  KEY `idx_subject` (`subject_type`,`subject_id`,`changed_at`)
) {TAIL} COMMENT='逻辑/证伪状态变更日志';
""",
    "irs_vm_target": f"""
CREATE TABLE IF NOT EXISTS `irs_vm_target` (
  `id`          bigint        NOT NULL AUTO_INCREMENT,
  `symbol`      varchar(32)   NOT NULL COMMENT '标的（vm_symbol）',
  `target_pct`  decimal(10,4) NULL COMMENT '目标仓位占总资产比例（%）',
  `note`        varchar(200)  NULL COMMENT '目标仓位说明',
  `create_time` datetime      NOT NULL DEFAULT (now()),
  `update_time` datetime      NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_target_symbol` (`symbol`)
) {TAIL} COMMENT='标的级目标仓位';
""",
}


def has_table(cur, t: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (t,))
    return cur.fetchone() is not None


def has_column(cur, t: str, c: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s", (t, c))
    return cur.fetchone() is not None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只 dry-run）")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    print("=" * 74)
    print("全链路整合方案 DDL", "【APPLY】" if args.apply else "【DRY-RUN】")
    print("=" * 74)

    print("\n[1] 加列")
    for table, col, sql in ADD_COLUMNS:
        if has_column(cur, table, col):
            print(f"  - {table}.{col} 已存在，跳过")
            continue
        print(f"  + {table}.{col}")
        if args.apply:
            cur.execute(sql)

    print("\n[2] 建表")
    for name, sql in CREATE_TABLES.items():
        if has_table(cur, name):
            print(f"  - {name} 已存在，跳过")
            continue
        print(f"  + {name}")
        if args.apply:
            cur.execute(sql)

    print("\n[3] 复核")
    for table, col, _ in ADD_COLUMNS:
        print(f"  {table}.{col:<30} {'OK' if has_column(cur, table, col) else 'MISSING'}")
    for name in CREATE_TABLES:
        print(f"  {name:<30} {'OK' if has_table(cur, name) else 'MISSING'}")

    cur.close()
    conn.close()
    print("\n" + ("已执行完毕。" if args.apply else "DRY-RUN 完成，未写库。确认后加 --apply 重跑。"))


if __name__ == "__main__":
    main()