# -*- coding: utf-8 -*-
"""G0b 影子双跑探针：落表最新行 vs 实时分位路径（新对日口径）逐值全等。

方案 §10 要求「连续 5 个交易日 72 键逐值全等」；本脚本把「连续 5 天」压缩成
**当前全量一次比对**，可在任意时点重复运行（含上线后每日复跑），
参照系是保留实时计算的两个 bds 分位专页（§6.4 B，天然影子通道）。

比对范围：6 subject × 4 指标 × 3 窗口 = 72 键，另比
`as_of` / 4 个 `status_*` / `window_cut` / `read_percentiles()` 契约。

用法：conda run -n fast311 python server_fast/scripts/probe_shadow_20261104.py
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, r"C:/quant_man/mysite_quant/ams_trae")

from server_fast.app.bds.services.valuation import (  # noqa: E402
    index_valuation, percentile_store as store, stock_valuation,
)
from server_fast.app.bds.services.valuation.percentile_stat import (  # noqa: E402
    PCT_METRICS, PCT_PREFIX, WINDOW_KEYS, window_cuts,
)


def norm(v):
    return None if v is None else round(float(v), 2)


def live_map():
    out = {}
    for m in PCT_METRICS:
        for it in index_valuation.get_percentiles(
                index_codes=["SHSE.000300", "SHSE.000905"], metric=m)["items"]:
            for k in WINDOW_KEYS:
                out[(it["index_code"], m, k)] = (it.get(k) or {}).get("pct")
    for m in PCT_METRICS:
        for it in stock_valuation.get_percentiles(
                ["SHSE.600036", "SHSE.600900", "SZSE.000333", "SZSE.300750"], metric=m)["items"]:
            ok = it.get("status") == "ok"
            for k in WINDOW_KEYS:
                out[(it["symbol"], m, k)] = (it.get(k) or {}).get("pct") if ok else None
    return out


def main():
    rows = store.read_latest_rows()
    live = live_map()
    print(f"读取侧命中 subject = {len(rows)}（应为 6），实时参照键 = {len(live)}（应为 72）")
    diffs, checked, status_diffs, cut_diffs = [], 0, [], []
    for (t, s), r in sorted(rows.items()):
        cuts = window_cuts(r["as_of"])
        wc = json.loads(r["window_cut"]) if isinstance(r["window_cut"], str) else (r["window_cut"] or {})
        for k, v in cuts.items():
            if wc.get(k) != v:
                cut_diffs.append((s, str(r["as_of"]), k, wc.get(k), v))
        for m, pre in zip(PCT_METRICS, ("pe", "pb", "ps", "dy")):
            st = r[f"status_{pre}"]
            for k in WINDOW_KEYS:
                col = f"{pre}_pct_{k.split('_')[1]}"
                got, want = norm(r.get(col)), norm(live.get((s, m, k)))
                checked += 1
                if got != want:
                    diffs.append((s, m, k, str(r["as_of"]), st, got, want))
            # status != ok 时该 metric 三个分位必须全 NULL
            if st != "ok":
                for k in WINDOW_KEYS:
                    if norm(r.get(f"{pre}_pct_{k.split('_')[1]}")) is not None:
                        status_diffs.append((s, m, str(r["as_of"]), st))

    print(f"\n[72 键逐值比对] 比对 {checked} 键，差异 {len(diffs)} 条")
    for d in diffs[:20]:
        print("   DIFF", d)
    print(f"[window_cut] 差异 {len(cut_diffs)} 条", cut_diffs[:5])
    print(f"[status 非 ok 但分位非空] {len(status_diffs)} 条", status_diffs[:5])
    print("\n[各 subject 最新行]")
    for (t, s), r in sorted(rows.items()):
        print(f"   {t:<6} {s:<12} as_of={r['as_of']} source={r['source_as_of']} "
              f"pe10y={r['pe_pct_10y']} pb10y={r['pb_pct_10y']} "
              f"ps10y={r['ps_pct_10y']} dy10y={r['dy_pct_10y']} "
              f"status={r['status_pe']}/{r['status_pb']}/{r['status_ps']}/{r['status_dy']}")
    contract = store.read_percentiles()
    print(f"\n[读取契约 percentiles()] 共 {len(contract)} 键（应为 12）：")
    for k, v in sorted(contract.items()):
        print(f"   {k[0]:<12} {k[1]:<8} {v}")

    ok = not diffs and not cut_diffs and not status_diffs and len(rows) == 6
    print("\n=== Exit ===")
    print("  G0b 影子全等：" + ("✅ 通过" if ok else "❌ 不通过，禁止切 G1"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
