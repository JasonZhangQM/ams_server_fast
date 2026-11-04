# -*- coding: utf-8 -*-
"""P2 分位落表哨兵探针（离线，不写库）。

覆盖方案 §10 哨兵 1/2/3/7/8 的计算侧部分：
1. 滑动窗口 vs 逐日精确 `stat_window`：pct/min/max/median/z/n 六字段逐值比对；
2. `as_of` 上界：历史行不含未来数据；
3. 窗口左界：`window_cut` 是日历对日，2/29 clamp 到 2/28；
7. 无效值日按 metric 独立 status（其余指标分位照常写值）；
8. 股息率单位是百分数、分位未取反。

以及 `build_rows` 全量组装的耗时与行数（决定 M2 是否能在 15 s 内跑完）。

用法：conda run -n fast311 python server_fast/scripts/probe_percentile_20261104.py
"""
from __future__ import annotations

import sys
import time
import bisect
from datetime import date, datetime, timedelta

sys.path.insert(0, r"C:/quant_man/mysite_quant/ams_trae")

from server_fast.app.bds.services.valuation import percentile_store as store  # noqa: E402
from server_fast.app.bds.services.valuation.percentile_stat import (  # noqa: E402
    PCT_METRICS, WINDOW_KEYS, WINDOW_YEARS, clean_value,
    daily_percentiles, shift_years, stat_window, window_cuts,
)
from server_fast.common.db import SessionLocal  # noqa: E402


def load_all():
    """6 subject × 4 指标的有效序列 + 原始行。"""
    out = {}
    with SessionLocal() as db:
        pairs = store.resolve_subject_types(db, list(store.L1_SUBJECTS))
        for t, s in pairs:
            out[(t, s)] = store.load_source(db, t, s)
    return out


def main():
    raws = load_all()
    total_points = 0
    diff = []
    t0 = time.perf_counter()
    for (t, s), raw in raws.items():
        for j, m in enumerate(PCT_METRICS, start=1):
            seq = [(r[0], float(r[j])) for r in raw if store._valid(t, m, r[j])]
            if not seq:
                continue
            daily = daily_percentiles(seq)
            n = len(seq)
            # 抽样：首 3 / 末 3 / 每 97 天一个（覆盖上市初期与近年）
            idx = sorted(set([0, 1, 2, n - 1, n - 2, n - 3] + list(range(0, n, 97))))
            for i in idx:
                if i < 0 or i >= n:
                    continue
                as_of, cur = seq[i]
                truncated = seq[:i + 1]
                for k in WINDOW_KEYS:
                    a = stat_window(truncated, cur, WINDOW_YEARS[k], as_of)
                    b = daily[as_of][k]
                    total_points += 1
                    for f in ("pct", "min", "max", "median", "z", "n"):
                        if a[f] != b[f]:
                            diff.append((t, s, m, k, str(as_of), f, a[f], b[f]))
    t_eq = time.perf_counter() - t0
    print(f"[1] 滑动窗口 vs 逐日 stat_window：比对 {total_points:,} 个点 × 6 字段，差异 {len(diff)} 条，耗时 {t_eq:.2f}s")
    for d in diff[:8]:
        print("     DIFF", d)

    # [2][3] as_of 上界与 window_cut
    print("\n[2] as_of 上界（600036 PE）：")
    raw = raws[("stock", "SHSE.600036")]
    seq = [(r[0], float(r[1])) for r in raw if clean_value("pe_ttm", r[1])]
    daily = daily_percentiles(seq)
    for a in (date(2025, 10, 23), seq[-1][0]):
        v = daily.get(a)
        if v:
            print(f"     as_of={a} 10y={v['pct_10y']['pct']}  不加 as_of 上界会退化成 {daily[seq[-1][0]]['pct_10y']['pct']}")
    print("\n[3] window_cut 日历对日：")
    for a in (date(2025, 10, 23), date(2016, 2, 29), date(2026, 3, 1)):
        print(f"     as_of={a} → {window_cuts(a)}")
    print(f"     2/29 clamp: shift_years(2016-02-29,10) = {shift_years(date(2016, 2, 29), 10)}")

    # [7][8] build_rows：行数、状态、单位
    print("\n[7][8] build_rows 全量组装：")
    t1 = time.perf_counter()
    total_rows = 0
    stat_counts = {}
    dy_sample = None
    loss_row = None
    end = None
    with SessionLocal() as db:
        end = store.source_end_date(db)
    for (t, s), raw in raws.items():
        rows = store.build_rows(db=None, subject_type=t, symbol=s, raw=raw,
                                source_as_of=end, calc_at=datetime.now(),
                                fp=store.fingerprint(raw))
        total_rows += len(rows)
        for r in rows:
            for m in PCT_METRICS:
                st = r[f"status_{store.PCT_PREFIX[m]}"]
                stat_counts[(s, m, st)] = stat_counts.get((s, m, st), 0) + 1
            if dy_sample is None and r["current_dy"] is not None:
                dy_sample = (s, str(r["as_of"]), r["current_dy"], r["dy_pct_10y"])
            if loss_row is None and r["status_dy"] == "loss" and r["pe_pct_10y"] is not None:
                loss_row = (s, str(r["as_of"]), r["status_dy"], r["pe_pct_10y"], r["dy_pct_10y"])
    t_build = time.perf_counter() - t1
    print(f"     6 subject 合计 {total_rows:,} 行，组装耗时 {t_build:.3f}s")
    print(f"     股息率原始值样本（应为百分数 4.x 而非 0.04x，且分位未取反）：{dy_sample}")
    print(f"     仅 dy 无效、PE 照常有分位的行：{loss_row}")
    print("     按 metric 状态计数（非 ok 的）：")
    for k, v in sorted(stat_counts.items()):
        if k[2] != "ok":
            print(f"       {k[0]} {k[1]} {k[2]} = {v}")

    print("\n=== 结论 ===")
    print(f"  等价性差异 = {len(diff)}（应为 0，若仅 z 差 0.01 按方案 §3.1 单独处理）")
    print(f"  全量组装 {total_rows:,} 行 {t_build:.3f}s（方案 §3.2 的 1.4s 估算只算了 pct，"
          f"本实现含 4 组 stat JSON 全字段；端到端预算 15 s）")
    return len(diff)


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)
