# -*- coding: utf-8 -*-
"""只读：确认 bills/irs 路由可导入 + 四态计数 + trades_of 不再重复行"""
import sys
sys.path.insert(0, r"C:\quant_man\mysite_quant\ams_trae")

from sqlalchemy import text
from server_fast.common.db import SessionLocal

db = SessionLocal()

# 1) 四态计数（与 bills router 同口径）
from server_fast.app.irs.service import vm_map
print("[1] vm_map.unmapped_count:", vm_map.unmapped_count(db))

# 2) trades_of 一行 = 一笔成交
for sym in ("GC00Y",):
    rows = vm_map.trades_of(db, sym, limit=50)
    ids = [r["id"] for r in rows]
    print(f"[2] trades_of({sym}) 行数={len(rows)} 去重后={len(set(ids))} 重复={len(ids) != len(set(ids))}")
    for r in rows[:3]:
        print("    ", r["id"], r["trade_time"], r["symbol"], "想法:", (r["note_idea"] or "")[:12],
              "复盘:", (r["note_review"] or "")[:12])

# 3) 路由可导入 + 关键端点签名（注意 router 上挂的是相对前缀路径，/api/v1 在 app 层加）
from server_fast.app.bills import router as br
from server_fast.app.irs import router as ir
print("[3] bills router ok, TRADE_CATS =", br.TRADE_CATS)
print("    irs router ok")
bpaths = {r.path for r in br.router.routes}
ipaths = {r.path for r in ir.router.routes}
for p in ("/bills/group-symbols/unmapped-count", "/bills/group-symbols/{id_}/vm",
          "/bills/unplanned", "/bills/{id_}/note", "/bills/{id_}/note/{kind}"):
    print(f"    bills {p}: {'OK' if p in bpaths else 'MISSING'}")
for p in ("/irs/value-monitors/coverage", "/irs/value-monitors/{id_}/detail",
          "/irs/value-monitors/{id_}/trades",
          "/irs/value-monitors/{id_}/versions", "/irs/value-monitors/{id_}/research",
          "/irs/value-monitors/{id_}/plans", "/irs/value-monitors/{id_}/logic",
          "/irs/value-monitors/{id_}/invalidate"):
    print(f"    irs   {p}: {'OK' if p in ipaths else 'MISSING'}")

# 4) 三态抽样：确认融资融券行 vm_symbol 为 NULL
n = db.execute(text("SELECT COUNT(*) FROM bills_group_symbol "
                    "WHERE category='融资融券' AND vm_symbol IS NOT NULL")).scalar()
print("[4] 融资融券带 vm_symbol 的行数（应为 0）:", n)

db.close()
print("DONE")
