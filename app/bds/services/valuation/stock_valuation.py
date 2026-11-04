# -*- coding: utf-8 -*-
"""个股估值日序列与分位（2026-11-02，`/irs/valuation-percent` 个股模式）。

与指数分位（`index_valuation.py`）的分工与差异：

- **数据源直接是源表**：个股没有"成分聚合"这一层，`bds_daily_valuation` 一行就是
  一个标的一天的估值，所以没有回填任务、没有覆盖率门禁、没有快照区间——
  查询即计算，读路径唯一。
- **指标集比指数少 `pe_median`**：横截面中位数对单标的无意义（它是"当日一篮子
  股票 PE 的中位"，单只股票没有横截面），白名单直接不含。
- **无效值清洗比指数激进**：指数用整体法把亏损股"计入分母"消化掉了负 PE；
  个股自身序列没有这个选项——负 PE（亏损）、负 PB（净资产为负）、负 PCF
  （经营现金流为负）与正值的语义不可比，混入会把分位系统性抬高
  （负值必然排在正值左边，等于给当前值"垫底"）。故窗口样本只保留有效值：
  pe_ttm / pcf_ttm_oper > 0.5（PE_ANOMALY_LOW，同指数口径），pb_mrq / ps_ttm > 0，
  dy_ttm >= 0。当前值无效时不算分位、单独给 status，前端标"亏损/净资产为负"。
- **股息率是反向指标**：dy_ttm 分位高 = 股息高 = 便宜。后端只出原始分位，
  反转染色由前端做（数字保持原口径，表里才能与 PE/PB 并排核对）。
- **窗口样本不足**：上市不足窗口期（或长期停牌）时窗口实际覆盖不到名义年限，
  每个窗口回 `windows_short` 标记（n < MIN_WINDOW_SAMPLES），前端提示，不静默。

窗口口径与指数分位**完全一致**：**日历对日**回溯 3/5/10 年
（`as_of=2025-10-23` 的 10 年窗口左界 `2015-10-23`；2/29 clamp 到 2/28），
不是旧的 `1095/1825/3650 自然日`，也不是「最近 N 个交易日」。
锚点 = 该标的最后一个有值交易日（不是 today），数据陈旧时窗口左端不漂移。
公式统一走公共内核 `percentile_stat.stat_window`，落表路径同调一份。

**分位不落库**这句话只对**本函数**成立：它保留实时计算供任意 A 股 / 5 指标分析；
按日固化的历史分位另落 `bds_valuation_percentile`（见方案 §6.4 处置矩阵）。
"""
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text

from server_fast.app.bds.models import SymbolInfo
from server_fast.app.bds.services.valuation.percentile_stat import (
    WINDOW_YEARS, clean_value, stat_window,
)
from server_fast.common.db import SessionLocal

logger = logging.getLogger("uvicorn.error")

# 个股可查询的指标白名单：比指数少 pe_median（横截面中位数对单标的无意义）
STOCK_METRICS = ("pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm_oper", "dy_ttm")

# 与指数口径一致的 PE/PCF 异常下限（0 < v <= 0.5 视为数据异常，等价盈利 > 2 倍市值）
PE_ANOMALY_LOW = 0.5

# 一次最多对比多少只个股：卡片网格按 5 列设计，>5 张卡会换行；
# 且每只全历史 ~6000 行拉进内存现算，上限放宽没收益
MAX_SYMBOLS = 10

# 窗口样本数低于此值 → windows_short 标记（约一年交易日，低于它说明
# 该标的实际历史覆盖不到名义窗口，分位参考价值打折）
MIN_WINDOW_SAMPLES = 250


def _clean(metric: str, v: Optional[float]) -> bool:
    """指标有效值判定（窗口样本与"当前值"共用同一把尺子）。

    规则本体已搬到公共内核 `percentile_stat.clean_value`，落表路径同一把尺子；
    这里保留薄封装，避免序列端点 / 分位函数里的既有调用点全部改名。
    """
    return clean_value(metric, v)


def resolve_symbols(symbols) -> list:
    """校验个股清单：必须是在市 A 股个股（`bds_symbol_info.market` 非空）。

    - 未知代码 / 非A股（指数、美股、黄金的 market 恒为 NULL）→ ValueError（router 转 400）
    - 已退市（`delisted_date <= CURDATE()`，见 MEMORY §七 铁律 1）→ ValueError
    - 去重保序，上限 MAX_SYMBOLS
    """
    if not symbols:
        raise ValueError("symbols 不能为空：请先选择个股")
    seen, uniq = set(), []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    if len(uniq) > MAX_SYMBOLS:
        raise ValueError(f"一次最多对比 {MAX_SYMBOLS} 只个股")
    today = date.today()
    rows = (
        SessionLocal()
        .query(SymbolInfo.symbol, SymbolInfo.name, SymbolInfo.market, SymbolInfo.delisted_date)
        .filter(SymbolInfo.symbol.in_(uniq))
        .all()
    )
    found = {r.symbol: r for r in rows}
    unknown = [s for s in uniq if s not in found]
    if unknown:
        raise ValueError(f"未知证券代码：{', '.join(unknown)}")
    not_stock = [s for s in uniq if found[s].market is None]
    if not_stock:
        raise ValueError(f"非 A 股个股，不支持个股分位：{', '.join(not_stock)}")
    delisted = [s for s in uniq if found[s].delisted_date and found[s].delisted_date <= today]
    if delisted:
        raise ValueError(f"已退市，不支持个股分位：{', '.join(delisted)}")
    return uniq


