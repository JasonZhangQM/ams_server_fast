# -*- coding: utf-8 -*-
"""探针：标的推演双锚点验收（2026-11-11）。

锚点 1：overrides={} 时 baseline_combo 与 GET /irs/risk-board 逐值一致；
锚点 2：单标的假设价=档位价时，subjects[sym] 与 level-scenario 同档逐值一致。
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

from server_fast.app.irs.service import um_simulate
from server_fast.common.db import SessionLocal

BASE = "http://127.0.0.1:8008/api/v1/irs"
TOL = 0.01


def close(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= TOL


db = SessionLocal()

# ---------- 锚点 1：0% 基准 == risk-board ----------
out0 = um_simulate.build(db, {})
bc = out0["baseline_combo"]
rb = requests.get(f"{BASE}/risk-board", params={"horizon": "now", "iv_mult": 1.0}, timeout=60).json()
print("锚点1 baseline_combo:", bc)
print("     risk-board      :", {"net": rb["net_total"], "margin": rb["margin_total"], "real": rb["real_total"]})
ok1 = (close(bc["net_at"], rb["net_total"]) and close(bc["margin_total"], rb["margin_total"])
       and close(bc["real"], rb["real_total"]))
print("锚点1 结果:", "PASS" if ok1 else "FAIL")

# ---------- 锚点 2：单标的档位价 == level-scenario 同档 ----------
row = db.execute(text(
    "SELECT id, symbol, price, pp_el, pp_l, pp_m, pp_h, pp_eh "
    "FROM irs_underlying_monitor WHERE symbol='SHSE.000905'"
)).mappings().first()
um_id, price, pp_m = row["id"], float(row["price"]), float(row["pp_m"])
print(f"\n000905 现价={price} pp_m={pp_m}")

out1 = um_simulate.build(db, {"SHSE.000905": pp_m})
sub = out1["subjects"]["SHSE.000905"]
ls = requests.get(f"{BASE}/underlying-monitors/{um_id}/level-scenario",
                  params={"horizon": "now", "iv_mult": 1.0}, timeout=120).json()
lv = ls["levels"]["m"]
print("锚点2 simulate subject:", {"delta_pnl": sub["delta_pnl"], "margin_at": sub["margin_at"],
                                   "combo_loss": out1["combo"]["loss"]})
print("     level-scenario    :", {"delta_pnl": lv["total"]["delta_pnl"],
                                   "margin_at": lv["total"]["margin_at"],
                                   "combo_loss": lv["combo"]["loss"]})
ok2 = (close(sub["delta_pnl"], lv["total"]["delta_pnl"])
       and close(sub["margin_at"], lv["total"]["margin_at"])
       and close(out1["combo"]["loss"], lv["combo"]["loss"]))
print("锚点2 结果:", "PASS" if ok2 else "FAIL")

# ---------- 附加：多标的并行 + zone_at / plans_at ----------
out2 = um_simulate.build(db, {"SHSE.000905": pp_m, "SHSE.600036": 44.0})
for sym, s in out2["subjects"].items():
    print(f"\n{sym}: 假设 {s['price_at']}（{s['pct'] * 100:.2f}%）zone {s['zone_now']}→{s['zone_at']}")
    print(f"  Δ盈亏 {s['delta_pnl']}  保证金Δ {s['margin_delta']}  到档浮盈 {s['pf_at_level']}")
    for p in s["plans_at"]:
        print(f"  剧本 seq{p['seq']} {p['trade_symbol']} {p['b_s']}{p['o_c'] or ''} "
              f"@ {p['effective_trigger']} → {'命中' if p['hit_at'] else '未到'}")
print("\n组合 combo:", out2["combo"])
print("预算 summary:", out2["budget"]["summary"])
print("warnings 条数:", len(out2["warnings"]), "| 逐仓行数:", len(out2["position_rows"]))
db.close()
print("\n总体:", "ALL PASS" if (ok1 and ok2) else "HAS FAIL")
