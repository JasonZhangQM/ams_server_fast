# -*- coding: utf-8 -*-
"""`bds_valuation_percentile` 落表服务（2026-11-04，P2 估值分位按日落盘 B/C 阶段）。

职责：把「每标的每交易日的 3/5/10 年 PE/PB/PS/股息率分位」固化成按 `as_of` 追加的历史表，
把首屏请求链路里同步现算 2.4 s 的分位搬出请求路径。

设计要点（docs/估值分位按日落盘方案_2026-11-04.md）：
- 分位是 `f(subject, metric, window, as_of | 截至 as_of 的上游序列)` 的纯函数，`as_of`
  已把「窗口每天滑动」参数化；时间推移只追加新行，上游历史修订才覆盖旧行。
- 计算统一调公共内核 `percentile_stat.daily_percentiles`（滑动窗口 O(n log n)），
  **禁止复用 `get_series`**（其默认下界 `end-3650 天` 比 10y 对日左界晚 2~3 天，
  会静默少取 1~2 个交易日样本）。
- 指数侧「非空即有效」、个股侧按 `clean_value` 清洗，**两套规则故意不统一**。
- 无效值日照常落行，但按 metric 独立置 status 并把该 metric 的 3 个分位列全置 NULL；
  禁止整行清空（个股 dy_ttm 有 1,136 个 NULL 日，不能连 PE/PB/PS 一起丢）。
- 🔴 修订检测主判据是 Python 侧源序列指纹 `input_fp`，**绝不用 `MAX(update_time)`**：
  `market_data/daily_valuation.py` 对已存在行无条件 setattr 覆盖，重复同步同一天
  会刷时间戳但值没变 ⇒ 100% 误报。
- 写入走 `upsert_df_to_db` + **显式 `fields_pct_update` 白名单**，`note` 人工列不在白名单
  （哨兵：重算不覆盖人工值）。
- 归属 bds（🔒4.12：bds 只能被依赖、禁 import irs）。**2026-11-24 起默认标的范围改由
  证券信息表 `bds_symbol_info` 驱动**（`resolve_scope_subjects`：all=A股指数∪A股个股
  默认 / index / stock / industry，个股档带退市口径三态），取代监测表默认清单；
  显式 `subjects`（单标的/子集/串联链）优先级最高。无估值数据的标的**不做预过滤**，
  逐只走 no_data 跳过路径（全市场 ~5,926 标的量级下逐只 EXISTS 点查不可行）。
  孤儿白名单口径同步改为「SymbolInfo 范围档清单」。

模式：M1 增量追加 / M2 全量重建 / M3 修订区间重算 / auto（**默认、推荐**）。
四者调用同一个纯函数，区别只在「写哪些 as_of 行」——计算始终用全序列（窗口需要历史），
全量组装约 5.4 s。M2 另有「快跳过」：源序列指纹 + 源末日 + 窗口口径三者未变时，
重写与不写逐值全等，直接跳过该 subject（实测 57 s → 约 3 s）。

2026-11-04 P0 修复（docs/估值分位落盘问题优化方案_2026-11-04.md §1）：
- 🔴 `input_fp` 变化（上游历史/同日修订）或窗口口径陈旧 ⇒ **任何模式**都强制该 subject
  全量重建。原 `auto` 的 `_earliest_change_date()` 是占位实现（返回表内最新行），
  修订 10 年前的值时只重写 1 行却把最新行 fp 刷成新值 ⇒ 下次快跳过把错误永久固化。
- 🔴 孤儿清理改为**显式 `prune_orphans` 开关 + 仅默认清单（非显式 subjects）可执行**：
  传 `subjects`（单标的/子集）或 M1/M3 一律不删，避免单标的 M2 误删其余 24,779 行。
  监测表移除标的后历史行默认保留，只在 `result.orphan_in_store` 提示（用户裁定 2）。

2026-11-04 P1 修复（docs/估值分位落盘问题优化方案_2026-11-04.md §2）：
- 🔴 `SCHEMA_EPOCH` 结构变更闸：新增列 / 改口径后，历史行 `calc_at` 早于该时刻 ⇒
  强制全量重建。否则新增列永远等不到回填——快跳过只看 fp / 源末日 / window_cut，
  这三者都没变时会静默跳过，新列 26,816 行全是 NULL。

2026-11-23 瘦身（44 → 28 列，激进版）：删除前端零消费的纯审计列——current_*、
*_stat、windows_short、windows_undercovered（percentile_warnings 链路下线，无调用方）、
status_note、first_date / raw_n / cleaned_n_*。保留：12 分位列 + status 四态 +
window_cut / source_as_of / input_fp / calc_at（三道闸 + 新鲜度）+ note 人工哨兵。
SCHEMA_EPOCH 同步上调触发全量重建回填。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Callable, Optional

import pandas as pd
from sqlalchemy import text

from server_fast.app.bds.services.valuation.percentile_stat import (
    PCT_METRICS, PCT_PREFIX, WINDOW_KEYS,
    clean_value, daily_percentiles, shift_years, window_cuts,
)
from server_fast.common.db import SessionLocal
from server_fast.common.utils import upsert_df_to_db
from server_fast.config import settings
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.services.symbol_info.market_board import INDEX_INDUSTRY

logger = logging.getLogger("uvicorn.error")

TABLE = "bds_valuation_percentile"
# 🔒4.12：bds 只能被依赖。这里只登记**表名字符串**，不 import irs 任何模块/模型；
# 默认标的清单由 subject_diagnostics() 在运行时用原生 SQL 取（方案 §3.1）。
MONITOR_TABLE = "irs_underlying_monitor"
# 口径 A（2026-11-23 用户定稿）：窗口有效样本下限（约一年交易日）。
# n 低于它 ⇒ 该窗口分位不保存（NULL）。与实时分位专页的 short 标记口径一致。
MIN_WINDOW_SAMPLES = 250

# 🔴 表结构 / 落表口径变更时刻：calc_at 早于它的行必须全量重建（P1 新增列时用）。
# 快跳过的三道闸（fp / 源末日 / window_cut）感知不到「多了/改了一列」，
# 没有这道闸，新增列的历史行会永远保持 NULL。以后再改落表结构请同步上调此常量，
# 并把变更原因写进 docs 的实施记录；result.schema_epoch 会把它暴露给排查。
# 2026-11-23 瘦身（44 → 28 列）：删 current_*/ *_stat / windows_short /
# windows_undercovered / status_note / first_date / raw_n / cleaned_n_*，上调触发全量重建。
# 同日口径 A：窗口 n<250 分位不保存（NULL），再次上调（09:00）触发全量重写。
SCHEMA_EPOCH = datetime(2026, 11, 23, 9, 0, 0)

# ⚠️ **兜底常量，不再是默认清单**（2026-11-04 起）：仅当 irs_underlying_monitor
# 查询失败 / 空表时由 subject_diagnostics() 回退，同时作为几个历史探针的固定基线。
# 真正的默认清单 = 运行时从监测表推导 + 过滤无估值数据标的，见下方函数组。
# 保留名字是为了不破坏 probe_percentile / probe_p1 的基线引用。
L1_SUBJECTS: tuple[str, ...] = (
    "SHSE.000300", "SHSE.000905",
    "SHSE.600036", "SHSE.600900", "SZSE.000333", "SZSE.300750",
)

UNIQUE_KEYS = ["subject_type", "symbol", "as_of"]

# 🔒4.9 显式白名单：只覆盖派生列，绝不含 id / create_time / update_time / note。
# 漏传 update_columns 会让 upsert_df_to_db 默认「除唯一键外全更」，
# 将来加人工列时就会把它悄悄覆盖掉（§10 哨兵 4）。
fields_pct_update: list[str] = [
    "pe_pct_3y", "pe_pct_5y", "pe_pct_10y",
    "pb_pct_3y", "pb_pct_5y", "pb_pct_10y",
    "ps_pct_3y", "ps_pct_5y", "ps_pct_10y",
    "dy_pct_3y", "dy_pct_5y", "dy_pct_10y",
    "status_pe", "status_pb", "status_ps", "status_dy",
    "window_cut",
    "source_as_of", "input_fp", "calc_at",
]

def _pct_cols() -> list[str]:
    """12 个分位列名，单一来源（避免两处手写列清单漂移）。"""
    return [f"{PCT_PREFIX[m]}_pct_{k.split('_')[1]}"
            for m in PCT_METRICS for k in WINDOW_KEYS]


PCT_COLUMNS = _pct_cols()


# ---------------------------------------------------------------------------
# 源序列
# ---------------------------------------------------------------------------

# subject_type → 上游估值源表（P1-6：两表末日独立，别再全用个股表）
SOURCE_TABLES: dict[str, str] = {
    "index": "bds_index_valuation",
    "stock": "bds_daily_valuation",
}


def source_end_dates(db) -> dict[str, Optional[date]]:
    """两张上游估值表各自的末日（P1-6）。

    🔒七·4：必须 `ORDER BY trade_date DESC LIMIT 1`，禁 `MAX()`（实测 17.9s vs 0.002s）。
    指数与个股是两套独立同步链路，末日可能差几天；拿个股末日判指数 subject 的
    「表内是否已到最新」会误判成需要全量重建，或反过来漏掉新增交易日。
    """
    return {t: db.execute(text(
        f"SELECT trade_date FROM {table} ORDER BY trade_date DESC LIMIT 1"
    )).scalar() for t, table in SOURCE_TABLES.items()}


def source_end_date(db, subject_type: Optional[str] = None) -> Optional[date]:
    """兼容旧签名：给定类型返回该表末日；不传返回**两表最早末日**（保守）。

    「最早末日」用于读取侧 `WHERE as_of >= end - 15 天`：指数表落后时不能被整体漏掉；
    `plan()` / 两个历史探针仍按无参调用，行为与改造前一致（当前两表末日相同）。
    """
    if subject_type is not None:
        table = SOURCE_TABLES.get(subject_type, SOURCE_TABLES["stock"])
        return db.execute(text(
            f"SELECT trade_date FROM {table} ORDER BY trade_date DESC LIMIT 1"
        )).scalar()
    ends = [d for d in source_end_dates(db).values() if d]
    return min(ends) if ends else None


def resolve_subject_types(db, subjects: list[str]) -> list[tuple[str, str]]:
    """把代码清单分成 (index|stock, symbol)，用上游估值表实际存在的指数代码判定。"""
    codes = set(db.execute(text(
        "SELECT DISTINCT index_code FROM bds_index_valuation")).scalars().all())
    return [("index" if s in codes else "stock", s) for s in subjects]


# ---------------------------------------------------------------------------
# 默认标的范围（方案「估值分位默认范围动态化」§3）
# ---------------------------------------------------------------------------

def monitor_subjects(db) -> list[str]:
    """运行时读标的监测表的全部代码（**不 import irs**，🔒4.12）。

    bds 只能被依赖；这里用表名字符串 + 原生 SQL 取业务域事实，不引用 irs 任何
    模块/模型，与「models.py 之间用表名字符串声明 FK」「跨模块取数放 service/router」
    是同一条边界。表不存在 / 查询失败由 subject_diagnostics() 回退兜底常量。
    """
    return [str(s) for s in db.execute(text(
        f"SELECT symbol FROM {MONITOR_TABLE} ORDER BY symbol")).scalars().all()]


def subjects_with_data(db, symbols: list[str]) -> set[str]:
    """判「有没有可算分位的数据」，只对监测表这几个代码做 EXISTS 式点查。

    ⚠️ 不对 1,991 万行的 bds_daily_valuation 做 DISTINCT 全扫；判据用
    `pe_ttm IS NOT NULL`，与 clean_value / load_source 的有效值口径同源 ⇒
    「存在行但四个指标全 NULL」的标的同样不落空壳分位行。
    SPX（海外指数，按规则应用 CAPE）/ GC00Y（商品，无 PE）都会被过滤。
    """
    out: set[str] = set()
    for s in symbols:
        for table, key in (("bds_index_valuation", "index_code"),
                           ("bds_daily_valuation", "symbol")):
            if db.execute(text(
                f"SELECT 1 FROM {table} WHERE {key}=:c "
                f"AND pe_ttm IS NOT NULL LIMIT 1"), {"c": s}).first():
                out.add(s)
                break
    return out


def subject_diagnostics(db) -> dict:
    """本次重算的默认标的清单 + 与监测表的偏差诊断（口径唯一来源）。

    三项用户裁定（方案 §7）：
    1. 无估值数据的标的自动过滤，写进 excluded_no_data 审计；
    2. 监测表移除的标的默认**保留历史行**，只在 orphan_in_store 提示；
    3. 每次任务**实时读表**，不缓存、不自动触发 ⇒ 监测表改完要跑一次重算。
    """
    fallback = False
    try:
        mon = monitor_subjects(db)
        if not mon:
            fallback = True
    except Exception:
        mon, fallback = [], True
    if fallback:
        mon = list(L1_SUBJECTS)

    has = subjects_with_data(db, mon)
    default = [s for s in mon if s in has]
    excluded = [s for s in mon if s not in has]
    existing = {str(r[0]) for r in db.execute(text(
        f"SELECT DISTINCT symbol FROM {TABLE}")).all()}
    # 「已移除」严格按监测表判定：手动单标的落盘、无数据被过滤的都不算孤儿
    orphan = sorted(existing - set(mon))
    return {"default": default, "monitor": mon,
            "excluded_no_data": excluded, "orphan_in_store": orphan,
            "fallback": fallback}


# ---------------------------------------------------------------------------
# 标的范围解析（2026-11-24：SymbolInfo 驱动五档，取代「监测表默认清单」）
# ---------------------------------------------------------------------------

# 范围档取值（手写清单不占 scope：走 subjects 显式参数，优先级高于 scope）
PCT_SCOPES = ("all", "index", "stock", "industry")
TRADING_STATUSES = ("trading", "delisted", "all")

# 逐标的审计明细（subjects / input_fp / source_as_of_by_subject）的落 JSON 上限：
# 全市场 ~5,926 标的时逐项明细会把 task.result 撑到数百 KB，超限只存计数。
AUDIT_CAP = 200


def _scope_conditions(scope: str, industry: Optional[str],
                      trading_status: str) -> tuple[str, str, dict]:
    """范围档 → (指数 WHERE, 个股 WHERE, 绑定参数)。

    清单解析（resolve_scope_subjects）与行数统计（count_scope_source_rows）共用
    同一条件构造，保证两处口径永不漂移。

    - 指数恒为 industry='A股指数'（退市口径不作用指数）
    - 个股恒为 market IS NOT NULL（指数 / 美股 / 黄金的 market 恒 NULL，天然排除）
      + 退市口径三态（与 /bds/symbol-infos 的 trading_status 同枚举同语义）
    - scope=industry 再叠加个股 industry 精确匹配
    """
    if trading_status not in TRADING_STATUSES:
        raise ValueError(f"非法 trading_status: {trading_status}（应为 trading/delisted/all）")
    params: dict = {}
    stock_where = "market IS NOT NULL"
    if trading_status == "trading":
        stock_where += " AND (delisted_date IS NULL OR delisted_date > :today)"
        params["today"] = date.today()
    elif trading_status == "delisted":
        stock_where += " AND delisted_date IS NOT NULL AND delisted_date <= :today"
        params["today"] = date.today()
    if scope == "industry":
        if not industry:
            raise ValueError("scope=industry 需填 industry 参数")
        stock_where += " AND industry = :industry"
        params["industry"] = industry
    return "industry = :idx_ind", stock_where, {**params, "idx_ind": INDEX_INDUSTRY}


def resolve_scope_subjects(db, scope: str, industry: Optional[str] = None,
                           trading_status: str = "trading") -> tuple[list[str], dict]:
    """SymbolInfo 驱动的标的清单解析（2026-11-24 方案，取代监测表默认清单）。

    scope: all=A股指数∪A股个股（默认）/ index=仅A股指数 / stock=仅A股个股 /
           industry=指定行业个股；手写清单不走这里（调用方直接传 subjects）。
    - 指数清单为空时回退 Config.INDEX_CODE 键集（与历史行情段同一兜底口径）
    - 无估值数据的标的**不做预过滤**：逐标的 EXISTS 点查在全市场 ~5,926 标的
      量级下不可行，no_data 跳过路径（load_source 空 → 0 行）已存在，直接复用
    - 清单先指数后个股、各按 symbol 升序 ⇒ 顺序稳定、进度可复现
    返回 (清单, meta)；meta.scope / meta.subject_source 供任务审计。
    """
    if scope not in PCT_SCOPES:
        raise ValueError(f"非法 scope: {scope}（应为 {'/'.join(PCT_SCOPES)}）")
    iw, sw, p = _scope_conditions(scope, industry, trading_status)
    index_syms = [str(s) for s in db.execute(
        text(f"SELECT symbol FROM bds_symbol_info WHERE {iw} ORDER BY symbol"),
        p).scalars().all()]
    if not index_syms:
        index_syms = list(dbsCfg.INDEX_CODE)
        logger.warning("证券信息表无 industry='%s' 标的，分位范围回退 Config.INDEX_CODE",
                       INDEX_INDUSTRY)
    if scope == "index":
        subs = index_syms
    else:
        stock_syms = [str(s) for s in db.execute(
            text(f"SELECT symbol FROM bds_symbol_info WHERE {sw} ORDER BY symbol"),
            p).scalars().all()]
        subs = list(dict.fromkeys(index_syms + stock_syms)) if scope == "all" else stock_syms
    return subs, {"scope": scope, "subject_source": f"symbol_info:{scope}"}


def count_scope_source_rows(db, scope: str, industry: Optional[str],
                            trading_status: str) -> int:
    """范围档的源表总行数（plan 的 total_count 用）。

    两条聚合半连接 COUNT 与 resolve_scope_subjects 共用 _scope_conditions，
    全市场量级下代替逐标的 COUNT 循环（5,926 次点查在 HTTP 请求线程不可接受）。
    """
    iw, sw, p = _scope_conditions(scope, industry, trading_status)
    total = 0
    if scope in ("all", "index"):
        total += int(db.execute(text(
            f"SELECT COUNT(*) FROM bds_index_valuation WHERE index_code IN "
            f"(SELECT symbol FROM bds_symbol_info WHERE {iw})"), p).scalar() or 0)
    if scope in ("all", "stock", "industry"):
        total += int(db.execute(text(
            f"SELECT COUNT(*) FROM bds_daily_valuation WHERE symbol IN "
            f"(SELECT symbol FROM bds_symbol_info WHERE {sw})"), p).scalar() or 0)
    return total


def _orphan_symbols(db, whitelist: list[str]) -> list[str]:
    """落表已有但不在白名单内的标的（默认保留历史行，仅审计提示）。

    白名单恒取「范围档默认清单」而非本次运行清单：显式子集 / 单标的重算时，
    其余标的历史行仍在白名单内，不算孤儿（与 P0-1「传 subjects 永不删」同语义）。
    结果按 AUDIT_CAP 截断，防白名单意外清空时 result 被撑爆。
    """
    existing = {str(r[0]) for r in db.execute(
        text(f"SELECT DISTINCT symbol FROM {TABLE}")).all()}
    return sorted(existing - set(whitelist))[:AUDIT_CAP]


def load_source(db, subject_type: str, symbol: str) -> list[tuple]:
    """直接查源表**全序列**（4 指标一次拉齐），禁止走 get_series。

    指数 `bds_index_valuation` 是整体法聚合结果，个股 `bds_daily_valuation` 是原始估值；
    两者列名一致，故只有表名 / 键名不同。
    """
    table = SOURCE_TABLES[subject_type]
    key = "index_code" if subject_type == "index" else "symbol"
    return db.execute(text(
        f"SELECT trade_date, pe_ttm, pb_mrq, ps_ttm, dy_ttm FROM {table} "
        f"WHERE {key} = :c ORDER BY trade_date"
    ), {"c": symbol}).all()


def fingerprint(raw: list[tuple]) -> str:
    """Python 侧源序列指纹（🔒6.9：集合指纹禁 MD5(GROUP_CONCAT())）。

    逐行拼 `trade_date|4 值` 后 sha256，值用原样 str（Decimal 字符串化稳定）。
    用于判「上游历史有没有被修订」，比 `MAX(update_time)` 可靠得多。
    """
    h = hashlib.sha256()
    for r in raw:
        d = r[0]
        parts = [d.isoformat() if hasattr(d, "isoformat") else str(d)]
        parts += ["" if v is None else str(v) for v in r[1:5]]
        h.update("|".join(parts).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _valid(subject_type: str, metric: str, v) -> bool:
    """指数侧非空即有效；个股侧按 clean_value。两套规则故意不统一（§2.3）。"""
    if v is None:
        return False
    return True if subject_type == "index" else clean_value(metric, float(v))


# ---------------------------------------------------------------------------
# 组装行
# ---------------------------------------------------------------------------

@lru_cache(maxsize=30000)
def _cuts_map(as_of: date) -> dict[str, date]:
    """窗口左界（date 对象）缓存，供 `_cuts_json` 序列化 window_cut 列。"""
    return {k: date.fromisoformat(v) for k, v in window_cuts(as_of).items()}


@lru_cache(maxsize=30000)
def _cuts_json(as_of: date) -> str:
    """窗口左界 JSON 缓存：同一交易日跨 6 个 subject 重复出现，序列化一次即可。"""
    return json.dumps({k: d.isoformat() for k, d in _cuts_map(as_of).items()},
                      ensure_ascii=False, separators=(",", ":"))


def build_rows(db, subject_type: str, symbol: str, raw: list[tuple],
               source_as_of: Optional[date], calc_at: datetime,
               fp: str, only_dates: Optional[set] = None) -> list[dict]:
    """一个 subject 的全历史宽表行（一 as_of 一行）。

    2026-11-23 瘦身后只产出：12 分位列 + status 四态 + window_cut + 工程三列
    （外加 current_* 4 键仅存于返回 dict，供 um 截面表 `_upsert` 消费——bds 历史
    表列已删、白名单不含，落库不带）。
    当日原始值 / 分布 stat / 窗口标记 / 样本量审计列已删除（前端零消费）。

    口径 A（2026-11-23 用户定稿）：**窗口有效样本 n < 250（约一年交易日）⇒
    该窗口分位直接不保存（NULL）**，不再有 short/undercovered 标记列。
    status 仍只描述当日值有效性（ok/loss/no_data），与窗口样本量解耦——
    status=ok 但某窗口 pct=NULL 是合法组合（新股早期窗口），页面显示 `—`。
    """
    if not raw:
        return []

    daily: dict[str, dict] = {}
    for j, m in enumerate(PCT_METRICS, start=1):
        seq = [(r[0], float(r[j])) for r in raw if _valid(subject_type, m, r[j])]
        # 增量模式只让内核对目标日构造 stat dict（滑动状态仍逐日维护）
        daily[m] = daily_percentiles(seq, only_dates=only_dates) if seq else {}

    # 每个 metric 是否全序列都无有效值（no_data），区别于某日无效（loss）
    no_data = {m: not any(_valid(subject_type, m, r[j]) for r in raw)
               for j, m in enumerate(PCT_METRICS, start=1)}

    rows = []
    iter_raw = raw if only_dates is None else [r for r in raw if r[0] in only_dates]
    for r in iter_raw:
        as_of = r[0]
        row: dict = {
            "subject_type": subject_type, "symbol": symbol, "as_of": as_of,
            "source_as_of": source_as_of, "input_fp": fp, "calc_at": calc_at,
            "window_cut": _cuts_json(as_of),
        }
        for j, m in enumerate(PCT_METRICS, start=1):
            pre = PCT_PREFIX[m]
            # current_* 只进行字典（um 截面表 upsert 消费）；bds 历史表列已删、
            # fields_pct_update 白名单不含 ⇒ 落库不受影响
            row[f"current_{pre}"] = None if r[j] is None else float(r[j])
            if no_data[m]:
                st = "no_data"
                stats = None
            elif as_of not in daily[m]:
                # 当天无有效值 ⇒ 该 metric 分位全 NULL，其余指标照常写
                st = "loss"
                stats = None
            else:
                st = "ok"
                stats = {k: daily[m][as_of][k] for k in WINDOW_KEYS}

            row[f"status_{pre}"] = st
            for k in WINDOW_KEYS:
                d = (stats or {}).get(k)
                # 口径 A：样本不足的窗口不保存分位（n 不足 ⇒ NULL，下游显示 —）
                row[f"{pre}_pct_{k.split('_')[1]}"] = (
                    d.get("pct") if d and d.get("n", 0) >= MIN_WINDOW_SAMPLES else None)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 旧值比对 / upsert / 孤儿清理
# ---------------------------------------------------------------------------

def _norm(v) -> Optional[float]:
    """空值归一化（🔒6.6）：None / NaN / '' 一律视作同一个「空」。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, 2)


