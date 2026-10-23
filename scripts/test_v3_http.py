# -*- coding: utf-8 -*-
"""v3 新端点 HTTP 冒烟（TestClient，不起服务）。

只做只读 + 一个"写回同值"的幂等 PATCH；不新建/不删除任何业务数据。
用法：conda run -n fast311 python scripts/test_v3_http.py
"""
import sys
sys.path.insert(0, r"C:\quant_man\mysite_quant\ams_trae")

from fastapi.testclient import TestClient
from server_fast.main import app

c = TestClient(app)
ok = fail = 0


def check(name, resp, expect=200, show=None):
    global ok, fail
    good = resp.status_code == expect
    print(("  OK  " if good else "  FAIL") + f" {name} -> {resp.status_code} (期望 {expect})")
    if not good:
        fail += 1
        print("         ", str(resp.text)[:300])
        return None
    ok += 1
    data = resp.json()
    if show:
        print("         ", show(data))
    return data


print("[1] irs 列表筛选")
d = check("GET /irs/value-monitors", c.get("/api/v1/irs/value-monitors"),
          show=lambda x: f"total={x['total']} 首行={x['items'][0]['symbol'] if x['items'] else '-'}")
first = d["items"][0] if d and d["items"] else None

d2 = check("GET ?theme=黄金", c.get("/api/v1/irs/value-monitors", params={"theme": "黄金"}),
           show=lambda x: f"total={x['total']} -> {[i['symbol'] for i in x['items']]}")
check("GET ?theme=黄金,IC贴水(多值)", c.get("/api/v1/irs/value-monitors",
                                          params=[("theme", "黄金"), ("theme", "IC贴水")]),
      show=lambda x: f"total={x['total']}")
check("GET ?research_state=none", c.get("/api/v1/irs/value-monitors", params={"research_state": "none"}),
      show=lambda x: f"total={x['total']}")
check("GET ?research_state=due", c.get("/api/v1/irs/value-monitors", params={"research_state": "due"}),
      show=lambda x: f"total={x['total']}")
check("GET ?research_state=bad(422)", c.get("/api/v1/irs/value-monitors", params={"research_state": "xx"}),
      expect=422)

print("[2] 详情抽屉")
if first:
    vid = first["id"]
    d = check(f"GET /value-monitors/{vid}/detail", c.get(f"/api/v1/irs/value-monitors/{vid}/detail"),
              show=lambda x: (f"symbol={x['vm']['symbol']} versions={len(x['versions'])} "
                              f"research={len(x['research'])} plans={len(x['plans'])} "
                              f"groups={len(x['groups'])} trades={len(x['trades'])} "
                              f"logic={len(x['logic'])} invalidate={len(x['invalidate'])}"))
    if d:
        # trades 一行 = 一笔成交：id 不许重复
        ids = [t["id"] for t in d["trades"]]
        dup = len(ids) != len(set(ids))
        print(f"  {'FAIL' if dup else '  OK'} trades 去重：{len(ids)} 行 / {len(set(ids))} 唯一")
        global_ok = not dup
        if dup:
            fail += 1

check("GET 不存在的 detail(404)", c.get("/api/v1/irs/value-monitors/999999/detail"), expect=404)
check("GET /value-monitors/coverage", c.get("/api/v1/irs/value-monitors/coverage"),
      show=lambda x: f"{len(x['items'])} 个标的, total_bills={x['total_bills']}")
check("GET /value-monitors/unmapped", c.get("/api/v1/irs/value-monitors/unmapped", params={"limit": 5}),
      show=lambda x: f"count={x['count']}")

print("[3] bills 归属")
check("GET /bills/group-symbols/unmapped-count", c.get("/api/v1/bills/group-symbols/unmapped-count"),
      show=lambda x: str(x))
check("GET ?mapped=no", c.get("/api/v1/bills/group-symbols", params={"mapped": "no", "limit": 3}),
      show=lambda x: f"total={x['total']}")
check("GET ?mapped=yes", c.get("/api/v1/bills/group-symbols", params={"mapped": "yes", "limit": 3}),
      show=lambda x: f"total={x['total']}")

gs = c.get("/api/v1/bills/group-symbols", params={"limit": 300}).json()["items"]
mapped = next((g for g in gs if g["vm_symbol"]), None)
# 资金流水行一律不可归属：RZRQ / CRJ / SYFY / cash 全部应 400
for _sym in ("RZRQ", "CRJ", "SYFY", "cash"):
    _row = next((g for g in gs if g["symbol"] == _sym), None)
    if _row:
        check(f"PATCH {_sym} 行设 vm(400)",
              c.patch(f"/api/v1/bills/group-symbols/{_row['id']}/vm",
                      json={"vm_symbol": "GC00Y"}), expect=400)
_licai = next((g for g in gs if g["category"] == "理财"), None)
if _licai:
    check("PATCH 理财行设 vm(400)",
          c.patch(f"/api/v1/bills/group-symbols/{_licai['id']}/vm",
                  json={"vm_symbol": "GC00Y"}), expect=400)
if mapped:
    # 写回同一个值：幂等，不改变任何业务状态
    check(f"PATCH 幂等写回 {mapped['symbol']}={mapped['vm_symbol']}",
          c.patch(f"/api/v1/bills/group-symbols/{mapped['id']}/vm",
                  json={"vm_symbol": mapped["vm_symbol"]}),
          show=lambda x: str(x))
check("PATCH 不存在的标的(400)", c.patch(f"/api/v1/bills/group-symbols/{mapped['id']}/vm",
                                        json={"vm_symbol": "NOPE"}), expect=400 if mapped else 200)

print("[4] 账单笔记 / 无计划成交")
bl = c.get("/api/v1/bills/bills", params={"limit": 3}).json()["items"]
b0 = bl[0] if bl else None
if b0:
    check(f"GET /bills/{b0['id']}/note", c.get(f"/api/v1/bills/{b0['id']}/note"),
          show=lambda x: f"{len(x)} 条：{[n['kind'] for n in x]}")
    print("         行字段：", sorted(bl[0].keys())[-6:])
check("GET /bills/unplanned", c.get("/api/v1/bills/unplanned", params={"limit": 3}),
      show=lambda x: f"count={x['count']}")

print("[5] 旧接口应已下线")
check("GET /bills/group-symbols/themes(404)", c.get("/api/v1/bills/group-symbols/themes"), expect=404)
check("GET ?has_note=true(忽略/200)", c.get("/api/v1/bills/group-symbols", params={"has_note": "true"}))

print("=" * 60)
print(f"通过 {ok} 项，失败 {fail} 项")
