# -*- coding: utf-8 -*-
"""指数加权估值日序列与分位（P0-1）。

口径（详见 `docs/指数分位方案_2026-10-20.md` §2，已实测裁定）：

- **PE / PB 用整体法**：`指标 = ΣMV / Σ(MV/指标_i)`，等价于「市值加权的盈利收益率取倒数」。
  ⚠️ 它不是市值加权平均 PE！后者在 000300 上会把 13.07 算成 28.30（偏差 116%），
  因为个别「大市值 + 极高 PE」的股票会把均值拉爆，而整体法天然免疫（高 PE 的 E 贡献小）。
- **亏损股净利润计入**（中证官方口径）：PE ≤ 0 的成分按 `MV/PE`（负值）进入分母。
  剔除亏损股会低估 5.6%（12.38 vs 13.07）。
- **股息率不需要特殊处理**：`Σ(dy_i×MV_i)/ΣMV_i` 恒等于市值加权平均，因为 D_i = dy_i×MV_i。
- **中位数只看横截面**：当日成分 PE（仅 PE>0）的中位数，与「10 年序列中位数」是两回事。
- **成分匹配 point-in-time**：用「snapshot_date ≤ trade_date 的最近快照」，禁止用最新快照
  回填全历史（会引入生存者偏差）。

性能：不逐日查（0.35 s/天 × 5,229 天 ≈ 30 分钟/指数），改为**按成分快照区间批量聚合**，
每个区间一条 SQL。全量回填 5 个指数约 6~11 分钟。
"""
import logging
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import IndexConstituent, IndexConstituentSync
from server_fast.common.db import SessionLocal

logger = logging.getLogger("uvicorn.error")

# 默认参与的宽基指数（不含 SHSE.000001：上证指数成分股已被用户两次手工删除，见 MEMORY §七）
DEFAULT_INDEX_CODES = [
    "SHSE.000300", "SHSE.000905", "SHSE.000852", "SHSE.000688", "SZSE.399006",
]

# 可查询的指标列
METRICS = ("pe_ttm", "pb_mrq", "dy_ttm", "pe_median")

# PE 异常清洗：0 < PE <= 0.5 视为数据异常（等价于盈利 > 2 倍市值，实测全市场仅 2 只）；
# PE <= 0 是亏损，必须计入分母（整体法口径），不剔除。
PE_ANOMALY_LOW = 0.5

# 分位窗口（自然日回溯）
WINDOWS = {"pct_3y": 3 * 365, "pct_5y": 5 * 365, "pct_10y": 10 * 365}

# 按快照区间聚合：一条 SQL 覆盖该区间内所有交易日
SQL_AGGREGATE = text("""
SELECT v.trade_date,
       COUNT(*)                       AS n_total,
       SUM(mv.tot_mv)                 AS mv_total,
       SUM(CASE WHEN v.pe_ttm > :pe_low THEN 1 ELSE 0 END)                    AS n_valid,
       SUM(CASE WHEN v.pe_ttm > :pe_low THEN mv.tot_mv END)                   AS mv_pe_valid,
       SUM(CASE WHEN v.pe_ttm IS NOT NULL AND (v.pe_ttm <= 0 OR v.pe_ttm > :pe_low)
                THEN mv.tot_mv / v.pe_ttm ELSE 0 END)                         AS profit_total,
       SUM(CASE WHEN v.pe_ttm IS NULL OR v.pe_ttm <= :pe_low THEN 1 ELSE 0 END) AS n_loss,
       SUM(CASE WHEN v.pb_mrq > 0 THEN mv.tot_mv / v.pb_mrq ELSE 0 END)       AS equity_total,
       SUM(CASE WHEN v.dy_ttm IS NOT NULL THEN mv.tot_mv END)                 AS mv_dy,
       SUM(CASE WHEN v.dy_ttm IS NOT NULL THEN v.dy_ttm * mv.tot_mv ELSE 0 END) AS dy_num
  FROM bds_index_constituent c
  JOIN bds_daily_valuation v ON v.symbol = c.symbol
  JOIN bds_daily_mktvalue  mv ON mv.symbol = v.symbol AND mv.trade_date = v.trade_date
 WHERE c.index_code = :idx
   AND c.trade_date = :snap
   AND v.trade_date >= :start
   AND v.trade_date <  :end
   AND mv.tot_mv > 0
 GROUP BY v.trade_date
 ORDER BY v.trade_date
""")