# ---------------------------------------------------------------------------
# 查询：序列 / 分位
# ---------------------------------------------------------------------------

def get_series(symbols, start_date: Optional[date] = None,
               end_date: Optional[date] = None, metric: str = "pe_ttm") -> dict:
    """多个股估值日序列（画图用）。结构与指数 `/index-valuations` 对齐，
    前端 ValuationChart 可原样复用。

    返回的 series 已按指标口径清洗（无效值置 null 并断线），否则一只曾经的
    亏损股会把整条 PE 曲线的纵轴拉到负区间，图就没法看了。
    `data_ranges` 是**原始**覆盖区间（含被清洗的日子），供前端标"数据从哪天起"。
    """
    if metric not in STOCK_METRICS:
        raise ValueError(f"metric 必须是 {STOCK_METRICS} 之一")
    codes = resolve_symbols(symbols)
    end_date = end_date or date.today()
    start_date = start_date or (end_date - timedelta(days=3650))

    from sqlalchemy import bindparam

    with SessionLocal() as session:
        infos = {
            r.symbol: r.name
            for r in session.query(SymbolInfo.symbol, SymbolInfo.name)
            .filter(SymbolInfo.symbol.in_(codes)).all()
        }
        stmt = text(
            f"SELECT symbol, trade_date, {metric} AS v FROM bds_daily_valuation "
            "WHERE symbol IN :codes AND trade_date BETWEEN :s AND :e "
            "ORDER BY trade_date"
        ).bindparams(bindparam("codes", expanding=True))
        rows = session.execute(stmt, {"codes": codes, "s": start_date, "e": end_date}).all()

    dates, series, ranges = [], {c: [] for c in codes}, {}
    tmp: dict = {}
    raw_seen: dict = {}
    for code, d, v in rows:
        f = None if v is None else float(v)
        tmp.setdefault(d, {})[code] = f if _clean(metric, f) else None
        lo, hi = raw_seen.get(code, (d, d))
        raw_seen[code] = (min(lo, d), max(hi, d))
    for d in sorted(tmp):
        dates.append(d.isoformat())
        for c in codes:
            series[c].append(tmp[d].get(c))
    for c in codes:
        lo, hi = raw_seen.get(c, (None, None))
        ranges[c] = {"first": lo.isoformat() if lo else None,
                     "last": hi.isoformat() if hi else None}

    return {
        "trade_dates": dates,
        "series": series,
        "sec_names": {c: infos.get(c, c) for c in codes},
        "metric": metric,
        "data_ranges": ranges,
    }


def _stat(seq: list, current: float, years: Optional[int], as_of: date) -> dict:
    """单窗口统计（薄封装，公式唯一来源 `percentile_stat.stat_window`）。

    `years` 是**年数**（3/5/10），`None` = 全历史 `pct_all`；`as_of` 是窗口右端锚点，
    公共内核会显式加 `d <= as_of` 上界 —— 实时路径的 `seq` 虽天然截至当前，
    但把上界写进公式才能与落表算历史行时用同一份实现。
    """
    return stat_window(seq, current, years, as_of)


