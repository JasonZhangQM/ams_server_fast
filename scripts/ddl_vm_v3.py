# -*- coding: utf-8 -*-
"""估值监测 v3 DDL —— 建表 / 加列 / 加外键（阶段一）与删列 / 删表（阶段二）

用法：
    conda run -n fast311 python scripts/ddl_vm_v3.py                # 阶段一 dry-run（只打印）
    conda run -n fast311 python scripts/ddl_vm_v3.py --apply        # 阶段一 执行
    conda run -n fast311 python scripts/ddl_vm_v3.py --stage 2      # 阶段二 dry-run
    conda run -n fast311 python scripts/ddl_vm_v3.py --stage 2 --apply

设计要点（对应 docs/估值监测版本化改造方案_v3_2026-10-23.md）：
- 阶段一**只加不删**：先建表、加列、加外键，把数据搬过去之后才允许阶段二删列。
  顺序反了 theme 和那 3 行笔记就只剩 backup/ 里的 dump 可捞。
- 全部幂等：建表 IF NOT EXISTS、加列/加外键前查 information_schema。
- 每张新表显式写 ENGINE/CHARSET/COLLATE=utf8mb4_0900_ai_ci，与主表 symbol 列逐字一致，
  否则建外键报 errno 3780 / 1005（本库已有 irs_discount_monitor 用 unicode_ci 的混用先例）。
"""
from __future__ import annotations

import argparse
import sys
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