# 横截面中位数：MySQL 无 MEDIAN，用窗口函数取中间两位（偶数样本取两个的平均）
SQL_MEDIAN = text("""
SELECT trade_date, AVG(pe_ttm) AS pe_median
  FROM (
    SELECT v.trade_date,
           v.pe_ttm,
           ROW_NUMBER() OVER (PARTITION BY v.trade_date ORDER BY v.pe_ttm) AS rn,
           COUNT(*)     OVER (PARTITION BY v.trade_date)                  AS cnt
      FROM bds_index_constituent c
      JOIN bds_daily_valuation v ON v.symbol = c.symbol
     WHERE c.index_code = :idx
       AND c.trade_date = :snap
       AND v.trade_date >= :start
       AND v.trade_date <  :end
       AND v.pe_ttm > :pe_low
  ) t
 WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))
 GROUP BY trade_date
""")

SQL_CLOSE = text("""
SELECT trade_date, close FROM bds_index_history
 WHERE symbol = :idx AND trade_date >= :start AND trade_date < :end
""")

SQL_UPSERT = text("""
INSERT INTO bds_index_valuation
    (index_code, trade_date, close, pe_ttm, pe_median, pb_mrq, dy_ttm,
     mv_total, profit_total, n_total, n_valid, n_loss, snapshot_date, calc_at)
VALUES
    (:index_code, :trade_date, :close, :pe_ttm, :pe_median, :pb_mrq, :dy_ttm,
     :mv_total, :profit_total, :n_total, :n_valid, :n_loss, :snapshot_date, :calc_at)
ON DUPLICATE KEY UPDATE
    close = VALUES(close), pe_ttm = VALUES(pe_ttm), pe_median = VALUES(pe_median),
    pb_mrq = VALUES(pb_mrq), dy_ttm = VALUES(dy_ttm), mv_total = VALUES(mv_total),
    profit_total = VALUES(profit_total), n_total = VALUES(n_total), n_valid = VALUES(n_valid),
    n_loss = VALUES(n_loss), snapshot_date = VALUES(snapshot_date), calc_at = VALUES(calc_at)
""")


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def resolve_index_codes(index_codes=None) -> list:
    """校验并去重指数清单；不传则用默认 5 个宽基。未知代码抛 ValueError（router 转 400）。"""
    if not index_codes:
        return list(DEFAULT_INDEX_CODES)
    known = dbsCfg.INDEX_CODE
    unknown = [c for c in index_codes if c not in known]
    if unknown:
        raise ValueError(f"未知指数代码：{', '.join(unknown)}")
    seen, out = set(), []
    for c in index_codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _snapshot_ranges(session: Session, index_code: str) -> list:
    """成分快照区间 [(snap, next_snap_or_None), ...]，升序。

    本表按「名单变化」落行，所以 [snap, next_snap) 区间内成分恒定 —— 这是批量聚合成立的前提。
    """
    snaps = [
        r[0] for r in session.query(IndexConstituent.trade_date)
        .filter(IndexConstituent.index_code == index_code)
        .distinct()
        .order_by(IndexConstituent.trade_date.asc())
        .all()
    ]
    return [(snaps[i], snaps[i + 1] if i + 1 < len(snaps) else None) for i in range(len(snaps))]


# ---------------------------------------------------------------------------
# 单区间计算与入库
# ---------------------------------------------------------------------------

def _to_float(v):
    return None if v is None else float(v)


