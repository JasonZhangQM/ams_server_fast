# -*- coding: utf-8 -*-
"""累计收益率 / 回撤序列的**唯一实现**（2026-11-08 从 bds/router.py 抽出）。

两个调用方共用，禁止各自再写一遍公式：
- `GET /bds/index-cum-returns`（首页多指数累计收益图）
- `GET /irs/um-market-stats`（标的监测抽屉「估值与走势」Tab）

口径（与抽出前首页实现逐值一致）：
- 累计净值 `cum = close / 区间首日 close`
- 累计收益率 `(cum - 1) × 100`，四舍五入 2 位
- 当日回撤 `(cum / cum.expanding().max() - 1) × 100`，≤ 0，四舍五入 2 位

⚠️ 入参 DataFrame 必须 index=trade_date 升序、每列一个标的、值为收盘价；
   区间裁剪与实时价补点由调用方负责，本函数不碰日期、不猜价格。
"""
from __future__ import annotations

import pandas as pd


def cum_return_drawdown(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 `(累计净值, 累计收益率%, 当日回撤%)`，列名/索引与入参一致。

    累计净值一并返回，供调用方定位「区间最大回撤的峰值日」，避免用已经
    round(2) 的收益率反推峰值（精度损失会错一天）。
    """
    cum = df / df.iloc[0]
    cum_return = ((cum - 1) * 100).round(2)
    running_max = cum.expanding().max()
    drawdown = ((cum / running_max - 1) * 100).round(2)
    return cum, cum_return, drawdown


def iso_date(v) -> str | None:
    """DataFrame 索引可能是 date / Timestamp / str，统一成 YYYY-MM-DD。"""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def drawdown_summary(cum: pd.DataFrame, cum_return: pd.DataFrame,
                     drawdown: pd.DataFrame, col) -> dict:
    """单标的区间摘要：数值与日期全部由后端算好，前端只展示。

    `max_drawdown` 是**本区间**最大回撤，不是全史；峰值日取谷底之前（含谷底）
    累计净值最高的那一天。`recovered` 表示谷底之后是否重新回到前高（回撤 ≥ -0.01%）。
    """
    cr = cum_return[col].dropna()
    dd = drawdown[col].dropna()
    cv = cum[col].dropna()
    if cr.empty or dd.empty or cv.empty:
        return {}
    trough_date = dd.idxmin()
    before = cv.loc[:trough_date]
    peak_date = before.idxmax() if not before.empty else cr.index[0]
    after = dd.loc[dd.index > trough_date]
    recovered = after[after >= -0.01]
    return {
        "start_date": iso_date(cr.index[0]),
        "end_date": iso_date(cr.index[-1]),
        "cum_return": float(cr.iloc[-1]),
        "current_drawdown": float(dd.iloc[-1]),
        "max_drawdown": float(dd.min()),
        "max_dd_peak_date": iso_date(peak_date),
        "max_dd_trough_date": iso_date(trough_date),
        "max_dd_peak_value": float(cr.loc[peak_date]),
        "max_dd_trough_value": float(cr.loc[trough_date]) if trough_date in cr.index else None,
        "recovered": not recovered.empty,
        "recover_date": iso_date(recovered.index[0]) if not recovered.empty else None,
    }
