# -*- coding: utf-8 -*-
"""加仓点 × 风险预算（改造 4，2026-11-06；2026-11-16 随双防线口径改写）。

方案：docs/风险预算与加仓额度_系统改造方案_2026-11-05.md §改造 4、
docs/规则手册明细_2026-11-16.md §双防线

依赖方向 **irs → bills 单向**（🔒4.12）：预算的唯一实现在
`irs/service/budget.py`（档位锚定双防线），本模块只做两件事：

1. 「标的」→ 预算品种组 的导航（标的这一层只有 irs 知道）
2. 把余缺读数翻成一句人话结论

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


def _line_note(line: dict) -> str:
    """单道防线余缺 → 半句话：缺口（需入金）/ 剩余空间。"""
    gap = float(line.get("gap") or 0)
    if gap > 0:
        return (f"{line['label']}缺口 {gap:,.0f} 元"
                f"（需入金 {float(line.get('fund_needed') or 0):,.0f} 元）")
    return f"{line['label']}达标（剩余空间 {float(line.get('left') or 0):,.0f} 元）"


def feasibility(symbol: str, acct: Optional[str] = None) -> dict:
    """标的能不能加仓：逐品种组给两道防线余缺 + 卡在哪个约束。

    返回 `{symbol, scopes: [...], binding, verdict, note}`；
    `binding` 取最紧的一条（breach 时 = 需入金更大的那道防线）。
    2026-11-16 起不再算可开手数（用户定稿）：加仓点只回答「余/缺多少」。
    """
    from server_fast.app.irs.service import budget as tier_budget

    scopes = scopes_of(symbol)
    if not scopes:
        return {"symbol": symbol, "scopes": [], "binding": None, "verdict": None,
                "note": "该标的不对应任何保证金品种组（个股/海外指数无追保风险），不套预算"}

    blk = tier_budget.evaluate_live()
    rows = []
    for sc in scopes:
        res = tier_budget.scope_feasibility(sc, acct, block=blk)
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
                "note": "该标的对应品种组当前没有保证金持仓，无预算判定对象"}

    def _tightness(r: dict) -> tuple:
        """排序键：breach 最紧（需入金大者优先），over 次之，ok 最后（剩余空间小者优先）。"""
        v = r["verdict"]
        if v == "breach":
            lines = r["lines"]
            b = r.get("binding") or "r"
            return (0, -float(lines[b].get("fund_needed") or 0))
        if v == "over":
            return (1, 0.0)
        return (2, min(float(r["lines"]["r"].get("left") or 0),
                       float(r["lines"]["e"].get("left") or 0)))

    tight = min(found, key=_tightness)
    verdict = tight["verdict"]
    b = tight.get("binding")
    lines = tight["lines"]
    if verdict == "breach":
        line = lines.get(b) or lines["r"]
        note = (f"禁开新仓：{tight['account']} {_line_note(line)}"
                + ("；另一道防线亦破" if float(lines["r"]["gap"]) > 0 and float(lines["e"]["gap"]) > 0 else ""))
        binding_label = tier_budget.LINE_NAMES.get(b, b)
    elif verdict == "over":
        if tight.get("status") == "over" and tight.get("pct") is not None:
            note = (f"禁开新仓：{tight['account']} {tight['scope_name']}"
                    f"占净值 {tight['pct']:.1%} 超上限 {(tight['cap'] or 0):.0%}")
        else:
            note = f"禁开新仓：{tight['account']} 品种组超限"
        binding_label = tight["scope_name"]
    else:
        note = (f"可加仓：{tight['account']} {_line_note(lines['r'])}、{_line_note(lines['e'])}"
                f"；{tight['scope_name']}组剩余额度 {(tight.get('scope_headroom') or 0):,.0f} 元")
        binding_label = None

    return {
        "symbol": symbol,
        "scopes": rows,
        "binding": {"account": tight["account"], "scope": tight["scope"],
                    "binding": b, "binding_label": binding_label,
                    "verdict": verdict},
        "lines": lines,
        "verdict": verdict,
        "as_of": blk.get("as_of"),
        "note": note,
    }