def _calc_range(conn, index_code: str, snap: date, next_snap: Optional[date],
                start_date: date, with_median: bool) -> int:
    """算一个快照区间的全部交易日并 upsert，返回写入行数。"""
    end = next_snap or date(2100, 1, 1)
    params = {"idx": index_code, "snap": snap, "start": max(snap, start_date),
              "end": end, "pe_low": PE_ANOMALY_LOW}

    rows = conn.execute(SQL_AGGREGATE, params).mappings().all()
    if not rows:
        return 0

    medians = {}
    if with_median:
        medians = {r["trade_date"]: r["pe_median"]
                   for r in conn.execute(SQL_MEDIAN, params).mappings().all()}

    closes = {r[0]: r[1] for r in conn.execute(SQL_CLOSE, params).all()}

    now = datetime.now()
    payload = []
    for r in rows:
        mv_total = float(r["mv_total"] or 0)
        profit_total = float(r["profit_total"] or 0)
        equity_total = float(r["equity_total"] or 0)
        mv_dy = float(r["mv_dy"] or 0)
        dy_num = float(r["dy_num"] or 0)
        payload.append({
            "index_code": index_code,
            "trade_date": r["trade_date"],
            # profit_total <= 0 ⇒ 全成分合计亏损，PE 无意义，留空（分位计算会跳过）
            "pe_ttm": round(mv_total / profit_total, 4) if profit_total > 0 else None,
            "pe_median": _to_float(medians.get(r["trade_date"])),
            "pb_mrq": round(mv_total / equity_total, 4) if equity_total > 0 else None,
            "dy_ttm": round(dy_num / mv_dy, 4) if mv_dy > 0 else None,
            "close": _to_float(closes.get(r["trade_date"])),
            "mv_total": round(mv_total, 2),
            "profit_total": round(profit_total, 2),
            "n_total": int(r["n_total"]),
            "n_valid": int(r["n_valid"]),
            "n_loss": int(r["n_loss"]),
            "snapshot_date": snap,
            "calc_at": now,
        })

    conn.execute(SQL_UPSERT, payload)
    return len(payload)


# ---------------------------------------------------------------------------
# 全量回填 / 增量
# ---------------------------------------------------------------------------

def backfill_index_valuation(index_codes=None, start_date: Optional[date] = None,
                             on_progress: Optional[Callable] = None,
                             stop_event=None, with_median: bool = True) -> dict:
    """全量/区间回填。幂等（upsert），可重复执行。

    :param start_date: 不给则从各指数最早快照起；给了则只算 >= start_date 的交易日
    :param on_progress: on_progress(index_code, snap_date, done_ranges, total_ranges)
    :param with_median: 是否同时算横截面中位数（额外一条窗口函数 SQL，约慢一倍）
    :return: {index_code: {"rows": n, "ranges": n, "aborted": bool, "checked_to": date|None}}
    """
    codes = resolve_index_codes(index_codes)
    from server_fast.config import settings
    engine = settings.DB_ENGINE
    out = {}

    for code in codes:
        if stop_event is not None and stop_event.is_set():
            out[code] = {"rows": 0, "ranges": 0, "aborted": True}
            continue

        with SessionLocal() as session:
            ranges = _snapshot_ranges(session, code)
            wm = (session.query(IndexConstituentSync)
                  .filter(IndexConstituentSync.index_code == code).first())
            checked_to = wm.checked_to if wm else None

        if not ranges:
            logger.warning("->%s 无成分快照，跳过估值回填", code)
            out[code] = {"rows": 0, "ranges": 0, "aborted": False, "checked_to": checked_to}
            continue

        # 起点过滤：区间右端 <= start_date 的整段都不需要算
        if start_date is not None:
            ranges = [r for r in ranges if (r[1] is None or r[1] > start_date)]
        total_ranges = len(ranges)
        rows_written, done = 0, 0
        aborted = False

        for snap, next_snap in ranges:
            if stop_event is not None and stop_event.is_set():
                aborted = True
                break
            with engine.begin() as conn:
                n = _calc_range(conn, code, snap, next_snap,
                                start_date or snap, with_median)
            rows_written += n
            done += 1
            if on_progress is not None:
                on_progress(code, snap, done, total_ranges)

        logger.info("->%s 估值回填完成：%d 行 / %d 区间", code, rows_written, done)
        out[code] = {"rows": rows_written, "ranges": done, "aborted": aborted,
                     "checked_to": checked_to}

    return out


def increment_index_valuation(index_codes=None, days: int = 5) -> dict:
    """增量：重算最近 `days` 个交易日（覆盖财报修订导致的 PE 跳变），幂等。

    ⚠️ 前置：应先跑成分增量（水位 `checked_to` 前进），否则可能用旧成分算新日期。
    """
    codes = resolve_index_codes(index_codes)
    start = date.today() - timedelta(days=days * 2)  # 自然日放宽，交易日由数据决定
    return backfill_index_valuation(codes, start_date=start)