def audit_diff(db, planned: list[dict], keys: set) -> dict:
    """重算前后逐值比对 12 个分位列，写进 task.result.audit。"""
    empty = {"changed": 0, "max_pct": 0.0, "appeared": 0, "vanished": 0, "samples": []}
    if not planned:
        return empty
    if not keys:
        return {**empty, "appeared": len(planned)}
    where = " OR ".join("(subject_type=:t{} AND symbol=:s{})".format(i, i)
                         for i in range(len(keys)))
    params: dict = {}
    for i, (t, s) in enumerate(sorted(keys)):
        params[f"t{i}"] = t
        params[f"s{i}"] = s
    cols = ", ".join(PCT_COLUMNS)
    old_rows = db.execute(text(
        f"SELECT subject_type, symbol, as_of, {cols} FROM {TABLE} WHERE {where}"
    ), params).all()
    old = {(r[0], r[1], r[2]): tuple(_norm(x) for x in r[3:]) for r in old_rows}

    new = {(r["subject_type"], r["symbol"], r["as_of"]):
           tuple(_norm(r.get(c)) for c in PCT_COLUMNS) for r in planned}
    changed, appeared, vanished, max_pct, samples = 0, 0, 0, 0.0, []
    for k, nv in new.items():
        ov = old.get(k)
        if ov is None:
            appeared += 1
            continue
        diffs = [(PCT_COLUMNS[i], ov[i], nv[i])
                 for i in range(len(PCT_COLUMNS)) if ov[i] != nv[i]]
        if diffs:
            changed += 1
            for c, a, b in diffs:
                if a is not None and b is not None:
                    max_pct = max(max_pct, abs(b - a))
            if len(samples) < 5:
                samples.append({"key": [str(k[0]), k[1], k[2].isoformat()],
                                "diffs": diffs[:3]})
    vanished = len(set(old) - set(new))
    return {"changed": changed, "max_pct": round(max_pct, 2),
            "appeared": appeared, "vanished": vanished, "samples": samples}


