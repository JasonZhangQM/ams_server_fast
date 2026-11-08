# -*- coding: utf-8 -*-
"""标的监测**最新**估值分位表（`irs_um_percentile`）服务。

2026-11-05 新建。与 bds 侧按日历史表 `bds_valuation_percentile` 分工：

| | 历史表（bds） | 本表（irs） |
|---|---|---|
| 粒度 | 一标的一观察日（26,816 行） | 一标的一行（≤8 行） |
| 用途 | 存档 / 审计 / 将来查询页 | `/irs/today`、`/irs/underlying-monitors`、详情、证伪、档位纠偏 |
| 范围 | 监测表，孤儿默认保留 | 监测表，**纯镜像**（范围外的行删除） |

**口径零漂移的实现方式**：不另写任何分位公式，直接调
`bds.services.valuation.percentile_store.build_rows(..., only_dates={anchor})`。
该函数内部仍逐日推进滑动窗口（保证与历史行同口径），但只对锚点日构造 stat dict，
因此单标的 0.3~0.8 s；同一 `as_of` 行与历史表逐值相等，验收可直接两表 `==`。

依赖方向（🔒4.12）：irs → bds 允许；本模块只读 bds 的估值服务与源表，不反向。
刷新范围固定 = `irs_underlying_monitor` 全部标的（本域 ORM 直读），不做范围选择。

⚠️ 读取侧**不回落实时计算**：表空 / 单标的缺失 ⇒ 该键不出现 ⇒ 页面显示 `—`。
回退等于慢问题复发 + 双套口径（历史表方案 §6.3 同款裁决）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.app.bds.services.valuation import percentile_store
from server_fast.app.irs.models import UmPercentile
from server_fast.common.db import SessionLocal

logger = logging.getLogger("uvicorn.error")

TABLE = "irs_um_percentile"

# 4 个指标在宽表里的列前缀（与 bds 历史表同源，改这里必须同步 percentile_stat）
PCT_PREFIX = {"pe_ttm": "pe", "pb_mrq": "pb", "ps_ttm": "ps", "dy_ttm": "dy"}
JSON_FIELDS = {"pe_stat", "pb_stat", "ps_stat", "dy_stat",
               "windows_short", "windows_undercovered", "window_cut"}

# 🔒4.9 显式白名单：只覆盖派生列，绝不含 id / create_time / update_time / note，
# 也不含唯一键 subject_type / symbol（建行时赋值，之后不动）。
UPDATE_FIELDS: list[str] = [
    "as_of", "source_as_of", "first_date", "raw_n",
    "current_pe", "current_pb", "current_ps", "current_dy",
    "pe_pct_3y", "pe_pct_5y", "pe_pct_10y",
    "pb_pct_3y", "pb_pct_5y", "pb_pct_10y",
    "ps_pct_3y", "ps_pct_5y", "ps_pct_10y",
    "dy_pct_3y", "dy_pct_5y", "dy_pct_10y",
    "pe_stat", "pb_stat", "ps_stat", "dy_stat",
    "status_pe", "status_pb", "status_ps", "status_dy", "status_note",
    "windows_short", "windows_undercovered", "window_cut",
    "cleaned_n_pe", "cleaned_n_pb", "cleaned_n_ps", "cleaned_n_dy",
    "input_fp", "calc_at",
]


def _jsonable(field: str, v):
    """`build_rows` 的 JSON 列给的是 `json.dumps` 字符串，赋值给 JSON 列前须还原成对象。

    直接写字符串会被 SQLAlchemy 的 JSON 序列化器再包一层 ⇒ 库里存成 `"{\\"a\\":1}"`
    这样的字符串（而不是 JSON 对象），读取侧只能靠 `_as_dict` 兜底。
    """
    if field not in JSON_FIELDS or v is None:
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


# ---------------------------------------------------------------------------
# 范围：固定 = 标的监测表
# ---------------------------------------------------------------------------

def monitor_symbols(db: Session) -> list[str]:
    """刷新范围唯一来源：`irs_underlying_monitor` 全部标的（本域，直接用 ORM/SQL）。"""
    return [str(s) for s in db.execute(text(
        "SELECT symbol FROM irs_underlying_monitor ORDER BY symbol")).scalars().all()]


# ---------------------------------------------------------------------------
# 刷新
# ---------------------------------------------------------------------------

def refresh(on_progress: Optional[Callable[[int, int], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """全量重算最新分位 + 幂等 upsert + 删除范围外的行。

    - **全量**：分位是「截至锚点的全历史」函数，上游任一历史日修订都会改变今天的分位，
      增量判定（指纹 / 源末日）在 6~8 行的规模上没有收益，只会引入第二套失效逻辑。
    - **锚点**：各标的上游序列最后一个交易日（`raw[-1][0]`），不是 today；指数与个股
      两条同步链路的末日可能差几天，各取各的（🔒1.24 的邻接问题，别拿全局末日判）。
    - **剔除**：上游两张估值表都没有该标的 ⇒ 计入 `excluded_no_data`，不落空壳行
      （SPX 走 CAPE、GC00Y 无 PE，都属此类）。

    :return: 审计 dict，写进 `bds_sync_task.result`（🔒4.8：不写 errors）
    """
    started = datetime.now()
    calc_at = started

    rows: list[dict] = []
    excluded: list[str] = []
    as_of_by_subject: dict[str, str] = {}
    stale: dict[str, int] = {}

    with SessionLocal() as db:
        symbols = monitor_symbols(db)
        if not symbols:
            raise ValueError("标的监测表为空，无法刷新最新分位表")
        ends = percentile_store.source_end_dates(db)          # {index: date, stock: date}
        pairs = percentile_store.resolve_subject_types(db, symbols)
        total = len(pairs)

        for i, (subject_type, symbol) in enumerate(pairs, start=1):
            if should_stop is not None and should_stop():
                logger.warning("最新分位刷新收到停止信号，已处理 %s/%s", i - 1, total)
                break
            raw = percentile_store.load_source(db, subject_type, symbol)
            if not raw:
                excluded.append(symbol)
            else:
                anchor = raw[-1][0]
                src_end = ends.get(subject_type)
                built = percentile_store.build_rows(
                    db, subject_type, symbol, raw, src_end, calc_at,
                    percentile_store.fingerprint(raw),
                    only_dates={anchor})
                if built:
                    rows.append(built[0])
                    as_of_by_subject[symbol] = anchor.isoformat()
                    if src_end and anchor < src_end:
                        # 源表已到 src_end 但该标的还停在 anchor（停牌 / 上游缺行）
                        stale[symbol] = (src_end - anchor).days
            if on_progress is not None:
                on_progress(i, total)

        upserted, appeared = _upsert(db, rows)
        deleted = _delete_out_of_scope(db, pairs)
        db.commit()

    duration = int((datetime.now() - started).total_seconds() * 1000)
    return {
        "symbols": symbols,
        "excluded_no_data": excluded,
        "rows_upserted": upserted,
        "rows_appeared": appeared,
        "rows_deleted": deleted,
        "as_of_by_subject": as_of_by_subject,
        "source_as_of": {k: (v.isoformat() if v else None) for k, v in ends.items()},
        "stale_by_subject": stale,
        "calc_at": calc_at.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": duration,
    }


def _upsert(db: Session, rows: list[dict]) -> tuple[int, int]:
    """逐行 upsert（≤8 行，ORM 足够；不用 pandas 避免整表重写）。

    白名单 `UPDATE_FIELDS` 显式 setattr，`note` 与时间戳天然不被覆盖（🔒6.6 哨兵）。
    """
    if not rows:
        return 0, 0
    existing = {(r.subject_type, r.symbol): r for r in db.query(UmPercentile).all()}
    appeared = 0
    for row in rows:
        key = (row["subject_type"], row["symbol"])
        obj = existing.get(key)
        if obj is None:
            obj = UmPercentile(subject_type=key[0], symbol=key[1])
            db.add(obj)
            existing[key] = obj
            appeared += 1
        for f in UPDATE_FIELDS:
            setattr(obj, f, _jsonable(f, row.get(f)))
    db.flush()
    return len(rows), appeared


def _delete_out_of_scope(db: Session, pairs: list[tuple[str, str]]) -> int:
    """删除不在 `(subject_type, symbol)` 集合里的行 —— 本表是监测表的**纯镜像**。

    与历史表「孤儿默认保留」的策略相反：这张表的语义是「当前该看哪些标的的分位」，
    留着已移除的标的会让页面继续读到它。删除可逆（重算即回），故不设开关。
    """
    valid = {(t, s) for t, s in pairs}
    deleted = 0
    for r in db.query(UmPercentile).all():
        if (r.subject_type, r.symbol) not in valid:
            db.delete(r)
            deleted += 1
    if deleted:
        db.flush()
    return deleted


# ---------------------------------------------------------------------------
# 读取侧（`um_today.percentiles()` 唯一入口）
# ---------------------------------------------------------------------------

def read_map(db: Optional[Session] = None) -> dict[tuple[str, str], float]:
    """`{(symbol, metric): pct_10y}`，与 `percentile_store.read_percentiles()` 契约一致。

    只给 `pe_ttm` / `pb_mrq`（irs 全链路只用这两个）；`status != ok` 或值为空 ⇒ 该键
    不出现 ⇒ 页面显示 `—`（与切换前「分位失败」表现一致）。
    **不回落实时计算**，不做新鲜度闸门（卡片上展示 `calc_at` / `as_of` 供人工判断）。
    """
    own = db is None
    session = SessionLocal() if own else db
    try:
        out: dict[tuple[str, str], float] = {}
        for r in session.query(UmPercentile).all():
            for metric, pre in (("pe_ttm", "pe"), ("pb_mrq", "pb")):
                if getattr(r, f"status_{pre}") != "ok":
                    continue
                v = getattr(r, f"{pre}_pct_10y")
                if v is None:
                    continue
                out[(r.symbol, metric)] = round(float(v), 2)
        return out
    finally:
        if own:
            session.close()


def stats() -> dict:
    """只读概览，供批量同步卡片展示「表是否新鲜」。"""
    with SessionLocal() as db:
        rows = db.query(UmPercentile).all()
        if not rows:
            return {"rows": 0, "calc_at": None, "oldest_calc_at": None,
                    "subjects": [], "source_as_of": {}}
        calc = [r.calc_at for r in rows if r.calc_at]
        return {
            "rows": len(rows),
            "calc_at": max(calc).strftime("%Y-%m-%d %H:%M:%S") if calc else None,
            "oldest_calc_at": min(calc).strftime("%Y-%m-%d %H:%M:%S") if calc else None,
            "subjects": [{"symbol": r.symbol, "subject_type": r.subject_type,
                          "as_of": r.as_of.isoformat() if r.as_of else None,
                          "pe_pct_10y": None if r.pe_pct_10y is None else float(r.pe_pct_10y),
                          "pb_pct_10y": None if r.pb_pct_10y is None else float(r.pb_pct_10y),
                          "status_pe": r.status_pe, "status_pb": r.status_pb}
                         for r in sorted(rows, key=lambda x: x.symbol)],
            "source_as_of": {r.symbol: (r.source_as_of.isoformat()
                                        if r.source_as_of else None) for r in rows},
        }


def read_row(symbol: str, db: Optional[Session] = None) -> Optional[dict]:
    """读取**单标的**最新分位整行，供标的监测抽屉「估值与走势」Tab 展示。

    2026-11-08 新增。只做字段搬运：不计算、不回落实时公式、不从
    `bds_valuation_percentile` 历史表派生（🔒 分位读取唯一入口是本表）。
    `status != ok` 的行照常返回，由前端标灰并说明原因（不静默抹掉）。
    """
    own = db is None
    session = SessionLocal() if own else db
    try:
        r = session.query(UmPercentile).filter(UmPercentile.symbol == symbol).first()
        if r is None:
            return None

        def f(v):
            return None if v is None else round(float(v), 4)

        current, pct, status = {}, {}, {}
        for metric, pre in PCT_PREFIX.items():
            current[metric] = f(getattr(r, f"current_{pre}"))
            status[metric] = getattr(r, f"status_{pre}")
            pct[metric] = {
                "3y": f(getattr(r, f"{pre}_pct_3y")),
                "5y": f(getattr(r, f"{pre}_pct_5y")),
                "10y": f(getattr(r, f"{pre}_pct_10y")),
            }
        return {
            "symbol": r.symbol,
            "subject_type": r.subject_type,
            "as_of": r.as_of.isoformat() if r.as_of else None,
            "source_as_of": r.source_as_of.isoformat() if r.source_as_of else None,
            "calc_at": r.calc_at.strftime("%Y-%m-%d %H:%M:%S") if r.calc_at else None,
            "first_date": r.first_date.isoformat() if r.first_date else None,
            "raw_n": r.raw_n,
            "status_note": r.status_note,
            "current": current,
            "pct": pct,
            "status": status,
        }
    finally:
        if own:
            session.close()

