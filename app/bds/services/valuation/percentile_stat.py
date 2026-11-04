# -*- coding: utf-8 -*-
"""估值分位**公共内核**：窗口定义 + 单窗口统计 + 全历史滑动窗口。

2026-11-04 抽出，取代散落在 `index_valuation.py` / `stock_valuation.py` 两份 `_stat`。
三个调用方（指数分位专页、个股分位专页、`percentile_store` 落表）共用这里的
`shift_years` / `stat_window`，杜绝「专页一套、列表一套、落表一套」的静默漂移。

口径（`docs/估值分位按日落盘方案_2026-11-04.md` §1、§2）：

- `as_of` = 观察日（分位值所属交易日），窗口右端锚点，取该序列最后一个有值交易日，
  **不是 today**。时间推移只追加新 as_of，不改历史行。
- 窗口左界是**日历对日** `shift_years(as_of, y)`，不是 `as_of - y*365 天`。
  例：`as_of=2025-10-23`、10 年 ⇒ 左界 `2015-10-23`（3650 天口径会给 2015-10-26）。
  左界含当日（`>=`），2/29 落平年 clamp 到 2/28。
- 🔴 **必须显式带 `as_of` 上界**：实时路径的 `rows` 天然截至当前，靠隐含上界成立；
  落表算历史行时手里是全序列，不加会把未来数据算进去（实测所有日期退化成同一个值）。
- `pct = Σ[x <= current] / n × 100`，含等于、含当前值自身；`z = (current-mean)/std`。
- `pct_all` = 全历史（`years=None`），只由分位专页实时算，**不落库**。

分位是按 `as_of` 的纯函数：上游序列不变时，已算出的历史行永久有效。
"""
from __future__ import annotations

import bisect
import math
from datetime import date
from functools import lru_cache
from typing import Optional, Sequence

# 落表与分位专页共用的窗口定义（替代旧 WINDOWS = {3/5/10 * 365 天}）
WINDOW_YEARS: dict[str, int] = {"pct_3y": 3, "pct_5y": 5, "pct_10y": 10}
# 落库只落这三个窗口；pct_all 仍实时算、不落库
WINDOW_KEYS: tuple[str, ...] = ("pct_3y", "pct_5y", "pct_10y")
# 按日落盘的四个指标（两源表同名列），顺序即宽表列序
PCT_METRICS: tuple[str, ...] = ("pe_ttm", "pb_mrq", "ps_ttm", "dy_ttm")
# 指标 → 宽表列前缀
PCT_PREFIX: dict[str, str] = {
    "pe_ttm": "pe", "pb_mrq": "pb", "ps_ttm": "ps", "dy_ttm": "dy",
}

PE_ANOMALY_LOW = 0.5  # 与 index_valuation / stock_valuation 同源：0 < PE <= 0.5 视为异常


@lru_cache(maxsize=30000)
def shift_years(d: date, years: int) -> date:
    """日历对日回溯：月/日不变，年份减 `years`。

    2/29 落到平年时 clamp 到 **2/28**（向前取，窗口略宽，与「左界含当日」一致）。
    不能依赖 `date.replace` 抛异常后由调用方猜 —— 全站只有这一处实现。
    纯函数且入参可哈希 ⇒ lru_cache：6 subject × 4 指标 × 3 窗口里同一天会重复算
    数十万次，缓存后 `date.replace` 调用量降到不同日期数（约 6 千）。
    """
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def clean_value(metric: str, v: Optional[float]) -> bool:
    """个股指标有效值判定（从 `stock_valuation._clean` 搬来，指数侧不用）。

    指数侧沿用「SQL `WHERE metric IS NOT NULL` + 不清洗」的整体法口径，
    两套规则**故意不统一**，统一了数字就会漂移。
    """
    if v is None:
        return False
    if metric in ("pe_ttm", "pcf_ttm_oper"):
        return v > PE_ANOMALY_LOW
    if metric in ("pb_mrq", "ps_ttm"):
        return v > 0
    if metric == "dy_ttm":
        return v >= 0
    return False


_EMPTY_STAT = {"pct": None, "min": None, "max": None,
               "median": None, "z": None, "n": 0}