# ---------------------------------------------------------------------------
# 查询：序列 / 分位
# ---------------------------------------------------------------------------

def get_series(index_codes=None, start_date: Optional[date] = None,
               end_date: Optional[date] = None, metric: str = "pe_ttm") -> dict:
    """多指数序列（画图用）。结构与 `/index-cum-returns` 对齐，前端可复用图表逻辑。"""
    if metric not in METRICS:
        raise ValueError(f"metric 必须是 {METRICS} 之一")
    codes = resolve_index_codes(index_codes)
    end_date = end_date or date.today()
    start_date = start_date or (end_date - timedelta(days=3650))

    with SessionLocal() as session:
        stmt = text(
            f"SELECT index_code, trade_date, {metric} AS v FROM bds_index_valuation "
            f"WHERE index_code IN :codes AND trade_date BETWEEN :s AND :e "
            f"ORDER BY trade_date"
        ).bindparams(bindparam("codes", expanding=True))
        rows = session.execute(
            stmt, {"s": start_date, "e": end_date, "codes": list(codes)}).all()

        snaps = {
            c: d for c, d in session.query(
                IndexConstituentSync.index_code, IndexConstituentSync.last_change_date
            ).filter(IndexConstituentSync.index_code.in_(list(codes))).all()
        }

    dates, series = [], {c: [] for c in codes}
    tmp = {}
    for code, d, v in rows:
        tmp.setdefault(d, {})[code] = v
    for d in sorted(tmp):
        dates.append(d.isoformat())
        for c in codes:
            series[c].append(tmp[d].get(c))

    return {
        "trade_dates": dates,
        "series": series,
        "sec_names": {c: dbsCfg.INDEX_CODE.get(c, {}).get("sec_name", c) for c in codes},
        "last_change_dates": {c: (snaps.get(c).isoformat() if snaps.get(c) else None) for c in codes},
        "metric": metric,
    }


def list_paged(index_codes=None, start_date: Optional[date] = None,
               end_date: Optional[date] = None, limit: int = 20,
               offset: int = 0) -> dict:
    """行式分页查询（原始数据页用）：一条记录 = 一个指数的一天。

    与 `get_series` 的分工：那个返回 pivot（`trade_dates[]` + `series{}`）给图表画线，
    **中间量全被压掉了**；这里保留 `n_total`/`n_valid`/`n_loss`/`snapshot_date`/`calc_at`，
    用于核对「这个 PE 是怎么算出来的、用的是哪天确认的成分」。

    排序：`trade_date DESC, index_code ASC`——同一天多指数时不会跳行，
    且首页固定是「最新那天 × 5 个指数」，与分位接口的当前值对得上。

    性能（2026-10-20 实测，全表 18,433 行）：`COUNT(*)` 0.17 s（约 0.14 s 是本地 MySQL
    往返固定开销）、`offset=18000` 仍 0.12 s ⇒ 不走 `bds_table_stat` 计数缓存，
    也没有 `daily_valuations`（643 万行）那种深翻页惩罚。
    """
    codes = resolve_index_codes(index_codes)

    where = ["index_code IN :codes"]
    params: dict = {"codes": list(codes)}
    if start_date:
        where.append("trade_date >= :s")
        params["s"] = start_date
    if end_date:
        where.append("trade_date <= :e")
        params["e"] = end_date
    cond = " AND ".join(where)

    with SessionLocal() as session:
        stmt = text(
            "SELECT id, index_code, trade_date, close, pe_ttm, pe_median, pb_mrq, dy_ttm, "
            "       mv_total, profit_total, n_total, n_valid, n_loss, snapshot_date, calc_at, "
            "       create_time, update_time "
            f"  FROM bds_index_valuation WHERE {cond} "
            " ORDER BY trade_date DESC, index_code ASC LIMIT :limit OFFSET :offset"
        ).bindparams(bindparam("codes", expanding=True))
        rows = session.execute(
            stmt, {**params, "limit": limit, "offset": offset}).mappings().all()

        total = session.execute(
            text(f"SELECT COUNT(*) FROM bds_index_valuation WHERE {cond}")
            .bindparams(bindparam("codes", expanding=True)), params).scalar()

    items = []
    for r in rows:
        d = dict(r)
        # 表内不存 sec_name（指数名称随 Config.INDEX_CODE 变更，存库会造成不一致），查询时补
        d["sec_name"] = dbsCfg.INDEX_CODE.get(d["index_code"], {}).get("sec_name", d["index_code"])
        items.append(d)

    return {"items": items, "total": int(total or 0), "limit": limit, "offset": offset}


