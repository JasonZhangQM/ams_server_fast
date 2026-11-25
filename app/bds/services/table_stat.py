# -*- coding: utf-8 -*-
"""列表页总数缓存（bds_table_stat）：无筛选分页总数 O(1) 读取。

背景（2026-10-16 实测，详见 docs/每日估值页面首屏加载优化方案_2026-10-16.md）：
bds 列表端点用 `query.count()` 取分页 total。在两张 600 万行级日频表上，无筛选时
`COUNT(*)` 需 5~8 秒（innodb_buffer_pool_size=128MB，二级索引 192MB 全读盘，重复
执行也不复用缓存），占首屏 7.89s 的 87%；而真正取数
`ORDER BY trade_date DESC LIMIT 20` 只要 6ms。

方案（L1-b）：用一张小表缓存「每张表的总行数」，无筛选时直接读（O(1)）；
有筛选时仍走精确 `COUNT(*)`（走索引，毫秒级）。写入时机 = 批量同步收尾重算。

覆盖范围：两张 6M 日频表 + history_market + option-market + 财务四表 + 累计型基础/宏观七表。
- 两张日频表 / history_market：无筛选 COUNT(*) 慢到必须缓存（百万~千万行级）。
- option-market（数百行）、财务四表（各 35 万行级）、七张累计型表（当前 0~11 万行）：
  登记目的不是当下性能，而是与列表页 50k 分页护栏、前端 setPaginationTotal 的
  「≈」口径保持全站一致，且这些表随交易日/刷新持续累积，将来 COUNT 会退化
  （用户 2026-11-15 要求）。财务四表同属批量同步 TABLES，收尾 targets=tables 自动重算；
  七张累计型表由三条同步线程在 finally 里按各自 targets 刷新。
其余未登记的 bds 列表表 count 本就是毫秒级，仍不登记。
要扩只需在 STAT_TABLES 加一行（刷新自动纳入）。
⚠️ history_market（`bds_history_market`，个股全量回填后可达 2000 万行级）在批量同步收尾
重算时会做一次全表 COUNT，耗时较长但发生在任务提交之后，不阻塞界面。
⚠️ option-market 不走批量同步：独立期权线程在 finally 刷新；宏观卡片 option_market 段
   由 _run_sync_thread 收尾把 stage 名映射成 option-market 后一并刷新。
⚠️ 漏刷任一已登记表 ⇒ 24h 后其列表页 total 回落 information_schema 估算并显示「≈」。

用法：
    from server_fast.app.bds.services.table_stat import (
        resolve_total, resolve_cached_total, refresh_table_stat)

    total, estimated = resolve_total(db, query, DailyValuation, has_filter=bool(symbol or start_date))
    total, estimated = resolve_cached_total(db, "bds_index_valuation")  # 原生 SQL 端点，无 Query 对象
    refresh_table_stat(db, targets=['daily-valuation'])   # 批量同步收尾调用
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.app.bds.models import (
    BdsTableStat,
    DailyIndicator,
    DailyMktvalue,
    DailyValuation,
    EconomicIndicator,
    FinanceDeriv,
    FundBalance,
    FundCashflow,
    FundIncome,
    GoldReserve,
    HistoryMarket,
    IndexConstituent,
    IndexValuation,
    OptionInfo,
    OptionMarket,
    TradeDate,
    ValuationPercentile,
)

# 同步目标名 → ORM 模型。key 由各同步链路自带标识决定，调用方直接把 targets 传进来，
# 未登记的静默跳过：
#   - 批量同步 TABLES：daily-valuation / daily-mktvalue / fund-* / finance-deriv / history_market
#   - 期权行情：option-market（独立线程直接传；宏观卡片 option_market 段由收尾映射进来）
#   - 宏观分段同步 stages：trade_date / option_info / wscn / daily / gold（本身就是 stage 名）
#   - 指数成分 / 估值线程：index-constituent / index-valuation
# history_market：个股+指数历史行情，回填后可达 2000 万行级，无筛选 COUNT(*) 显著变慢
# 财务四表：各 35 万行级，为全站 50k 护栏口径一致而登记
# 累计型七表：交易日历 / 宏观三表 / 期权合约字典 / 指数成分 / 指数估值，随同步持续累积
STAT_TABLES: dict[str, type] = {
    "daily-valuation": DailyValuation,
    "daily-mktvalue": DailyMktvalue,
    "history_market": HistoryMarket,
    "option-market": OptionMarket,
    "fund-balance": FundBalance,
    "fund-income": FundIncome,
    "fund-cashflow": FundCashflow,
    "finance-deriv": FinanceDeriv,
    # ↓ 宏观分段同步的 stage 名直接作 key ⇒ _run_sync_thread 收尾 targets=stages 自动命中
    "trade_date": TradeDate,
    "option_info": OptionInfo,
    "wscn": EconomicIndicator,
    "daily": DailyIndicator,
    "gold": GoldReserve,
    # ↓ 两条独立指数同步线程
    "index-constituent": IndexConstituent,
    "index-valuation": IndexValuation,
    # ↓ 估值分位落盘（独立卡片线程收尾刷新）：全市场后 1,800 万行级 + JSON 宽列，
    #   无筛选 COUNT(*) 慢到必须缓存（2026-11-25 对齐 history_market 口径）
    "valuation-percentile": ValuationPercentile,
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


def resolve_cached_total(db: Session, table_name: str) -> tuple[int, bool]:
    """读「无筛选」总数缓存，返回 (total, total_is_estimated)。

    与 resolve_total 的 has_filter=False 分支**共用唯一实现**（禁止另写一份判定）：
    命中且未过期（is_exact=1 且 update_time 在 STAT_MAX_AGE 内）⇒ 精确历史值；
    行缺失/过旧 ⇒ 回落 information_schema 估算，estimated=True（前端标「共 ≈N 条」）。

    专供没有 ORM Query 对象的调用方（如 `index_valuation.list_paged` 用原生 text SQL）：
    它们自己负责「有筛选走精确 COUNT」，无筛选时调本函数即可。
    """
    row = (
        db.query(BdsTableStat)
        .filter(BdsTableStat.table_name == table_name)
        .first()
    )
    if row is not None:
        if not row.is_exact:
            return int(row.row_count), True
        if row.update_time and datetime.now() - row.update_time <= STAT_MAX_AGE:
            return int(row.row_count), False

    return estimate_total(db, table_name), True


def resolve_total(
    db: Session, query, model, has_filter: bool
) -> tuple[int, bool]:
    """解析列表端点的分页 total，返回 (total, total_is_estimated)。

    - has_filter=True  → 精确 `COUNT(*)`（走索引，毫秒级），estimated=False。
      注意：`start_date` 语义是「起始日」（>=），选到早年时匹配行数可达 600 万行，
      count 会退化到 3~10s（2026-10-16 实测 ~1.4µs/行，磁盘瓶颈）；实际使用基本
      不会这么筛，故仍取精确值。
    - has_filter=False → 复用 resolve_cached_total 读 bds_table_stat。
    """
    if has_filter:
        return int(query.count()), False
    return resolve_cached_total(db, model.__tablename__)


def refresh_table_stat(db: Session, targets: Optional[list[str]] = None) -> dict[str, int]:
    """重算并写回 bds_table_stat，返回 {表名: 精确行数}。

    :param targets: 同步目标名列表（批量同步 TABLES 取值 / 宏观 stage 名 / 线程标识，
                    如 ['daily-valuation']、['trade_date','wscn']、['index-valuation']）；
                    未登记（不需要缓存）的目标名静默跳过，便于直接传批量同步的 tables
                    或宏观同步的 stages；None = 全部登记表。

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
