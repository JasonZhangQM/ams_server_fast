# -*- coding: utf-8 -*-
"""标的复盘 DDL（2026-10-29）。

背景：复盘从「逐笔成交笔记」上移到「标的复盘」，与投研同层，并锚定研究版本。
- 一次复盘回答「这版研究的结论兑现了没有」，必须挂到具体版本；
- 同一版本可反复复盘（持仓中、平仓后、季度末），故不设唯一键；
- review_date 由后端填落笔当天，前端不可传（反自欺）；
- 删除研究版本时级联删除其复盘。

脚本幂等：CREATE TABLE IF NOT EXISTS，可重复执行。

用法：
    conda run -n fast311 python scripts/ddl_vm_review_20261029.py            # dry-run
    conda run -n fast311 python scripts/ddl_vm_review_20261029.py --apply     # 执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DDL = """
CREATE TABLE IF NOT EXISTS `irs_vm_review` (
  `id`           bigint       NOT NULL AUTO_INCREMENT,
  `symbol`       varchar(32)  NOT NULL COMMENT '标的代码',
  `research_id`  bigint       NOT NULL COMMENT '被复盘的研究版本 irs_vm_research.id',
  `content`      text         NOT NULL COMMENT '复盘正文',
  `review_date`  date         NOT NULL COMMENT '落笔日期（后端填，前端不可传）',
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `k_vmr_symbol_date` (`symbol`,`review_date`),
  KEY `k_vmr_research_date` (`research_id`,`review_date`),
  CONSTRAINT `fk_vmr_vm` FOREIGN KEY (`symbol`)
    REFERENCES `irs_value_monitor` (`symbol`) ON DELETE CASCADE,
  CONSTRAINT `fk_vmr_research` FOREIGN KEY (`research_id`)
    REFERENCES `irs_vm_research` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标的复盘（锚定研究版本）'
"""


def load_env() -> dict:
    env = {}
    env_path = Path(__file__).resolve().parents[1] / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行；缺省仅打印计划")
    args = ap.parse_args()

    env = load_env()
    conn = pymysql.connect(
        host=env.get("DB_HOST", "localhost"), port=int(env.get("DB_PORT", 3306)),
        user=env["DB_USER"], password=env["DB_PASSWORD"], database=env["DB_NAME"],
        charset="utf8mb4", autocommit=True)
    try:
        with conn.cursor() as cur:
            if table_exists(cur, "irs_vm_review"):
                cur.execute("SELECT COUNT(*) FROM `irs_vm_review`")
                if cur.fetchone()[0] > 0:
                    print("[abort] irs_vm_review 已有数据，拒绝重建；请手工迁移")
                    return 1
                print(f"[{'apply' if args.apply else 'dry'}] DROP 空表 irs_vm_review（锚点改为研究版本）")
                if args.apply:
                    cur.execute("DROP TABLE `irs_vm_review`")
            print(f"[{'apply' if args.apply else 'dry'}] CREATE TABLE irs_vm_review")
            if args.apply:
                cur.execute(DDL)
                print("[ok] 建表完成")
    finally:
        conn.close()

    if not args.apply:
        print("dry-run 结束；确认后加 --apply 执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