def get_percentiles(symbols, metric: str = "pe_ttm") -> dict:
    """个股分位汇总：当前值 vs 全历史 / 10 / 5 / 3 年 + z-score + 极值。

    结构与指数 `/index-valuation-percentiles` 对齐（items 内主键是 `symbol`），
    另带个股特有字段：`status`（ok/loss/no_data）、`status_note`、`online_date`、
    `first_date`（数据首日）、`raw_n`/`cleaned_n`、`windows_short`。
    窗口口径与指数分位完全一致（见模块 docstring）。

    **历史分位已按 `as_of` 落 `bds_valuation_percentile`**（2026-11-04）；本函数保留实时
    计算，供任意 A 股 / 5 指标分析与影子校验。原「窗口每天滑动、存下次日过期」的理由
    已被 `as_of` 参数化消解。落表只固化 6 个 irs subject × 4 指标 × 3 窗口，
    本端点的任意选股能力不受影响。
    """
    if metric not in STOCK_METRICS:
        raise ValueError(f"metric 必须是 {STOCK_METRICS} 之一")
    codes = resolve_symbols(symbols)

    with SessionLocal() as session:
        infos = {r.symbol: r for r in session.query(SymbolInfo)
                 .filter(SymbolInfo.symbol.in_(codes)).all()}
        # 最新「可算数据」日：与指数页同一把尺子（估值源末日）。
        # ⚠️ 必须 ORDER BY ... DESC LIMIT 1，禁 MAX()（k_dv_td 索引，见 index_valuation 注释）
        latest_available = session.execute(text(
            "SELECT trade_date FROM bds_daily_valuation ORDER BY trade_date DESC LIMIT 1"
        )).scalar()

    out = []
    for code in codes:
        info = infos.get(code)
        with SessionLocal() as session:
            rows = session.execute(
                text(f"SELECT trade_date, {metric} AS v FROM bds_daily_valuation "
                     "WHERE symbol = :c ORDER BY trade_date"),
                {"c": code},
            ).all()

        raw = [(d, None if v is None else float(v)) for d, v in rows]
        if not raw or all(v is None for _, v in raw):
            out.append({
                "symbol": code,
                "sec_name": info.name if info else code,
                "industry": info.industry if info else None,
                "board": info.board if info else None,
                "online_date": info.online_date.isoformat() if info and info.online_date else None,
                "metric": metric, "current": None, "current_valid": False,
                "status": "no_data", "status_note": "估值源无该标的数据",
                "last_date": None, "as_of": None, "is_stale": False, "stale_days": None,
                "first_date": None, "raw_n": len(raw), "cleaned_n": 0,
                "pct_all": None, "pct_10y": None, "pct_5y": None, "pct_3y": None,
                "windows_short": [],
            })
            continue

        last_date, current_raw = raw[-1][0], raw[-1][1]
        seq = [(d, v) for d, v in raw if _clean(metric, v)]
        first_date = raw[0][0]
        stale = bool(latest_available and last_date < latest_available)

        base = {
            "symbol": code,
            "sec_name": info.name if info else code,
            "industry": info.industry if info else None,
            "board": info.board if info else None,
            "online_date": info.online_date.isoformat() if info and info.online_date else None,
            "metric": metric,
            "current": current_raw,
            "last_date": last_date.isoformat(),
            "as_of": last_date.isoformat(),
            "is_stale": stale,
            "stale_days": (latest_available - last_date).days if stale else 0,
            "first_date": first_date.isoformat(),
            "raw_n": len(raw),
            "cleaned_n": len(seq),
        }

        if not seq:
            # 当前值无效且历史上也没有有效样本：按指标给出人话
            note = {
                "pe_ttm": "PE 无有效样本（上市以来亏损或数据异常）",
                "pcf_ttm_oper": "PCF 无有效样本（经营现金流持续为负或数据异常）",
                "pb_mrq": "PB 无有效样本（净资产为负）",
                "ps_ttm": "PS 无有效样本（营收缺失或数据异常）",
                "dy_ttm": "股息率无有效样本（从未分红）",
            }.get(metric, "无有效样本，无法计算分位")
            base.update({"current_valid": False, "status": "loss", "status_note": note,
                         "pct_all": None, "pct_10y": None, "pct_5y": None, "pct_3y": None,
                         "windows_short": []})
            out.append(base)
            continue

        current = seq[-1][1]
        windows_short = []
        for key, years in (("pct_all", None), ("pct_10y", WINDOW_YEARS["pct_10y"]),
                           ("pct_5y", WINDOW_YEARS["pct_5y"]),
                           ("pct_3y", WINDOW_YEARS["pct_3y"])):
            st = _stat(seq, current, years, last_date)
            base[key] = st
            if st["n"] < MIN_WINDOW_SAMPLES:
                windows_short.append(key)

        if not _clean(metric, current_raw):
            # 历史有有效样本、当前值无效：分位一律不给。
            # 窗口锚定在最后有值日，若照常出分位，等于拿旧分位冒充当前估值，
            # 且亏损期的"低 PE"会伪装成便宜 —— 宁可空着也不能误导。
            note = {
                "pe_ttm": f"当前 PE={current_raw:.2f}（亏损或异常），不参与分位",
                "pcf_ttm_oper": f"当前 PCF={current_raw:.2f}（现金流为负或异常），不参与分位",
                "pb_mrq": f"当前 PB={current_raw:.2f}（净资产为负），不参与分位",
                "ps_ttm": f"当前 PS={current_raw:.2f}（异常），不参与分位",
                "dy_ttm": f"当前股息率={current_raw:.2f}（异常），不参与分位",
            }.get(metric, "当前值无效，不参与分位")
            base.update({"current_valid": False, "status": "loss", "status_note": note,
                         "pct_all": None, "pct_10y": None, "pct_5y": None, "pct_3y": None,
                         "windows_short": []})
        else:
            base.update({"current_valid": True, "status": "ok", "status_note": None,
                         "windows_short": windows_short})
        out.append(base)

    return {
        "metric": metric,
        "latest_trade_date": latest_available.isoformat() if latest_available else None,
        "items": out,
    }
