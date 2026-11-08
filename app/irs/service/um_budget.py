# -*- coding: utf-8 -*-
"""加仓点 × 风险预算（改造 4，2026-11-06）。

方案：docs/风险预算与加仓额度_系统改造方案_2026-11-05.md §改造 4

依赖方向 **irs → bills 单向**（🔒4.12）：额度的唯一实现在
`bills/services/budget.py`，本模块只做两件事：

1. 「标的」→ 预算品种组 的导航（标的这一层只有 irs 知道）
2. 把额度读数翻成一句人话结论

**不在这里算钱**。前端也不许自己拼装——两次请求跨时点 = 2026-10-23 错误的微缩版。
"""
from typing import Optional

# 标的 → 预算品种组（可多组：股指的期货与期权义务仓是两个组，取最严）
# ⚠️ 只登记「有保证金品种」的标的；个股/海外指数落到无保证金品种组，返回 found=False 不判
TARGET_SCOPE = {
    "SHSE.000300": ["index_fut", "opt_short"],
    "SHSE.000905": ["index_fut", "opt_short"],
    "SHSE.000016": ["index_fut", "opt_short"],
    "GC00Y": ["gold"],
    "XAU": ["gold"],
    "AU": ["gold"],
}

# ETF 标的（期权义务仓）：沪深两市的期权标的都是 51xxxx / 58xxxx / 159xxx
ETF_PREFIX = ("SHSE.51", "SHSE.58", "SZSE.159")


def scopes_of(symbol: str) -> list[str]:
    """标的代码 → 预算品种组清单；未登记返回空（不猜）。"""
    if symbol in TARGET_SCOPE:
        return TARGET_SCOPE[symbol]
    if any(symbol.startswith(p) for p in ETF_PREFIX):
        return ["etf_short"]
    return []


def feasibility(symbol: str, acct: Optional[str] = None) -> dict:
    """标的能不能加仓：逐品种组给额度 + 可开手数 + 卡在哪个约束。

    返回 `{symbol, scopes: [...], binding, verdict, note}`；
    `binding` 取各组里最严的一条（额度最小），因为「能不能加」由最紧的那条决定。
    """
    from server_fast.app.irs.service import budget as bills_budget

    scopes = scopes_of(symbol)
    if not scopes:
        return {"symbol": symbol, "scopes": [], "binding": None, "verdict": None,
                "note": "该标的不对应任何保证金品种组（个股/海外指数无追保风险），不套预算"}

    blk = bills_budget.evaluate_live()
    rows = []
    for sc in scopes:
        res = bills_budget.scope_feasibility(sc, acct, block=blk)
        if not res["found"]:
            rows.append({"scope": sc, "scope_name": res["scope_name"], "found": False,
                         "note": res.get("note", "")})
            continue
        for a in res["accounts"]:
            rows.append({"scope": sc, "scope_name": res["scope_name"], "found": True, **a})

    found = [r for r in rows if r.get("found")]
    if not found:
        return {"symbol": symbol, "scopes": rows, "binding": None, "verdict": None,
                "as_of": blk.get("as_of"),
                "note": "该标的对应品种组当前没有保证金持仓，也无从反推每手保证金"}

    tight = min(found, key=lambda r: (r["headroom"] if r["headroom"] is not None else 0))
    verdict = tight["verdict"]
    if verdict == "block":
        note = f"禁开新仓：{tight['account']} 压力口径缺口 {abs(tight['headroom']):,.0f} 元"
    elif tight["lots"] > 0:
        note = f"可开 {tight['lots']} 手（{tight['account']} · {tight['scope_name']}）"
    else:
        note = (f"开不了：{tight['scope_name']}额度 "
                f"{(tight['scope_headroom'] or 0):,.0f} 元不足一手"
                f"（每手 {(tight['per_lot_margin'] or 0):,.0f} 元）")
    return {
        "symbol": symbol,
        "scopes": rows,
        "binding": {"account": tight["account"], "scope": tight["scope"],
                    "headroom": tight["headroom"], "verdict": verdict},
        "lots": tight["lots"],
        "per_lot_margin": tight["per_lot_margin"],
        "verdict": verdict,
        "as_of": blk.get("as_of"),
        "note": note,
    }