def get_percentiles(index_codes=None, metric: str = "pe_ttm") -> dict:
    """分位汇总：当前值 vs 全历史 / 10 年 / 5 年 / 3 年 + z-score + 极值。

    **分位不落库**：窗口每天滑动、当前值每天新增，存下来当天有效次日即过期。

    窗口口径**唯一**：自然日回溯（3/5/10 年 = 1095/1825/3650 天），不是「最近 N 个交易日」。
    """
    if metric not in METRICS:
        raise ValueError(f"metric 必须是 {METRICS} 之一")
    codes = resolve_index_codes(index_codes)
    today = date.today()

    with SessionLocal() as session:
        wms = {w.index_code: w for w in session.query(IndexConstituentSync)
               .filter(IndexConstituentSync.index_code.in_(list(codes))).all()}

        # 一次取全部指数序列（逐指数查会 5 倍往返，实测 0.41s → 0.1s 级）
        stmt = text(
            f"SELECT index_code, trade_date, {metric} AS v FROM bds_index_valuation "
            f"WHERE index_code IN :codes AND {metric} IS NOT NULL "
            f"ORDER BY index_code, trade_date"
        ).bindparams(bindparam("codes", expanding=True))
        grouped: dict = {c: [] for c in codes}
        for code, d, v in session.execute(stmt, {"codes": list(codes)}).all():
            grouped[code].append((d, float(v)))

        out = []
        for code in codes:
            rows = grouped.get(code) or []
            if not rows:
                out.append({
                    "index_code": code,
                    "sec_name": dbsCfg.INDEX_CODE.get(code, {}).get("sec_name", code),
                    "metric": metric, "current": None, "n": 0,
                    "last_change_date": None, "constituent_checked_to": None,
                    "constituent_gap_days": None, "constituent_stale": False,
                })
                continue

            last_date, current = rows[-1][0], float(rows[-1][1])
            seq_all = [float(v) for _, v in rows]

            def _stat(days: Optional[int]) -> dict:
                if days is None:
                    seq = seq_all
                else:
                    cut = today - timedelta(days=days)
                    seq = [float(v) for d, v in rows if d >= cut]
                if not seq:
                    return {"pct": None, "min": None, "max": None, "median": None, "z": None,
                            "n": 0}
                n = len(seq)
                srt = sorted(seq)
                mean = sum(seq) / n
                var = sum((x - mean) ** 2 for x in seq) / n if n > 1 else 0.0
                std = var ** 0.5
                return {
                    "pct": round(sum(1 for x in seq if x <= current) / n * 100, 2),
                    "min": round(srt[0], 4), "max": round(srt[-1], 4),
                    "median": round((srt[(n - 1) // 2] + srt[n // 2]) / 2, 4),
                    "z": round((current - mean) / std, 2) if std else None,
                    "n": n,
                }

            wm = wms.get(code)
            out.append({
                "index_code": code,
                "sec_name": dbsCfg.INDEX_CODE.get(code, {}).get("sec_name", code),
                "metric": metric,
                "current": round(current, 4),
                "last_date": last_date.isoformat(),
                "pct_all": _stat(None),
                "pct_10y": _stat(WINDOWS["pct_10y"]),
                "pct_5y": _stat(WINDOWS["pct_5y"]),
                "pct_3y": _stat(WINDOWS["pct_3y"]),
                # 成分新鲜度：分位的可信度取决于成分是否核查到末日
                "last_change_date": wm.last_change_date.isoformat() if wm and wm.last_change_date else None,
                "constituent_checked_to": wm.checked_to.isoformat() if wm and wm.checked_to else None,
                "constituent_gap_days": wm.gap_days if wm else None,
                "constituent_stale": bool(wm and wm.checked_to and wm.checked_to < last_date),
            })

    return {"metric": metric, "items": out}