def connect():
    return pymysql.connect(
        host=ENV.get("DB_QUZU", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=True)


# ---------------------------------------------------------------- 阶段一 DDL
TAIL = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"

CREATE_TABLE_SQL = {
    "irs_vm_version": f"""
CREATE TABLE IF NOT EXISTS `irs_vm_version` (
  `id`             bigint        NOT NULL AUTO_INCREMENT,
  `symbol`         varchar(32)   NOT NULL COMMENT '标的代码（irs_value_monitor.symbol）',
  `effective_date` date          NOT NULL COMMENT '生效日期（自该日起适用，含当日）',
  `pp_el`          decimal(12,4) DEFAULT NULL COMMENT '极低',
  `pp_l`           decimal(12,4) DEFAULT NULL COMMENT '低',
  `pp_m`           decimal(12,4) DEFAULT NULL COMMENT '中',
  `pp_h`           decimal(12,4) DEFAULT NULL COMMENT '高',
  `pp_eh`          decimal(12,4) DEFAULT NULL COMMENT '极高',
  `note`           varchar(64)   DEFAULT NULL COMMENT '调整理由（一句话）',
  `create_time`    datetime      NOT NULL DEFAULT (now()),
  `update_time`    datetime      NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_vmv_symbol_date` (`symbol`,`effective_date`),
  KEY `k_vmv_date` (`effective_date`),
  CONSTRAINT `fk_vmv_symbol` FOREIGN KEY (`symbol`) REFERENCES `irs_value_monitor` (`symbol`) ON DELETE RESTRICT
) {TAIL} COMMENT='估值监测档位版本';
""",
    "irs_vm_research": f"""
CREATE TABLE IF NOT EXISTS `irs_vm_research` (
  `id`             bigint       NOT NULL AUTO_INCREMENT,
  `symbol`         varchar(32)  NOT NULL COMMENT '标的代码（irs_value_monitor.symbol）',
  `research_date`  date         NOT NULL COMMENT '研究定稿日：结论自该日起成立',
  `thesis`         varchar(200) NULL COMMENT '一句话本质：买它本质上在买什么',
  `conclusion`     varchar(8)   NULL COMMENT '结论：买入/增持/持有/观望/回避/不入池',
  `max_risk`       varchar(200) NULL COMMENT '最大风险：最可能让我亏钱的那件事',
  `expectation_gap` text       NULL COMMENT '预期差：市场已定价什么 / 我认为未定价什么',
  `pre_condition`  varchar(200) NULL COMMENT '前置条件（组合约束，与标的质地分开写）',
  `next_review`    date         NULL COMMENT '下次复核日期',
  `research_note`  varchar(128) NULL COMMENT '知识库研究报告名（双链名，不含 .md）',
  `research_brief` text         NULL COMMENT '结论摘要（<=800 字，禁止抄报告正文）',
  `research_log`   text         NULL COMMENT '研究过程：怎么得出结论 / 换过什么锚 / 推翻过什么',
  `create_time`    datetime     NOT NULL DEFAULT (now()),
  `update_time`    datetime     NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_vmr_symbol_date` (`symbol`,`research_date`),
  KEY `k_vmr_date` (`research_date`),
  CONSTRAINT `fk_vmr_symbol` FOREIGN KEY (`symbol`) REFERENCES `irs_value_monitor` (`symbol`) ON DELETE RESTRICT
) {TAIL} COMMENT='标的研究定稿（按日期留版）';
""",
    "irs_vm_logic": f"""
CREATE TABLE IF NOT EXISTS `irs_vm_logic` (
  `id`         bigint       NOT NULL AUTO_INCREMENT,
  `symbol`     varchar(32)  NOT NULL COMMENT '标的代码',
  `seq`        int          NOT NULL COMMENT '序号 1-4',
  `content`    varchar(200) NOT NULL COMMENT '逻辑：逻辑->影响路径->是否已定价',
  `pricing`    varchar(8)   NULL COMMENT '是否已被定价：未定价/部分/已充分',
  `state`      varchar(8)   NOT NULL DEFAULT '完好' COMMENT '状态：完好/减弱/破坏',
  `state_at`   datetime     NULL COMMENT '状态最后变更时间',
  `evidence`   varchar(200) NULL COMMENT '证据（报告章节 / 数据代码 / 指标）',
  `create_time` datetime    NOT NULL DEFAULT (now()),
  `update_time` datetime    NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_vml_symbol_seq` (`symbol`,`seq`),
  KEY `k_vml_state` (`state`),
  CONSTRAINT `fk_vml_symbol` FOREIGN KEY (`symbol`) REFERENCES `irs_value_monitor` (`symbol`) ON DELETE RESTRICT
) {TAIL} COMMENT='标的投资逻辑条目';
""",
    "irs_vm_invalidate": f"""
CREATE TABLE IF NOT EXISTS `irs_vm_invalidate` (
  `id`           bigint       NOT NULL AUTO_INCREMENT,
  `symbol`       varchar(32)  NOT NULL COMMENT '标的代码',
  `logic_id`     bigint       NULL COMMENT '对应的逻辑条目（可空）',
  `seq`          int          NOT NULL COMMENT '序号',
  `proposition`  varchar(200) NOT NULL COMMENT '被证伪的命题',
  `signal`       varchar(200) NOT NULL COMMENT '证伪信号 + 阈值（必须可量化）',
  `source`       varchar(64)  NULL COMMENT '数据源',
  `action`       varchar(32)  NULL COMMENT '触发后动作：减仓/清仓/重估/暂停加仓',
  `status`       varchar(8)   NOT NULL DEFAULT '未触发' COMMENT '未触发/已触发/已失效',
  `triggered_at` date         NULL COMMENT '触发日',
  `create_time`  datetime     NOT NULL DEFAULT (now()),
  `update_time`  datetime     NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_vmi_symbol_seq` (`symbol`,`seq`),
  KEY `k_vmi_status` (`status`),
  CONSTRAINT `fk_vmi_symbol` FOREIGN KEY (`symbol`) REFERENCES `irs_value_monitor` (`symbol`) ON DELETE RESTRICT
) {TAIL} COMMENT='标的证伪条件登记';
""",
    "irs_vm_plan": f"""
CREATE TABLE IF NOT EXISTS `irs_vm_plan` (
  `id`              bigint        NOT NULL AUTO_INCREMENT,
  `vm_symbol`       varchar(32)   NOT NULL COMMENT '标的代码（irs_value_monitor.symbol）',
  `trade_symbol`    varchar(32)   NOT NULL COMMENT '交易代码（真实合约）',
  `seq`             int           NOT NULL COMMENT '序号',
  `b_s`             varchar(4)    NOT NULL COMMENT '买/卖',
  `o_c`             varchar(4)    NULL COMMENT '开/平',
  `trigger_op`      varchar(8)    NOT NULL DEFAULT 'lte' COMMENT '触发方向：lte(<=)/gte(>=)/range(区间)',
  `trigger_price`   decimal(12,4) NULL COMMENT '触发点位（与 trigger_ref 二选一）',
  `trigger_ref`     varchar(8)    NULL COMMENT '触发价引用档位：pp_el/pp_l/pp_m/pp_h/pp_eh',
  `trigger_price2`  decimal(12,4) NULL COMMENT '区间上沿（trigger_op=range 时必填）',
  `qty`             int           NULL COMMENT '每次执行手数（空=到价再定量）',
  `reason`          varchar(200)  NULL COMMENT '为什么这么做（对应哪条逻辑）',
  `is_cancelled`    tinyint(1)    NOT NULL DEFAULT 0 COMMENT '手动作废',
  `create_time`     datetime      NOT NULL DEFAULT (now()),
  `update_time`     datetime      NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_vmp_symbol_seq` (`vm_symbol`,`seq`),
  KEY `k_vmp_trade` (`trade_symbol`),
  CONSTRAINT `fk_vmp_vm` FOREIGN KEY (`vm_symbol`) REFERENCES `irs_value_monitor` (`symbol`) ON DELETE RESTRICT
) {TAIL} COMMENT='标的剧本';
""",
    "bills_bill_note": f"""
CREATE TABLE IF NOT EXISTS `bills_bill_note` (
  `id`         bigint   NOT NULL AUTO_INCREMENT,
  `bill_id`    bigint   NULL COMMENT '关联 bills_bill.id；成交被删时置 NULL，靠 biz_key 找回',
  `biz_key`    char(32) NOT NULL COMMENT '成交自然键 md5（与 uk_bills_bill 同口径）',
  `kind`       varchar(8) NOT NULL COMMENT '类型：想法（成交当时写）/ 复盘（事后追加）',
  `content`    text     NOT NULL COMMENT '正文',
  `content_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '落笔时间（后端填，前端不可传）',
  `create_time` datetime NOT NULL DEFAULT (now()),
  `update_time` datetime NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_bbn_bill_kind` (`bill_id`,`kind`),
  KEY `k_bbn_biz` (`biz_key`),
  CONSTRAINT `fk_bbn_bill` FOREIGN KEY (`bill_id`) REFERENCES `bills_bill` (`id`) ON DELETE SET NULL
) {TAIL} COMMENT='成交笔记';
""",
}

ADD_COLUMN_SQL = [
    ("irs_value_monitor", "theme",
     "ALTER TABLE `irs_value_monitor` ADD COLUMN `theme` VARCHAR(32) NULL "
     "COMMENT '主题（黄金/中国宽基/美国宽基/银行/家电…）' AFTER `name`"),
    # object_type（标的类型：股权/商品/指数/其他）已于 2026-10-23 按用户要求删除，
    # 见 scripts/ddl_drop_object_type.py。**不要取消注释**：重跑本脚本会把它加回来，
    # 而模型/接口/前端都已不再有这一列，加回来只会造成"库有代码无"的静默漂移。
    # ("irs_value_monitor", "object_type",
    #  "ALTER TABLE `irs_value_monitor` ADD COLUMN `object_type` VARCHAR(8) NULL "
    #  "COMMENT '标的类型：股权/商品/指数/其他' AFTER `theme`"),
    ("bills_group_symbol", "vm_symbol",
     "ALTER TABLE `bills_group_symbol` ADD COLUMN `vm_symbol` VARCHAR(32) NULL "
     "COMMENT '归属标的（irs_value_monitor.symbol）；融资融券与未归属为 NULL' AFTER `diff_dwt`"),
]

ADD_FK_SQL = [
    ("bills_group_symbol", "fk_bgs_vm",
     "ALTER TABLE `bills_group_symbol` ADD CONSTRAINT `fk_bgs_vm` FOREIGN KEY (`vm_symbol`) "
     "REFERENCES `irs_value_monitor` (`symbol`) ON DELETE RESTRICT"),
]

# ---------------------------------------------------------------- 阶段二 DDL
DROP_COLUMNS = ["logic", "plan", "invalidate", "review", "theme", "note_at"]
DROP_TABLE = "bills_group_symbol_note_log"


def has_table(cur, t: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (t,))
    return cur.fetchone() is not None


def has_column(cur, t: str, c: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s", (t, c))
    return cur.fetchone() is not None


def has_fk(cur, t: str, name: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s", (t, name))
    return cur.fetchone() is not None


def table_columns(cur, t: str) -> list[str]:
    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (t,))
    return [r[0] for r in cur.fetchall()]


def run_stage1(apply: bool) -> None:
    conn = connect()
    cur = conn.cursor()
    print("=" * 78)
    print("阶段一：建表 / 加列 / 加外键（只加不删）", "【APPLY】" if apply else "【DRY-RUN】")
    print("=" * 78)

    # 0) 前置：symbol 列必须是 varchar(32) + utf8mb4_0900_ai_ci，否则 FK 建不起来
    cur.execute("SELECT COLUMN_TYPE, COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='irs_value_monitor' AND COLUMN_NAME='symbol'")
    row = cur.fetchone()
    print(f"\n[0] 被引用列 irs_value_monitor.symbol = {row[0]} / {row[1]}")
    if row[0] != "varchar(32)" or row[1] != "utf8mb4_0900_ai_ci":
        print("  !! 列类型或排序规则不符，外键会失败，中止")
        return

    # 1) 主表加列（必须先于子表，否则建表时 FK 找不到引用列？实际引用的是 symbol，不受影响；
    #    但把主表放前面可保证"先主表后子表"的阅读顺序与方案一致）
    print("\n[1] 主表 / 合约表 加列")
    for table, col, sql in ADD_COLUMN_SQL:
        if has_column(cur, table, col):
            print(f"  - {table}.{col} 已存在，跳过")
            continue
        print(f"  + {table}.{col}")
        if apply:
            cur.execute(sql)

    # 2) 建 6 张新表
    print("\n[2] 建新表")
    for name, sql in CREATE_TABLE_SQL.items():
        if has_table(cur, name):
            print(f"  - {name} 已存在，跳过")
            continue
        print(f"  + {name}")
        if apply:
            cur.execute(sql)

    # 3) 合约表加外键（vm_symbol 有值才建得起来；此时一般为空）
    print("\n[3] 外键")
    for table, fk, sql in ADD_FK_SQL:
        if has_fk(cur, table, fk):
            print(f"  - {table}.{fk} 已存在，跳过")
            continue
        print(f"  + {table}.{fk} -> irs_value_monitor.symbol (RESTRICT)")
        if apply:
            cur.execute(sql)

    # 4) 复核
    print("\n[4] 复核")
    for name in CREATE_TABLE_SQL:
        if has_table(cur, name):
            cols = table_columns(cur, name)
            print(f"  {name:<22} {len(cols):>2} 列  {cols}")
        else:
            print(f"  {name:<22} 不存在")
    cur.close()
    conn.close()


def run_stage2(apply: bool) -> None:
    conn = connect()
    cur = conn.cursor()
    print("=" * 78)
    print("阶段二：删 6 个笔记列 + DROP 留痕表（不可逆）", "【APPLY】" if apply else "【DRY-RUN】")
    print("=" * 78)

    # 前置：确认数据已经搬到新表（判据不是"旧列是否为空"——旧列本来就会保留原文直到 DROP）
    checks = []
    cur.execute("SELECT COUNT(*) FROM bills_bill_note")
    checks.append(("bills_bill_note 行数（应=3）", cur.fetchone()[0], 3))
    cur.execute("SELECT COUNT(*) FROM irs_vm_research WHERE symbol='GC00Y' "
                "AND COALESCE(research_log,'')<>''")
    checks.append(("irs_vm_research(GC00Y, 有 research_log)", cur.fetchone()[0], 1))
    cur.execute("SELECT COUNT(*) FROM irs_vm_plan WHERE vm_symbol='GC00Y'")
    checks.append(("irs_vm_plan(GC00Y) 条数（应>=4）", cur.fetchone()[0], 4))
    cur.execute("SELECT COUNT(*) FROM irs_vm_invalidate WHERE symbol='GC00Y'")
    checks.append(("irs_vm_invalidate(GC00Y)", cur.fetchone()[0], 1))
    cur.execute("SELECT COUNT(*) FROM irs_value_monitor WHERE symbol='GC00Y' AND theme='黄金'")
    checks.append(("主表 GC00Y.theme=黄金", cur.fetchone()[0], 1))
    cur.execute("SELECT COUNT(*) FROM irs_vm_version")
    checks.append(("irs_vm_version 行数（应=8）", cur.fetchone()[0], 8))
    cur.execute("SELECT COUNT(*) FROM bills_group_symbol WHERE vm_symbol IS NOT NULL")
    checks.append(("vm_symbol 已填行数（应=5）", cur.fetchone()[0], 5))

    print("\n[0] 迁移完成校验")
    bad = []
    for name, got, want in checks:
        ok = got >= want
        print(f"  {'OK ' if ok else '!! '} {name:<38} {got}")
        if not ok:
            bad.append(name)
    if bad:
        print("  !! 迁移未完成，拒绝删列。先跑 migrate_group_symbol_notes.py --apply")
        cur.close()
        conn.close()
        return

    print("\n[1] 删列")
    for col in DROP_COLUMNS:
        if not has_column(cur, "bills_group_symbol", col):
            print(f"  - {col} 已不存在，跳过")
            continue
        print(f"  - DROP bills_group_symbol.{col}")
        if apply:
            cur.execute(f"ALTER TABLE `bills_group_symbol` DROP COLUMN `{col}`")

    print("\n[2] 删留痕表")
    if not has_table(cur, DROP_TABLE):
        print(f"  - {DROP_TABLE} 已不存在，跳过")
    else:
        cur.execute(f"SELECT COUNT(*) FROM `{DROP_TABLE}`")
        n = cur.fetchone()[0]
        print(f"  - DROP {DROP_TABLE}（{n} 行，已备份）")
        if apply:
            cur.execute(f"DROP TABLE `{DROP_TABLE}`")

    print("\n[3] 复核 bills_group_symbol 列")
    print("  ", table_columns(cur, "bills_group_symbol"))
    cur.close()
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, default=1, choices=[1, 2])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.stage == 1:
        run_stage1(args.apply)
    else:
        run_stage2(args.apply)


if __name__ == "__main__":
    main()