def _stat_from_vals(vals: list[float], current: float) -> dict:
    """单窗口统计的**唯一精确实现**（与改造前两份 `_stat` 逐值全等）。"""
    if not vals:
        return dict(_EMPTY_STAT)
    n = len(vals)
    srt = sorted(vals)
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n if n > 1 else 0.0
    std = var ** 0.5
    return {
        "pct": round(sum(1 for x in vals if x <= current) / n * 100, 2),
        "min": round(srt[0], 4), "max": round(srt[-1], 4),
        "median": round((srt[(n - 1) // 2] + srt[n // 2]) / 2, 4),
        "z": round((current - mean) / std, 2) if std else None,
        "n": n,
    }


def stat_window(seq: Sequence[tuple[date, float]], current: float,
                years: Optional[int], as_of: date) -> dict:
    """单窗口统计（实时路径用）。

    :param seq: `[(trade_date, value), ...]`，已按口径清洗、日期升序。
    :param current: 截至 `as_of` 的最后一个有效值（不是窗口内最后一个值）。
    :param years: 3/5/10；`None` 退化为 `pct_all` 全历史（专页用，不落库）。
    :param as_of: 窗口右端锚点，**必须显式传入**。

    `years=None` 时仍受 `as_of` 上界约束（全历史 = 截至该日的所有有效值）。
    """
    if years is None:
        vals = [v for d, v in seq if d <= as_of]
    else:
        cut = shift_years(as_of, years)
        vals = [v for d, v in seq if cut <= d <= as_of]
    return _stat_from_vals(vals, current)


def daily_percentiles(seq: Sequence[tuple[date, float]],
                      windows: Sequence[str] = WINDOW_KEYS,
                      rebuild_every: int = 250,
                      only_dates: Optional[set] = None) -> dict[date, dict]:
    """按日全历史分位：滑动窗口 O(n log n) 版，与逐日 `stat_window` **逐值全等**。

    实测（600036 `pe_ttm`，5,965 天 × 3 窗口）：朴素逐日切片+排序 10.902 s，
    本实现 0.081 s（134×），pct 差异 0 条。

    `seq` 是**有效值序列**；上游表中无效值日不在此序列里，由 `percentile_store`
    单独落「整行分位为 NULL + 按 metric 状态」的行（§4.3）。

    :param only_dates: 增量模式（M1/M3）只对这些 `as_of` 构造完整 stat dict；
        滑动窗口状态仍逐日维护以保证口径一致，但不写出的日子省掉 6 字段 dict 与
        5 次 round —— 实测把全量回填的组装成本降一个量级。
    :return: `{as_of: {"pct_3y": stat, "pct_5y": stat, "pct_10y": stat}}`
             默认含 `seq` 中所有日期；给 `only_dates` 时只含目标日。
    """
    if only_dates is not None and not isinstance(only_dates, set):
        only_dates = set(only_dates)
    out: dict[date, dict] = {}
    for key in windows:
        years = WINDOW_YEARS[key]
        sv: list[float] = []       # 窗口内有序值
        lo = 0                    # 窗口左端在 seq 中的下标（只前进不回退）
        s = 0.0                   # 增量 sum
        ss = 0.0                  # 增量 sumsq
        for i, (as_of, cur) in enumerate(seq):
            cut = shift_years(as_of, years)
            # 滑出：shift_years 随 as_of 单调不减（1999~2029 逐日实测 0 次回退）
            while lo <= i and seq[lo][0] < cut:
                v = seq[lo][1]
                sv.pop(bisect.bisect_left(sv, v))
                s -= v
                ss -= v * v
                lo += 1
            bisect.insort(sv, cur)
            s += cur
            ss += cur * cur

            # 增量浮点累加会累积舍入差（主要影响 z）⇒ 周期性用窗口内真值重建
            if rebuild_every and i % rebuild_every == 0:
                s = math.fsum(sv)
                ss = math.fsum(x * x for x in sv)

            # 增量模式：非目标日只维护滑动状态，不构造 stat dict / 不做 round
            if only_dates is not None and as_of not in only_dates:
                continue
            line = out.setdefault(as_of, {})
            n = len(sv)
            if not n:
                line[key] = dict(_EMPTY_STAT)
                continue
            mean = s / n
            var = max(ss / n - mean * mean, 0.0)
            std = var ** 0.5
            line[key] = {
                "pct": round(bisect.bisect_right(sv, cur) / n * 100, 2),
                "min": round(sv[0], 4), "max": round(sv[-1], 4),
                "median": round((sv[(n - 1) // 2] + sv[n // 2]) / 2, 4),
                "z": round((cur - mean) / std, 2) if std else None,
                "n": n,
            }
    return dict(sorted(out.items(), key=lambda kv: kv[0]))


def window_cuts(as_of: date, windows: Sequence[str] = WINDOW_KEYS) -> dict[str, str]:
    """本行实际用的三个左界（落 `window_cut` 审计列，可事后证明用的是对日口径）。"""
    return {k: shift_years(as_of, WINDOW_YEARS[k]).isoformat() for k in windows}
