# -*- coding: utf-8 -*-
"""逻辑 / 证伪录入接口的端到端验证（TestClient，不起服务）。

用 **seq=9001/9002** 这类不可能出现的序号做测试，测完全部删掉，
不碰任何真实的 1-4 号条目——上一轮的教训是「测试写了真实迁移行，把用户数据覆盖了」。

覆盖：
1. 新建逻辑 -> 200 created
2. 行内改 state（只传 seq+state，模拟前端漏回填）-> content 被清空（证明全字段覆盖属实）
3. 正常改 state（回填全字段）-> content 保留（证明前端的回填是必需的）
4. seq > 4 -> 返回 warning 但仍成功
5. 新建证伪 -> 200（此前因 VmInvalidateSave 混入 id 字段，永远 422）
6. 证伪信号不可量化 -> quantified=false + warning
7. 删除被证伪引用的逻辑 -> unlinked_invalidates=1，且证伪 logic_id 变 NULL
8. 删除不存在 -> 404

用法：conda run -n fast311 python scripts/test_vm_logic_http.py
"""
import sys

sys.path.insert(0, r"C:\quant_man\mysite_quant\ams_trae")

from fastapi.testclient import TestClient
from server_fast.main import app

T_SEQ = 9001          # 逻辑测试序号
TI_SEQ = 9001         # 证伪测试序号

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


# 取一个真实标的（必须存在，否则 404）
lst = c.get("/api/v1/irs/value-monitors").json()
vm = lst["items"][0]
vm_id = vm["id"]
print(f"测试标的：{vm['symbol']} (id={vm_id})，测试 seq={T_SEQ}\n")


def cleanup():
    c.delete(f"/api/v1/irs/value-monitors/{vm_id}/invalidate/{TI_SEQ}")
    for s in (T_SEQ, 9002, 9101, 9102, 9103, 9104, 9105):
        c.delete(f"/api/v1/irs/value-monitors/{vm_id}/logic/{s}")


cleanup()  # 上一轮残留

print("[1] 逻辑：新建 + 全字段覆盖")
check("PUT /logic seq=9001 新建", c.put(f"/api/v1/irs/value-monitors/{vm_id}/logic", json={
    "seq": T_SEQ, "content": "贴水收敛 -> 收益归零 -> 已部分定价",
    "pricing": "部分", "state": "完好", "evidence": "报告第三章"}),
    show=lambda x: f"action={x['action']} warning={x['warning']}")

# 模拟「只传 seq+state」——前端若漏回填，content 是空串。
# 后端加了 content 非空校验，所以这里应该 400 而不是静默清空（双保险）。
check("PUT /logic 漏回填 content='' -> 400（不静默清空）",
      c.put(f"/api/v1/irs/value-monitors/{vm_id}/logic", json={
          "seq": T_SEQ, "content": "", "state": "减弱"}), expect=400)

rows = c.get(f"/api/v1/irs/value-monitors/{vm_id}/logic").json()
row = next((r for r in rows if r["seq"] == T_SEQ), None)
kept = row is not None and bool(row["content"]) and row["state"] == "完好"
print(("  OK  " if kept else "  FAIL") + " 被拒后原 content 与 state 均未变")
ok, fail = (ok + 1, fail) if kept else (ok, fail + 1)

# 正确姿势：回填全字段
check("PUT /logic 改 state + 回填", c.put(f"/api/v1/irs/value-monitors/{vm_id}/logic", json={
    "seq": T_SEQ, "content": "贴水收敛 -> 收益归零 -> 已部分定价",
    "pricing": "部分", "state": "减弱", "evidence": "报告第三章"}))
rows = c.get(f"/api/v1/irs/value-monitors/{vm_id}/logic").json()
row = next(r for r in rows if r["seq"] == T_SEQ)
good = row["content"] and row["state"] == "减弱" and row["state_at"]
print(("  OK  " if good else "  FAIL") + f" 回填后内容保留 state={row['state']} state_at={row['state_at']}")
ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
logic_id = row["id"]

print("\n[2] 条数软校验（>4 只警告不拦）")
# 判据是**条数**不是 seq（seq 会有空洞）。为了造出 >4 条，临时建 9101-9105 五条。
# 此时已有第 1 段建的 9001 一条，所以 9104 是第 5 条（首次触发），9105 是第 6 条。
created = []
for s in range(9101, 9106):
    r = c.put(f"/api/v1/irs/value-monitors/{vm_id}/logic",
              json={"seq": s, "content": f"临时逻辑 {s}", "state": "完好"})
    created.append((s, r.json().get("warning") if r.status_code == 200 else "HTTP%d" % r.status_code))