def upsert_rows(planned: list[dict], chunk: int = 2000) -> int:
    """幂等 upsert（ON DUPLICATE KEY UPDATE 只覆盖白名单列），返回写行数。"""
    if not planned:
        return 0
    df = pd.DataFrame(planned)
    cols = UNIQUE_KEYS + [c for c in fields_pct_update if c in df.columns]
    # 空值归一化交给 upsert_df_to_db 内部的 df.replace({np.nan: None})；
    # 这里不再自行 where/astype，避免把 datetime / date 列变成 object 后类型漂移
    df = df[cols]
    total = 0
    for i in range(0, len(df), chunk):
        total += upsert_df_to_db(
            df.iloc[i:i + chunk], TABLE, settings.DB_ENGINE,
            unique_keys=UNIQUE_KEYS, update_columns=fields_pct_update)
    return total


def delete_orphans(db, keep_subjects: set[tuple[str, str]]) -> int:
    """清理「不在本次 subject 集合」的孤儿行（§10 哨兵 5）。

    ⚠️ 按 subject 维度删，不按 as_of 维度：M3 区间重算只写 [D, 末日]，
    若按 as_of 差集删会把 D 之前的历史行全误删。
    """
    existing = set(db.execute(text(
        f"SELECT DISTINCT subject_type, symbol FROM {TABLE}")).all())
    orphans = existing - keep_subjects
    for t, s in orphans:
        db.execute(text(f"DELETE FROM {TABLE} WHERE subject_type=:t AND symbol=:s"),
                   {"t": t, "s": s})
    db.commit()
    return len(orphans)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _count_rows_by_subject(db, pairs: list[tuple[str, str]]) -> int:
    """显式小清单的行数统计：逐标的 COUNT（千级以内点查可接受）。"""
    n = 0
    for t, s in pairs:
        table = SOURCE_TABLES[t]
        key = "index_code" if t == "index" else "symbol"
        n += int(db.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {key}=:c"), {"c": s}).scalar() or 0)
    return n


def plan(subjects: Optional[list[str]] = None, mode: str = "M2",
         from_date: Optional[date] = None, scope: Optional[str] = None,
         industry: Optional[str] = None, trading_status: str = "trading") -> dict:
    """任务计划：subject 清单、模式、预计行数（供 total_count 与前端展示）。

    2026-11-24 起：subjects=None 时按 SymbolInfo 范围档解析（scope 默认 all =
    A股指数 ∪ A股个股），取代原「监测表默认清单」；显式传 subjects（单标的 /
    子集）以用户清单为准。范围档的行数统计走聚合半连接 COUNT（与清单解析
    同条件），显式小清单保留逐标的 COUNT 循环。
    """
    if mode not in ("M1", "M2", "M3", "auto"):
        raise ValueError("mode 必须是 M1 / M2 / M3 / auto")
    if mode == "M3" and from_date is None:
        raise ValueError("M3 必须给 from_date（修订起始日）")
    with SessionLocal() as db:
        if subjects is not None:
            subs = list(subjects)
            scope_meta = {"scope": "explicit", "subject_source": "explicit"}
            explicit = True
        else:
            subs, scope_meta = resolve_scope_subjects(
                db, scope or "all", industry, trading_status)
            explicit = False
        pairs = resolve_subject_types(db, subs)
        end = source_end_date(db)
        if explicit:
            n_days = _count_rows_by_subject(db, pairs)
        else:
            n_days = count_scope_source_rows(
                db, scope_meta["scope"], industry, trading_status)
        index_count = sum(1 for t, _ in pairs if t == "index")
    return {"subjects": subs, "subject_pairs": pairs, "mode": mode,
            "from_date": from_date, "source_as_of": end, "total_count": n_days,
            "scope": scope_meta["scope"], "subject_source": scope_meta["subject_source"],
            "subject_count": len(subs), "index_count": index_count,
            "stock_count": len(pairs) - index_count,
            "subjects_explicit": explicit}


def rebuild(mode: str = "M2", subjects: Optional[list[str]] = None,
            from_date: Optional[date] = None,
            prune_orphans: bool = False,
            on_progress: Optional[Callable[[int, int], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None,
            scope: Optional[str] = None, industry: Optional[str] = None,
            trading_status: str = "trading") -> dict:
    """执行重算，返回结构化审计（写进 bds_sync_task.result）。

    `on_progress(done, total)` 由后台线程传入，用于刷任务进度。
    `should_stop()` 由后台线程传入停止信号；逐 subject 检查，命中即中止循环，
    已算完的 subject 仍会落库（停止是「不再继续」而不是「丢弃已完成」）。
    `prune_orphans=True` 且 `subjects is None` 且 mode ∈ (M2, auto) 时才清理孤儿标的；
    默认关闭，单标的/子集重算永不删别人的历史行（P0-1）。

    2026-11-24 起：subjects=None 时按 SymbolInfo 范围档**运行时**解析
    （scope 默认 all = A股指数 ∪ A股个股），取代监测表默认清单；
    🔴 与 P0-1 同构：`subjects is not None` 才是「用户显式指定」判据，
    范围档解析出的全市场清单仍走默认清单语义（允许 prune_orphans）。
    """
    t0 = time.perf_counter()
    if mode not in ("M1", "M2", "M3", "auto"):
        raise ValueError("mode 必须是 M1 / M2 / M3 / auto")
    if mode == "M3" and from_date is None:
        raise ValueError("M3 必须给 from_date（修订起始日）")

    calc_at = datetime.now()
    planned: list[dict] = []
    fps: dict[str, str] = {}
    as_of_range = [None, None]

    with SessionLocal() as db:
        # 范围清单每次任务实时按 SymbolInfo 解析（不缓存、不自动触发）。
        # 🔴 `subjects is not None` 才是「用户显式指定」的判据，绝不能拿展开后的
        # subs 判断——否则 prune_orphans 会被静默忽略（P0 端到端抓到过）。
        if subjects is not None:
            subs = list(subjects)
            scope_meta = {"scope": "explicit", "subject_source": "explicit"}
            # 孤儿审计恒按「范围档默认白名单」口径：显式子集重算时其余标的
            # 仍在白名单内，不算孤儿；范围档解析失败时退回本次清单（保守）。
            try:
                whitelist, _ = resolve_scope_subjects(
                    db, scope or "all", industry, trading_status)
            except ValueError:
                whitelist = subs
        else:
            subs, scope_meta = resolve_scope_subjects(
                db, scope or "all", industry, trading_status)
            whitelist = subs
        orphan = _orphan_symbols(db, whitelist)
        pairs = resolve_subject_types(db, subs)
        # P1-6：两表末日独立取；全局字段保留「最早末日」，仅供展示与近 15 天水位
        # 下界（保守，指数表落后时不会被整体漏掉）。逐 subject 判据一律用 end_t。
        ends = source_end_dates(db)
        source_as_of = min((d for d in ends.values() if d), default=None)
        # 表内每 subject 的最新行（M1 起点 / M2 快速跳过 / auto 的 fp 判据）。
        # window_cut 一并读出：口径变更后旧行的左界仍是 365 天口径，必须强制重建。
        # calc_at 用于 SCHEMA_EPOCH 结构变更闸（新增列后历史行必须回填）。
        # 🔒七·4：只取近 15 天（走 k_bvp_asof 索引），别为拿 6 行扫 26,816 行 JSON 列；
        # 某 subject 落后超过 15 天 ⇒ prev=None ⇒ 退化为全量重建，是安全降级而非误判。
        cut_recent = (source_as_of - timedelta(days=15)) if source_as_of else date(1970, 1, 1)
        latest_rows = db.execute(text(
            f"SELECT subject_type, symbol, as_of, input_fp, window_cut, calc_at FROM {TABLE} "
            f"WHERE as_of >= :cut"
        ), {"cut": cut_recent}).all()
        latest: dict[tuple[str, str], tuple] = {}
        for r in latest_rows:
            k = (r[0], r[1])
            if k not in latest or r[2] > latest[k][0]:
                latest[k] = (r[2], r[3], r[4], r[5])

        skipped_subjects: list[str] = []
        cut_stale_subjects: list[str] = []
        fp_changed_subjects: list[str] = []
        schema_stale_subjects: list[str] = []
        written_keys: set[tuple[str, str]] = set()
        source_by_subject: dict[str, Optional[date]] = {}

        for i, (t, s) in enumerate(pairs):
            # 停止信号：逐 subject 检查（单个 subject 的全量重建是原子步骤，不做中途打断）
            if should_stop is not None and should_stop():
                logger.warning("估值分位重算收到停止信号，已处理 %s/%s 个标的", i, len(pairs))
                break
            # P1-6：该 subject 所属源表的末日，不拿全局（个股表）末日判指数
            end_t = ends.get(t)
            source_by_subject[s] = end_t
            raw = load_source(db, t, s)
            fp = fingerprint(raw)
            fps[s] = fp
            prev = latest.get((t, s))
            # 三道强制全量闸：
            # ① 口径陈旧（P0，§7.2）：表内最新行 window_cut 不是日历对日；
            # ② input_fp 变化（P0-2/3）：上游历史或同日被修订，影响 [D, D+10y] 上千行；
            # ③ 结构陈旧（P1-4）：calc_at 早于 SCHEMA_EPOCH ⇒ 新增列尚未回填。
            # 任一命中都 ⇒ 该 subject 全量重建，不做脆弱的「找最早变更日」。
            stale = bool(prev) and not _cut_is_calendar(prev)
            fp_changed = bool(prev) and prev[1] != fp
            schema_stale = bool(prev) and (
                len(prev) < 4 or prev[3] is None or prev[3] < SCHEMA_EPOCH)
            if stale:
                cut_stale_subjects.append(s)
            if fp_changed:
                fp_changed_subjects.append(s)
            if schema_stale:
                schema_stale_subjects.append(s)

            # ---- M2 快速跳过（恒定性推论，§0.1）----
            # 源序列指纹、**该 subject 的**源末日、窗口口径、表结构版本四者都没变时，
            # 「全量重写」与「不写」逐值全等。实测第二次全量覆盖写 26,816 行要 57 s
            # （ON DUPLICATE KEY UPDATE 全字段），跳过可把日常重算降到只拉源序列的 1.6 s。
            if mode == "M2" and prev and prev[1] == fp and end_t \
                    and prev[0] >= end_t and not (stale or fp_changed or schema_stale):
                skipped_subjects.append(s)
                if on_progress:
                    on_progress(i + 1, len(pairs))
                continue

            # ---- 先定「写哪些 as_of」，再让 build_rows 只算这些行 ----
            # 计算内核仍逐日维护滑动窗口状态（口径不变），只是不为非目标日
            # 构造 6 字段 stat dict / 做 round / 序列化 JSON ⇒ 增量模式快一个量级。
            eff_mode, low = mode, None
            if stale or fp_changed or schema_stale:
                # 全局性事件，局部重写没有意义；显式 M3 的 from_date 也被覆盖（安全优先）
                eff_mode, low = "M2", None
            elif mode == "auto":
                if prev and prev[1] == fp and end_t and prev[0] >= end_t:
                    eff_mode = "skip"       # 输入未变且该源表末日未变：无需写
                else:
                    eff_mode, low = "M1", (prev[0] if prev else None)
            elif mode == "M1":
                low = prev[0] if prev else None
            elif mode == "M3":
                low = from_date

            if eff_mode == "skip":
                skipped_subjects.append(s)
                if on_progress:
                    on_progress(i + 1, len(pairs))
                continue

            only_dates = None
            if low is not None:
                only_dates = ({r[0] for r in raw if r[0] > low} if eff_mode == "M1"
                              else {r[0] for r in raw if r[0] >= low})
                if not only_dates:
                    if on_progress:
                        on_progress(i + 1, len(pairs))
                    continue
            rows = build_rows(db, t, s, raw, end_t, calc_at, fp,
                              only_dates=only_dates)
            planned.extend(rows)
            written_keys.add((t, s))
            if rows:
                a, b = rows[0]["as_of"], rows[-1]["as_of"]
                as_of_range[0] = min(as_of_range[0], a) if as_of_range[0] else a
                as_of_range[1] = max(as_of_range[1], b) if as_of_range[1] else b
            if on_progress:
                on_progress(i + 1, len(pairs))

        # audit 只比「本次实际写了的 subject」，否则被跳过的行会被误算成 vanished
        audit = audit_diff(db, planned, written_keys)
        rows_rowcount = upsert_rows(planned)
        # MySQL 的 rowcount 在「值未变的 ON DUPLICATE KEY UPDATE」时返回 0，
        # 它不等于写行数 ⇒ 审计里两个量分开记，rows_upserted 才是实际 UPSERT 行数
        rows_upserted = len(planned)
        # 🔴 P0-1 + 用户裁定 2：孤儿清理必须显式开 + 默认清单（非显式 subjects）才执行；
        # 监测表移除标的后历史行默认保留，只在 result.orphan_in_store 提示。
        # 传了 subjects（单标的/子集）说明只是局部重算，删别人的历史行是数据破坏；
        # M1/M3 也恒不删。keep_subjects 仍按本次全部 pairs（不能把跳过的当孤儿）。
        all_keys = {(t, s) for t, s in pairs}
        allow_prune = bool(prune_orphans and subjects is None and mode in ("M2", "auto"))
        orphans = delete_orphans(db, all_keys) if allow_prune else 0

    duration_ms = int((time.perf_counter() - t0) * 1000)
    index_count = sum(1 for t, _ in pairs if t == "index")
    return {
        "mode": mode, "window_mode": "calendar_anniversary",
        "metrics": list(PCT_METRICS), "windows": list(WINDOW_KEYS),
        "source_as_of": source_as_of.isoformat() if source_as_of else None,
        # P1-6：各 subject 所属源表的末日（指数 / 个股可能不同）
        # AUDIT_CAP 截断：全市场量级下逐标的明细不落 JSON，只存计数
        "source_as_of_by_subject": (
            {k: v.isoformat() if v else None for k, v in source_by_subject.items()}
            if len(source_by_subject) <= AUDIT_CAP else None),
        "source_as_of_by_subject_count": len(source_by_subject),
        # 范围档审计：清单来源、规模、孤儿（清单超限截断为 null，计数恒有）
        "scope": scope_meta["scope"], "subject_source": scope_meta["subject_source"],
        "subjects": subs if len(subs) <= AUDIT_CAP else None,
        "subject_count": len(subs),
        "index_count": index_count, "stock_count": len(pairs) - index_count,
        "orphan_in_store": orphan,
        "as_of_range": [d.isoformat() if d else None for d in as_of_range],
        "rows_total": len(planned), "rows_upserted": rows_upserted,
        "rows_rowcount": rows_rowcount, "orphans_deleted": orphans,
        "skipped_subjects": skipped_subjects, "skipped_count": len(skipped_subjects),
        "cut_stale_subjects": cut_stale_subjects,
        "fp_changed_subjects": fp_changed_subjects,
        "schema_stale_subjects": schema_stale_subjects,
        "schema_epoch": SCHEMA_EPOCH.isoformat(),
        "forced_full_count": len(set(cut_stale_subjects) | set(fp_changed_subjects)
                                 | set(schema_stale_subjects)),
        "prune_orphans": allow_prune,
        "subjects_explicit": subjects is not None,
        "audit": audit,
        "input_fp": ({k: v[:16] for k, v in fps.items()}
                     if len(fps) <= AUDIT_CAP else None),
        "input_fp_count": len(fps),
        "duration_ms": duration_ms,
    }


# 🔴 `_earliest_change_date()` 已删除（P0-2）：原实现只 `return prev_as_of`，
# 让 auto 在历史修订时仅重写最新一行，却把该行 fp 刷成新值 ⇒ 快跳过把错误固化。
# 现在统一走「fp 变化 ⇒ 该 subject 全量重建」，不再需要「找最早变更日」。


def _cut_is_calendar(prev: tuple) -> bool:
    """表内最新行的 10y 左界是否已是**日历对日**口径。

    M2 快速跳过的三道闸之一：口径从 365 天切到对日时，源 fp 与源末日都没变，
    只有 window_cut 能暴露「表还是旧口径」⇒ 必须强制全量重建（§7.2）。
    """
    if len(prev) < 3 or not prev[2]:
        return False
    try:
        cut = json.loads(prev[2]) if isinstance(prev[2], str) else (prev[2] or {})
    except (TypeError, ValueError):
        return False
    return cut.get("pct_10y") == shift_years(prev[0], 10).isoformat()


# ---------------------------------------------------------------------------
# 读取侧（§6.1）
# ---------------------------------------------------------------------------

READ_COLUMNS = ["subject_type", "symbol", "as_of", "source_as_of",
                "status_pe", "status_pb", "status_ps", "status_dy",
                "window_cut"] + PCT_COLUMNS


def read_latest_rows(recent_days: int = 15) -> dict[tuple[str, str], dict]:
    """取每个 subject 的最新一行。

    不用 `MAX(as_of)` 全表聚合（🔒七·4），改用 `WHERE as_of >= 两表最早源末日-15 天`
    走 `k_bvp_asof` 索引，Python 侧分组取最大 ⇒ 天然容忍各 subject 末日不同，
    也容忍指数估值表比个股表落后几天（P1-6）。
    """
    with SessionLocal() as db:
        end = source_end_date(db)
        if end is None:
            return {}
        cut = end - timedelta(days=recent_days)
        cols = ", ".join(READ_COLUMNS)
        rows = db.execute(text(
            f"SELECT {cols} FROM {TABLE} WHERE as_of >= :cut"
        ), {"cut": cut}).mappings().all()
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        d = dict(r)
        k = (d["subject_type"], d["symbol"])
        if k not in out or d["as_of"] > out[k]["as_of"]:
            out[k] = d
    return out


def read_percentiles() -> dict[tuple[str, str], Optional[float]]:
    """`um_today.percentiles()` 契约：{(symbol, metric): pct_10y}，只取 pe/pb。

    仍只给 pe_ttm / pb_mrq（新增的 PS / 股息率本期只落库备用，不进请求链路契约）；
    status != ok 不给分位；表空 / 过期 / 单 subject 缺失 ⇒ 该键不出现 ⇒ 页面显示 `—`。
    不回落实时计算（§6.3）。
    """
    out = {}
    for (_, symbol), row in read_latest_rows().items():
        for metric, pre in (("pe_ttm", "pe"), ("pb_mrq", "pb")):
            if row.get(f"status_{pre}") != "ok":
                continue
            out[(symbol, metric)] = _norm(row.get(f"{pre}_pct_10y"))
    return out
