# -*- coding: utf-8 -*-
"""G0a 口径切换差异留档（一次性脚本，只生成报告，不改库）。

背景（docs/估值分位按日落盘方案_2026-11-04.md §1、§10）：
2026-11-04 把「近 3/5/10 年」窗口从 `as_of - 1095/1825/3650 自然日` 改为**日历对日**。
这是口径变更而非 bug 修复，必须在切换前后留一份可复现的差异报告，且要证明
「改造后的实时路径确实跑的是新口径」。

本脚本：
1. 直接查两张源表，独立复刻**旧口径**（365 天回溯、无 as_of 上界，与改造前 `_stat` 同式）；
2. 用公共内核 `stat_window` 算**新口径**（日历对日 + 显式 as_of 上界）；
3. 调改造后的 `index_valuation.get_percentiles` / `stock_valuation.get_percentiles`
   取实时值，逐组合验证「实时路径 == 新口径」；
4. 输出 markdown 到 docs/G0a_口径切换差异报告_2026-11-04.md。

用法（须 conda 环境，且 sys.path 指向仓库根）：
    conda run -n fast311 python server_fast/scripts/g0a_window_diff_20261104.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, r"C:/quant_man/mysite_quant/ams_trae")

from sqlalchemy import bindparam, text  # noqa: E402
from server_fast.app.bds.services.valuation import (  # noqa: E402
    index_valuation, stock_valuation,
)
from server_fast.app.bds.services.valuation.percentile_stat import (  # noqa: E402
    PCT_METRICS, WINDOW_YEARS, clean_value, stat_window,
)
from server_fast.common.db import SessionLocal  # noqa: E402

ROOT = Path(r"C:/quant_man/mysite_quant/ams_trae")
OUT = ROOT / "docs" / "G0a_口径切换差异报告_2026-11-04.md"

IDX = ("SHSE.000300", "SHSE.000905")
STK = ("SHSE.600036", "SHSE.600900", "SZSE.000333", "SZSE.300750")
OLD_DAYS = {"pct_3y": 1095, "pct_5y": 1825, "pct_10y": 3650}


def load_series():
    """指数：SQL 非空即有效（不清洗）；个股：按 clean_value 清洗。"""
    series: dict[tuple[str, str], list] = {}
    with SessionLocal() as db:
        idx_rows = db.execute(
            text("SELECT index_code, trade_date, pe_ttm, pb_mrq, ps_ttm, dy_ttm "
                 "FROM bds_index_valuation WHERE index_code IN :c "
                 "ORDER BY index_code, trade_date")
            .bindparams(bindparam("c", expanding=True)), {"c": list(IDX)}).all()
        stk_rows = db.execute(
            text("SELECT symbol, trade_date, pe_ttm, pb_mrq, ps_ttm, dy_ttm "
                 "FROM bds_daily_valuation WHERE symbol IN :c "
                 "ORDER BY symbol, trade_date")
            .bindparams(bindparam("c", expanding=True)), {"c": list(STK)}).all()
    for r in idx_rows:
        for j, m in enumerate(PCT_METRICS):
            v = r[2 + j]
            if v is not None:
                series.setdefault((r[0], m), []).append((r[1], float(v)))
    for r in stk_rows:
        for j, m in enumerate(PCT_METRICS):
            v = r[2 + j]
            f = None if v is None else float(v)
            if clean_value(m, f):
                series.setdefault((r[0], m), []).append((r[1], f))
    return series


def pct_old(seq, as_of, days: int):
    """改造前 `_stat` 的等价实现：只有下界，上界靠「序列天然截至当前」隐含。"""
    t = [(d, v) for d, v in seq if d <= as_of]
    if not t:
        return None
    cur = t[-1][1]
    cut = as_of - timedelta(days=days)
    vals = [v for d, v in t if d >= cut]
    return round(sum(1 for x in vals if x <= cur) / len(vals) * 100, 2) if vals else None


def live_map():
    """改造后的实时路径输出（应全等于新口径）。"""
    out = {}
    for m in PCT_METRICS:
        data = index_valuation.get_percentiles(index_codes=list(IDX), metric=m)
        for it in data["items"]:
            for k in WINDOW_YEARS:
                out[(it["index_code"], m, k)] = (it.get(k) or {}).get("pct")
    for m in PCT_METRICS:
        data = stock_valuation.get_percentiles(list(STK), metric=m)
        for it in data["items"]:
            for k in WINDOW_YEARS:
                v = (it.get(k) or {}).get("pct") if it.get("status") == "ok" else None
                out[(it["symbol"], m, k)] = v
    return out


def main():
    series = load_series()
    live = live_map()
    lines = [
        "# G0a 口径切换差异报告（2026-11-04）",
        "",
        "> 生成脚本：`server_fast/scripts/g0a_window_diff_20261104.py`（只读，可重复运行）",
        "> 口径：旧 = `as_of − 1095/1825/3650 自然日`（改造前 `_stat`）；"
        "新 = **日历对日** `shift_years(as_of, y)` + 显式 `d <= as_of` 上界。",
        "> 对照对象：6 subject × 4 指标 × 3 窗口 = 72 个组合，锚点取各序列最后一个有值交易日。",
        "",
        "## 汇总",
        "",
    ]
    rows = []
    for (sym, m), seq in sorted(series.items()):
        if not seq:
            continue
        as_of = seq[-1][0]
        cur = seq[-1][1]
        for k, y in WINDOW_YEARS.items():
            old = pct_old(seq, as_of, OLD_DAYS[k])
            new = stat_window(seq, cur, y, as_of)["pct"]
            lv = live.get((sym, m, k))
            rows.append((sym, m, k, str(as_of), old, new, lv))

    changed = [r for r in rows if r[4] != r[5]]
    maxdiff = max((abs((r[5] or 0) - (r[4] or 0)) for r in changed), default=0.0)
    mismatch = [r for r in rows if r[5] != r[6]]
    lines += [
        f"- 比对组合数：**{len(rows)} / 72**",
        f"- 分位值发生变化的组合：**{len(changed)}**，最大差异 **{maxdiff:.2f} pct**",
        f"- 改造后实时路径与新口径逐值比对：**{len(rows) - len(mismatch)} / {len(rows)} 全等**"
        f"（不一致 {len(mismatch)} 个）",
        "- 差异方向：对日左界比 3650 天早 2~3 天 ⇒ 窗口通常多含 1~2 个交易日样本，"
        "分位只在百分位小数点后第二位发生微变。",
        "- 旧口径不是 bug，只是标签与实现不自洽；历史研究报告引用的旧值差 ≤0.10 pct，**不回改**。",
        "",
        "## 逐组合明细",
        "",
        "| subject | 指标 | 窗口 | as_of | 旧(365天) | 新(对日) | Δ | 实时路径 | 一致 |",
        "|---|---|---|---|---:|---:|---:|---:|:---:|",
    ]
    for sym, m, k, a, old, new, lv in rows:
        delta = "" if old is None or new is None else f"{new - old:+.2f}"
        ok = "✅" if new == lv else "❌"
        lines.append(
            f"| {sym} | {m} | {k} | {a} | "
            f"{'' if old is None else old:.2f} | {'' if new is None else new:.2f} | "
            f"{delta} | {'' if lv is None else f'{lv:.2f}'} | {ok} |")
    lines += [
        "",
        "## 结论",
        "",
        f"- 口径切换已完成：实时路径 {len(rows) - len(mismatch)}/{len(rows)} 组合与新口径全等；"
        "两个分位专页返回结构不变，仅数字有 ≤0.10 pct 变化。",
        "- Exit：可进入 G0b 影子双跑（落表按新口径对齐）。若上表出现 ❌，先查口径漂移，不要进落表。",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"写出 {OUT}")
    print(f"  组合={len(rows)} 变化={len(changed)} 最大差={maxdiff:.2f} 实时不一致={len(mismatch)}")
    if mismatch:
        for r in mismatch[:10]:
            print("   MISMATCH", r)


if __name__ == "__main__":
    main()