warned = [(s, w) for s, w in created if w]
first_ok = bool(warned) and warned[0][0] == 9104
print(("  OK  " if first_ok else "  FAIL") +
      f" 第 5 条首次触发 warning：{warned[0][1] if warned else '未触发'}")
ok, fail = (ok + 1, fail) if first_ok else (ok, fail + 1)
early_quiet = all(w is None for s, w in created if s <= 9103)
print(("  OK  " if early_quiet else "  FAIL") + " 前 4 条不误报")
ok, fail = (ok + 1, fail) if early_quiet else (ok, fail + 1)

print("\n[3] 证伪：此前必然 422 的保存")
d = check("PUT /invalidate 可量化", c.put(f"/api/v1/irs/value-monitors/{vm_id}/invalidate", json={
    "seq": TI_SEQ, "proposition": "IC 贴水会持续收敛", "signal": "年化贴水 < 2% 且持续 5 个交易日",
    "logic_id": logic_id, "source": "中金所日行情", "action": "减仓", "status": "未触发"}),
    show=lambda x: f"action={x['action']} quantified={x['quantified']}")
q = d and d.get("quantified") is True
print(("  OK  " if q else "  FAIL") + " 可量化判据 quantified=True")
ok, fail = (ok + 1, fail) if q else (ok, fail + 1)

d = check("PUT /invalidate 不可量化(不阻断)", c.put(f"/api/v1/irs/value-monitors/{vm_id}/invalidate", json={
    "seq": TI_SEQ, "proposition": "IC 贴水会持续收敛", "signal": "分位偏高",
    "logic_id": logic_id, "status": "未触发"}),
    show=lambda x: f"quantified={x['quantified']} warning={x['warning']}")
q = d and d.get("quantified") is False and d.get("warning")
print(("  OK  " if q else "  FAIL") + " 不可量化 quantified=False 且带 warning")
ok, fail = (ok + 1, fail) if q else (ok, fail + 1)

print("\n[4] 删除被引用的逻辑 -> 摘引用")
d = check("DELETE /logic/9001", c.delete(f"/api/v1/irs/value-monitors/{vm_id}/logic/{T_SEQ}"),
          show=lambda x: f"unlinked_invalidates={x['unlinked_invalidates']}")
n = d and d.get("unlinked_invalidates") == 1
print(("  OK  " if n else "  FAIL") + " 引用它的 1 条证伪被摘掉")
ok, fail = (ok + 1, fail) if n else (ok, fail + 1)

invs = c.get(f"/api/v1/irs/value-monitors/{vm_id}/invalidate").json()
inv = next((i for i in invs if i["seq"] == TI_SEQ), None)
clean = inv is not None and inv["logic_id"] is None
print(("  OK  " if clean else "  FAIL") + f" 证伪 logic_id 已置 NULL（{inv['logic_id'] if inv else '缺失'}）")
ok, fail = (ok + 1, fail) if clean else (ok, fail + 1)

print("\n[5] 删除不存在 / 清理")
check("DELETE /logic/9001 再删一次(404)", c.delete(f"/api/v1/irs/value-monitors/{vm_id}/logic/{T_SEQ}"),
      expect=404)
check("DELETE /invalidate/9001", c.delete(f"/api/v1/irs/value-monitors/{vm_id}/invalidate/{TI_SEQ}"))
check("DELETE /invalidate/9001 再删一次(404)",
      c.delete(f"/api/v1/irs/value-monitors/{vm_id}/invalidate/{TI_SEQ}"), expect=404)
for s in range(9101, 9106):
    c.delete(f"/api/v1/irs/value-monitors/{vm_id}/logic/{s}")
print("  已清理软校验测试行 9101-9105")

# 终态核对：测试行必须全部消失
rows = c.get(f"/api/v1/irs/value-monitors/{vm_id}/logic").json()
invs = c.get(f"/api/v1/irs/value-monitors/{vm_id}/invalidate").json()
test_seqs = {T_SEQ, 9002, 9101, 9102, 9103, 9104, 9105}
leftover = [r["seq"] for r in rows if r["seq"] in test_seqs] + \
           [i["seq"] for i in invs if i["seq"] == TI_SEQ]
print(("  OK  " if not leftover else "  FAIL") + f" 测试行已清空（残留 {leftover or '无'}）")
ok, fail = (ok + 1, fail) if not leftover else (ok, fail + 1)

print(f"\n通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
