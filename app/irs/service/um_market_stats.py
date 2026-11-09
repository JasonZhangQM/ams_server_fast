# -*- coding: utf-8 -*-
"""标的监测抽屉「估值与走势」Tab 的聚合数据（2026-11-08）。

纯读、无落库、无写按钮。四块数据一次返回，口径全在后端算，前端零计算：

1. 估值分位：`irs_um_percentile`（🔒 分位读取唯一入口，`um_percentile.read_row`）——
   不另写公式、不回落实时计算、不从 `bds_valuation_percentile` 历史表派生。
2. 估值数据：按 `subject_type` 分别读 `bds_index_valuation` / `bds_daily_valuation`。
3. 累计收益率：`bds_history_market.close`（个股后复权 / 指数不复权）。
4. 回撤：与首页共用 `bds/services/market_data/cum_return.py`，不补实时价。

数据缺失（实测 GC00Y / SPX 无估值与日频行情）⇒ `coverage=false` +
主表行情字段降级展示。绝不猜价、不拿实时价凑数。

依赖方向（🔒4.12）：irs → bds 允许；本模块只读 bds 的表名字符串与公共函数，
不 import irs 之外的业务模块，也不让 bds 反向依赖 irs。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.app.bds.services.market_data.cum_return import (
    cum_return_drawdown,
    drawdown_summary,
    iso_date,
)
from server_fast.app.bds.services.valuation.percentile_stat import shift_years
from server_fast.common.db import SessionLocal

# 区间：数字走「日历对日」（与分位窗口同口径，2/29 落平年 clamp 到 2/28）
RANGE_YEARS = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}
# 指标白名单：两张估值源表共有的 4 列，拼 SQL 前必须命中，防注入
METRIC_LABELS = {
    "pe_ttm": "PE(TTM)",
    "pb_mrq": "PB(MRQ)",
    "ps_ttm": "PS(TTM)",
    "dy_ttm": "股息率(%)",
}
SOURCE_TABLES = {
    "index": ("bds_index_valuation", "index_code"),
    "stock": ("bds_daily_valuation", "symbol"),
}


def _start_date(range_key: str, today: date) -> Optional[date]:
    """`all` 返回 None（不加下界），其余返回具体起点。"""
    if range_key == "all":
        return None
    if range_key == "ytd":
        return date(today.year, 1, 1)
    return shift_years(today, RANGE_YEARS.get(range_key, 5))


def _latest_trade_date(db: Session) -> Optional[date]:
    """交易日历含未来交易日 ⇒ 必须 `<= CURDATE()`；大表取最新日禁 MAX()。"""
    return db.execute(text(
        "SELECT trade_date FROM bds_trade_date WHERE trade_date <= CURDATE() "
        "ORDER BY trade_date DESC LIMIT 1")).scalar()


def _subject_type(db: Session, symbol: str) -> Optional[str]:
    """优先取分位表的判定；无分位行时按两张估值源表实有数据判定。

    `GC00Y` / `SPX` 两处都查不到 ⇒ None ⇒ 估值块留空走降级。
    """
    row = db.execute(text(
        "SELECT subject_type FROM irs_um_percentile WHERE symbol = :s LIMIT 1"),
        {"s": symbol}).first()
    if row and row[0] in SOURCE_TABLES:
        return row[0]
    for st, (table, key) in SOURCE_TABLES.items():
        hit = db.execute(text(
            f"SELECT 1 FROM {table} WHERE {key} = :s LIMIT 1"), {"s": symbol}).first()
        if hit:
            return st
    return None


def _load_valuation(db: Session, symbol: str, subject_type: str,
                    metric: str, start: Optional[date]) -> Optional[dict]:
    """估值原始值序列（不是分位序列）——分位只从 `irs_um_percentile` 读。"""
    if metric not in METRIC_LABELS or subject_type not in SOURCE_TABLES:
        return None
    table, key = SOURCE_TABLES[subject_type]
    sql = (f"SELECT trade_date, {metric} FROM {table} "
           f"WHERE {key} = :s AND trade_date <= CURDATE() ")
    params: dict = {"s": symbol}
    if start:
        sql += "AND trade_date >= :d0 "
        params["d0"] = start
    sql += "ORDER BY trade_date ASC"
    rows = db.execute(text(sql), params).all()
    if not rows:
        return None
    dates = [iso_date(r[0]) for r in rows]
    values = [None if r[1] is None else round(float(r[1]), 4) for r in rows]
    # 末日 = 最后一个**有值**的日期（源表可能有 NULL 行）
    idx = next((i for i in range(len(values) - 1, -1, -1) if values[i] is not None), None)
    return {
        "dates": dates,
        "values": values,
        "as_of": dates[idx] if idx is not None else None,
        "current": values[idx] if idx is not None else None,
    }


def _load_prices(db: Session, symbol: str,
                 start: Optional[date]) -> Optional[tuple[pd.DataFrame, Optional[str]]]:
    """日频收盘价。按 (symbol, trade_date) 去重，同日优先保留带 sec_type 标签的正式行。

    实测 `bds_history_market` 有 `sec_type/adjust` 为 NULL 的残留行（4 个指数各 1 行），
    不去重会让同一天出现两个点、回撤序列错位。
    """
    sql = ("SELECT trade_date, close, sec_type, adjust FROM bds_history_market "
           "WHERE symbol = :s AND trade_date <= CURDATE() ")
    params: dict = {"s": symbol}
    if start:
        sql += "AND trade_date >= :d0 "
        params["d0"] = start
    sql += "ORDER BY trade_date ASC"
    rows = db.execute(text(sql), params).all()
    by_date: dict = {}
    for d, close, sec_type, _adjust in rows:
        if close is None:
            continue
        if d in by_date and sec_type is None:
            continue
        by_date[d] = float(close)
    if not by_date:
        return None
    keys = sorted(by_date)
    df = pd.DataFrame({"close": [by_date[k] for k in keys]},
                      index=pd.to_datetime(keys))
    adjust = next((r[3] for r in rows if r[3]), None)
    return df, adjust


def _fallback(db: Session, symbol: str) -> Optional[dict]:
    """无估值/行情标的的降级展示：主表现成行情字段，后端算百分比。"""
    r = db.execute(text(
        "SELECT price, py_close, y_high, y_low FROM irs_underlying_monitor "
        "WHERE symbol = :s"), {"s": symbol}).first()
    if not r:
        return None
    price, py_close, y_high, y_low = [None if v is None else float(v) for v in r]

    def pct(a, b):
        if a is None or b in (None, 0):
            return None
        return round((a / b - 1) * 100, 2)

    return {
        "price": price,
        "py_close": py_close,
        "y_high": y_high,
        "y_low": y_low,
        "ytd_return_pct": pct(price, py_close),
        "to_high_pct": pct(price, y_high),
        "to_low_pct": pct(price, y_low),
    }


def _arr(series: pd.Series) -> list:
    return [None if pd.isna(v) else float(v) for v in series]


def build(symbol: str, range_key: str = "5y", metric: str = "pe_ttm",
          db: Optional[Session] = None) -> dict:
    """一次返回四块数据。`symbol` 是业务主键（id 会漂移，禁用 id 关联）。"""
    own = db is None
    session = SessionLocal() if own else db
    try:
        today = date.today()
        start = _start_date(range_key, today)
        latest_td = _latest_trade_date(session)

        # ① 分位：唯一入口，只读整行（函数内导入，避开 service 包初始化顺序）
        from server_fast.app.irs.service import um_percentile

        pct = um_percentile.read_row(symbol, session)
        subject_type = (pct or {}).get("subject_type") or _subject_type(session, symbol)

        # ② 估值原始值序列
        val = _load_valuation(session, symbol, subject_type, metric, start) if subject_type else None

        # ③④ 累计收益率 + 回撤（同一条价格序列，同一套口径）
        prices = _load_prices(session, symbol, start)
        perf = None
        if prices is not None and not prices[0].empty:
            df, adjust = prices
            cum, cum_ret, dd = cum_return_drawdown(df)
            perf = {
                "dates": [iso_date(d) for d in df.index],
                "cum_return": _arr(cum_ret["close"]),
                "drawdown": _arr(dd["close"]),
                "summary": drawdown_summary(cum, cum_ret, dd, "close"),
                # 个股后复权、指数不复权，前端只展示不推断
                "adjust": adjust or ("post" if subject_type == "stock" else "none"),
                "first_close": round(float(df["close"].iloc[0]), 4),
                "last_close": round(float(df["close"].iloc[-1]), 4),
            }

        coverage = {
            "percentile": pct is not None,
            "valuation": bool(val and any(v is not None for v in val["values"])),
            "price": perf is not None,
        }
        price_as_of = (perf or {}).get("summary", {}).get("end_date")
        stale_days = None
        if latest_td and price_as_of:
            # 交易日口径：0=当日、1=T−1、2=T−2（按 bds_trade_date 数交易日）。
            # 与风险看板/风险引擎共用 risk_scenario._stale_days，禁止用日历天差
            # ——跨周末会凭空虚高 2 天，也禁止在本模块另写一套计数。
            # 函数内导入：避开 service 包初始化顺序（同 um_percentile）。
            from server_fast.app.irs.service import risk_scenario

            stale_days = risk_scenario._stale_days(
                session, date.fromisoformat(price_as_of), latest_td)

        if not coverage["valuation"] and not coverage["price"]:
            message = "该标的暂无估值与日频行情落表数据（外盘 / 商品类），下方仅展示主表行情字段，不构成累计收益率或回撤。"
        elif not coverage["valuation"]:
            message = "该标的无估值序列，仅展示行情走势与回撤。"
        elif not coverage["price"]:
            message = "该标的无日频行情序列，仅展示估值分位与估值曲线。"
        elif not coverage["percentile"]:
            message = "该标的最新估值分位截面缺失（未落盘），分位区显示 —。"
        else:
            message = None

        return {
            "symbol": symbol,
            "subject_type": subject_type,
            "range": range_key,
            "metric": metric,
            "metric_label": METRIC_LABELS[metric],
            "coverage": coverage,
            "percentile": pct,
            "valuation": val,
            "performance": perf,
            "fallback": _fallback(session, symbol),
            "freshness": {
                "latest_trade_date": iso_date(latest_td),
                "percentile_as_of": (pct or {}).get("as_of"),
                "percentile_source_as_of": (pct or {}).get("source_as_of"),
                "valuation_as_of": (val or {}).get("as_of"),
                "price_as_of": price_as_of,
                "stale_days": stale_days,
            },
            "message": message,
        }
    finally:
        if own:
            session.close()
