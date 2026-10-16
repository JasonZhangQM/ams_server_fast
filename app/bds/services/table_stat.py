# -*- coding: utf-8 -*-
"""列表页总数缓存（bds_table_stat）：无筛选分页总数 O(1) 读取。

背景（2026-10-16 实测，详见 docs/每日估值页面首屏加载优化方案_2026-10-16.md）：
bds 列表端点用 `query.count()` 取分页 total。在两张 600 万行级日频表上，无筛选时
`COUNT(*)` 需 5~8 秒（innodb_buffer_pool_size=128MB，二级索引 192MB 全读盘，重复
执行也不复用缓存），占首屏 7.89s 的 87%；而真正取数
`ORDER BY trade_date DESC LIMIT 20` 只要 6ms。

方案（L1-b）：用一张小表缓存「每张表的总行数」，无筛选时直接读（O(1)）；
有筛选时仍走精确 `COUNT(*)`（走索引，毫秒级）。写入时机 = 批量同步收尾重算。

覆盖范围：只登记「无筛选 count 慢到需要缓存」的表（当前两张 6M 日频表）。
其余 bds 列表表（最大 11.5 万行）count 本就是毫秒级，纳入只会把精确总数换成
可能陈旧的值，零收益 ⇒ 不登记。要扩只需在 STAT_TABLES 加一行（刷新自动纳入）。

用法：
    from server_fast.app.bds.services.table_stat import resolve_total, refresh_table_stat

    total, estimated = resolve_total(db, query, DailyValuation, has_filter=bool(symbol or start_date))
    refresh_table_stat(db, targets=['daily-valuation'])   # 批量同步收尾调用
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.app.bds.models import BdsTableStat, DailyMktvalue, DailyValuation

# 同步目标名（= sync_runner.config.TABLES 的取值）→ ORM 模型
STAT_TABLES: dict[str, type] = {
    "daily-valuation": DailyValuation,
    "daily-mktvalue": DailyMktvalue,
}

# 缓存有效期：超过则视为过旧 → 回落 information_schema 估算（并让前端标 ≈）。
# 批量同步（每日一次量级）在收尾重算，正常始终落在有效期内。
STAT_MAX_AGE = timedelta(hours=24)


def estimate_total(db: Session, table_name: str) -> int:
    """用 information_schema 估算表行数（InnoDB 统计值，实测偏差约 −7.5%）。

    O(1) 读取、不做任何扫描，仅在计数表缺失/过旧时作为兜底。
    目标表不存在时返回 0。
    """
    sql = text(
        "SELECT TABLE_ROWS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    )
    value = db.execute(sql, {"t": table_name}).scalar()
    return int(value or 0)


def resolve_total(
    db: Session, query, model, has_filter: bool
) -> tuple[int, bool]:
    """解析列表端点的分页 total，返回 (total, total_is_estimated)。

    - has_filter=True  → 精确 `COUNT(*)`（走索引，毫秒级），estimated=False。
      注意：`start_date` 语义是「起始日」（>=），选到早年时匹配行数可达 600 万行，
      count 会退化到 3~10s（2026-10-16 实测 ~1.4µs/行，磁盘瓶颈）；实际使用基本
      不会这么筛，故仍取精确值。
    - has_filter=False → 读 bds_table_stat：命中且未过期（is_exact=1 且
      update_time 在 STAT_MAX_AGE 内）⇒ 精确历史值；行缺失/过旧 ⇒ 回落
      information_schema 估算，estimated=True（前端显示「共 ≈N 条」）。
    """
    if has_filter:
        return int(query.count()), False

    row = (
        db.query(BdsTableStat)
        .filter(BdsTableStat.table_name == model.__tablename__)
        .first()
    )
    if row is not None:
        if not row.is_exact:
            return int(row.row_count), True
        if row.update_time and datetime.now() - row.update_time <= STAT_MAX_AGE:
            return int(row.row_count), False

    return estimate_total(db, model.__tablename__), True


def refresh_table_stat(db: Session, targets: Optional[list[str]] = None) -> dict[str, int]:
    """重算并写回 bds_table_stat，返回 {表名: 精确行数}。

    :param targets: 同步目标名列表（sync_runner TABLES 的取值，如['daily-valuation']）；
                    未登记（不需要缓存）的目标名静默跳过，便于直接传批量同步的 tables；
                    None = 全部登记表。

    单表一次全表精确 `COUNT(*)`（600 万行级 5~8s），由批量同步收尾调用；
    手工校准走 POST /bds/refresh-table-stat。异常向上抛出，由调用方决定是否忽略。
    """
    if targets is None:
        names = list(STAT_TABLES)
    else:
        # dict.fromkeys 去重且保序，避免同一目标被重算两次
        names = [t for t in dict.fromkeys(targets) if t in STAT_TABLES]

    result: dict[str, int] = {}
    for name in names:
        table = STAT_TABLES[name].__tablename__
        row_count = int(db.query(STAT_TABLES[name]).count())
        # 统计时间取「数完之后」的时刻，避免出现 update_time < create_time
        now = datetime.now()
        row = (
            db.query(BdsTableStat)
            .filter(BdsTableStat.table_name == table)
            .first()
        )
        if row is None:
            db.add(BdsTableStat(table_name=table, row_count=row_count, is_exact=1, update_time=now))
        else:
            row.row_count = row_count
            row.is_exact = 1
            row.update_time = now
        db.commit()
        result[table] = row_count
    return result
