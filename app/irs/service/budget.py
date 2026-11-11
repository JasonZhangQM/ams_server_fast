# -*- coding: utf-8 -*-
"""风险预算与加仓额度（2026-11-06）。

方案：docs/风险预算与加仓额度_系统改造方案_2026-11-05.md（v2）

四档读数，判据取最严的一档：

    scope_headroom  = net(0)       × cap_scope        − margin_scope(0)
    gate_now        = net(0)       × (total−reserve)  − margin_all(0)
    gate_stress     = net(+stress) × (total−reserve)  − margin_all(+stress)
    gate_stress_max = net(+stress) × total            − margin_all(+stress)   # 仅展示，不参与取 min

`total` 与 `reserve` **不是连乘的两个系数**：`total` 是保证金占净值的上限，`reserve`
是其中只留给预设加仓点的那一块，故日常可开 = `total − reserve`（0.85−0.15=0.70）。

四条铁律（改动前先读方案 §二）：

1. 判据吃的是 **gate_stress**，不是 gate_now。用当前风险度判"能不能加仓"，等于
   "最便宜的时候最不敢买"——正是 2026-10-23 的错误。见方案 §八 哨兵 1。
2. 超限只**禁开**，不自动强减。M05 黄金占 72%，自动强减会在最不该动的时候逼人平仓；
   减仓归手册 R4 管，与 R2（不许开新的）分家。
3. 现货-only 账户跳过预算判定：M03 持仓率 100% 但没有追保风险，红标只会钝化告警。
4. 前端不得自行拼装额度：两次请求跨时点 = 10-23 错误的微缩版。irs 侧要额度，
   一律 `from server_fast.app.irs.service.budget import headroom_of`（🔒4.12 允许）。
5. 压力档 `budget_stress` 是**全局单值**（一次 run() 只能给全组一个跌幅），由
   `stress_drop(params)` 统一读取；压力情景一律经 `stress_run()` 生成并打标，
   `evaluate()` 校验跌幅。11-11 修复：此前 load_cfg 读了它却无人消费，是死参数。
"""
import math
import re
from typing import Optional

# 压力档默认值：方案 R1 原文「标的再跌 15% 后的保证金 + 追保缓冲」。
# ⚠️ 只是零配置 fallback；实际生效值一律读 `stress_drop(params)`（`irs_risk_param`
# 的 `('*','budget_stress')`），别再拿这个常量直接跑 run()。
STRESS_DROP = 0.15

# 零配置默认值：表里只存「偏离默认的账户」，M02 这种 100 元账户不用配任何行
DEFAULT_BUDGET = {
    # 账户级品种组上限（占该账户净值）
    "pct": {
        "index_fut": 0.40,
        "gold": 0.25,
        "opt_short": 0.25,
        "etf_short": 0.25,
        "equity": 0.60,
    },
    "total": 0.85,     # 单账户保证金占用上限
    "reserve": 0.15,   # 其中只留给预设加仓点的一块（故日常可开 70%）
    "stress": STRESS_DROP,
}

SCOPE_NAMES = {
    "index_fut": "股指期货",
    "gold": "黄金",
    "opt_short": "股指期权义务仓",
    "etf_short": "ETF期权义务仓",
    "equity": "现货",
    "other": "未归类",
}

# 期货品种 → 预算品种组（与 risk_scenario.FUT_UND_KEY 用途不同：那个给情景分组，这个给额度）
BUDGET_SCOPE = {
    "IC": "index_fut", "IF": "index_fut", "IH": "index_fut", "IM": "index_fut",
    "au": "gold",
}
# 期权义务仓按保证金公式族归组（option_market.margin_type_of），不按代码前缀猜
MTYPE_SCOPE = {"cffex": "opt_short", "etf": "etf_short"}
# shex（商品期权）一律并入其标的品种组 —— 否则黄金期货与黄金卖 put 各占一套预算，
# 等于把同一个风险记了两遍账（方案 §改造 1 的 🔴）

_PROD_RE = re.compile(r"^[A-Za-z]+")


def product_of(symbol: str) -> str:
    """从交易代码取品种码：'CFFEX.IC2612'→'IC'、'SHFE.au2612P840'→'au'。

    期货/期权是「交易所.代码」，股票是「代码.交易所」；只取含字母那段的前缀，
    两种情况都不会误取交易所名。
    """
    code = (symbol or "").split(".")[-1]
    m = _PROD_RE.match(code)
    return m.group(0) if m else ""


def scope_of(pos: dict) -> str:
    """单个持仓 → 预算品种组。现货走 equity。"""
    kind = pos.get("kind")
    if kind == "stock":
        return "equity"
    if kind in ("call", "put"):
        mtype = pos.get("mtype") or ""
        if mtype == "shex":  # 商品期权：并入标的品种组（au → gold）
            return BUDGET_SCOPE.get(product_of(pos.get("name", "")), "other")
        return MTYPE_SCOPE.get(mtype, "other")
    return BUDGET_SCOPE.get(product_of(pos.get("name", "")), "other")


def load_cfg(params: dict, acct: str) -> dict:
    """取该账户生效的预算参数：账户级 → 全局 `*` → 代码默认值。

    `params` 是 `risk_scenario._load_params()` 的 `{(scope, parameter): value}`。
    表里只存偏离默认的行；`scope` 既可以是账户名，也可以是 `*`（全局）。

    ⚠️ `budget_stress` 不在这里：压力情景是「全组同跌一次 run()」，只能有一个跌幅，
    属于全局参数，由 `stress_drop(params)` 单独读取（方案 §11.2）。
    """
    def pick(key: str, default: float) -> float:
        for scope in (acct, "*"):
            v = params.get((scope, key))
            if v is not None:
                return float(v)
        return default

    pct = {}
    for scope, cap in DEFAULT_BUDGET["pct"].items():
        pct[scope] = pick(f"budget_pct:{scope}", cap)
    return {
        "pct": pct,
        "total": pick("budget_total", DEFAULT_BUDGET["total"]),
        "reserve": pick("budget_reserve", DEFAULT_BUDGET["reserve"]),
    }


def stress_drop(params: dict) -> float:
    """生效压力档：全局单值，只读 `('*','budget_stress')`，缺省回代码默认。

    一次 `run(ctx,'sync',drop)` 只能给全组一个跌幅 ⇒ 该参数天然账户无关
    （方案 §11.2 明确 `scope='*'`）。表里的账户级行一律报错、不静默忽略：
    11-11 之前的 bug 正是「load_cfg 读了 budget_stress 却没有任何地方用它」。
    """
    bad = sorted({s for (s, k) in params if k == "budget_stress" and s != "*"})
    if bad:
        raise ValueError(
            f"budget_stress 只允许 scope='*'，发现账户级配置 {bad}；"
            "压力情景是全组一次 run()，不能按账户分别设"
        )
    v = float(params.get(("*", "budget_stress"), DEFAULT_BUDGET["stress"]))
    if not 0 < v < 1:
        raise ValueError(f"budget_stress 必须落在 (0, 1)，当前 {v}")
    return v


def _check_stress_run(run_stress: dict, eff: float) -> None:
    """哨兵：确保压力情景跌幅 == 生效配置，杜绝「参数读了不用」再次发生。"""
    got = run_stress.get("_stress_drop")
    if got is None:
        raise ValueError(
            "压力情景缺 _stress_drop 标记：必须经 budget.stress_run() 生成，"
            "禁止直接调 risk_scenario.run(ctx,'sync',常量)"
        )
    if abs(float(got) - eff) > 1e-9:
        raise ValueError(f"压力档跨口径：情景 {float(got):.2%} ≠ 生效配置 {eff:.2%}")


def stress_run(ctx: dict, params: dict, horizon: str = "now",
               iv_mult: float = 1.0) -> tuple[float, dict]:
    """按生效压力档跑一次 run() 并打标；所有预算调用点的唯一入口。"""
    from server_fast.app.irs.service import risk_scenario

    d = stress_drop(params)
    out = risk_scenario.run(ctx, "sync", d, horizon, iv_mult)
    out["_stress_drop"] = d
    return d, out


def _r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


def _r4(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 4)


def _per_lot(ctx: dict, run_now: dict) -> tuple[dict, dict]:
    """(acct, scope) → (当前保证金, 每手保证金)。

    每手保证金取该品种组内**单合约最大**值（保守口径）：
    同一品种组不同行权价的手续费率不同，取最大才不会把「够开一张虚值」误报成够开。
    """
    qty_of = {(p["acct"], p["name"]): max(int(p.get("qty") or 0), 1) for p in ctx["positions"]}
    margin_of: dict[tuple[str, str], float] = {}
    for d in run_now.get("detail", []):
        margin_of[(d["acct"], d["name"])] = margin_of.get((d["acct"], d["name"]), 0.0) + d["margin"]
    agg: dict[tuple[str, str], dict] = {}
    for p in ctx["positions"]:
        key = (p["acct"], p["name"])
        m = margin_of.get(key, 0.0)
        if m <= 0:
            continue
        scope = scope_of(p)
        slot = agg.setdefault((p["acct"], scope), {"margin": 0.0, "per_lot": 0.0})
        slot["margin"] += m
        slot["per_lot"] = max(slot["per_lot"], m / qty_of[key])
    return ({k: v["margin"] for k, v in agg.items()},
            {k: v["per_lot"] for k, v in agg.items()})


def evaluate(ctx: dict, params: dict, run_now: dict, run_stress: dict) -> dict:
    """在已算好的两次 run()（0% 与 +stress%）之上做后处理，不重算风险。

    `run_now` / `run_stress` 必须来自**同一次** `load_context()`，否则又跨时点。
    `run_stress` 必须经 `stress_run()` 生成（带 `_stress_drop` 标记），跌幅与
    `irs_risk_param` 的生效值不符时直接报错，不许静默用错的情景。
    """
    eff_stress = stress_drop(params)
    _check_stress_run(run_stress, eff_stress)
    scope_margin, scope_per_lot = _per_lot(ctx, run_now)
    accounts: dict[str, dict] = {}
    total_headroom = 0.0
    blocked: list[str] = []
    worst = "ok"
    judged = 0

    for acct, acc in ctx["accounts"].items():
        now = run_now["accounts"][acct]
        stress = run_stress["accounts"][acct]
        cfg = load_cfg(params, acct)
        net0 = float(now["net"])
        margin0 = float(now["margin"])
        net_s = float(stress["net"])
        margin_s = float(stress["margin"])
        book_net = float(acc["system_net"])

        # 品种组清单 = 有占用的组 ∪ 可开新仓的空组（现金账户不列 equity，见函数头铁律 3）
        scopes = {s for (a, s) in scope_margin if a == acct}
        scopes |= {s for s in cfg["pct"] if s != "equity"}
        by_scope = []
        for scope in sorted(scopes):
            m = scope_margin.get((acct, scope), 0.0)
            cap = cfg["pct"].get(scope)
            pct = (m / net0) if net0 > 0 else None
            per_lot = scope_per_lot.get((acct, scope)) or 0.0
            headroom = (net0 * cap - m) if cap is not None else None
            by_scope.append({
                "scope": scope,
                "name": SCOPE_NAMES.get(scope, scope),
                "margin": _r2(m),
                "pct": _r4(pct),
                "cap": cap,
                "headroom": _r2(headroom),
                "per_lot": _r2(per_lot),
                "lots": 0,          # 下面统一回填（依赖 gate 才能算）
                "status": ("unknown" if cap is None else
                           ("over" if (pct is not None and pct > cap + 1e-9) else "ok")),
            })

        gate_now = net0 * (cfg["total"] - cfg["reserve"]) - margin0
        gate_stress = net_s * (cfg["total"] - cfg["reserve"]) - margin_s
        gate_max = net_s * cfg["total"] - margin_s

        # 取 min 的候选：各品种组额度 + 两道总闸（gate_stress_max 只是「含加仓包的极限」，不参与判定）
        cands = [(s["headroom"], s["scope"]) for s in by_scope if s["headroom"] is not None]
        cands.append((gate_now, "total@now"))
        cands.append((gate_stress, "total@stress"))
        binding_v, binding = min(cands, key=lambda c: c[0])

        # 现货-only 账户跳过预算判定（无追保风险）
        is_idle = (margin0 <= 0 and abs(net0) < 1e-6)
        equity_only = (margin0 <= 0 and abs(net0) > 1e-6)
        if is_idle or equity_only:
            verdict = "skip"
        elif binding_v < 0:
            verdict = "block"
        elif any(s["status"] == "over" for s in by_scope):
            verdict = "over"
        else:
            verdict = "ok"

        for s in by_scope:
            if s["per_lot"] and s["headroom"] is not None:
                room = min(s["headroom"], gate_now, gate_stress)
                s["lots"] = max(int(math.floor(room / s["per_lot"])), 0) if room > 0 else 0

        lots = max([s["lots"] for s in by_scope] or [0])
        best = max(by_scope, key=lambda s: s["lots"]) if by_scope else None
        per_lot = ((best["per_lot"] or 0.0) if best and best["lots"] > 0
                   else max([s["per_lot"] or 0.0 for s in by_scope] or [0.0]))

        if verdict != "skip":
            judged += 1
            total_headroom += binding_v
            if verdict == "block":
                blocked.append(acct)
                worst = "block"
            elif verdict == "over" and worst != "block":
                worst = "over"

        accounts[acct] = {
            "net0": _r2(net0),
            "book_net": _r2(book_net),
            "book_gap": _r2(net0 - book_net),
            "margin": _r2(margin0),
            "total": {
                "cap": _r4(cfg["total"]),
                "reserve": _r4(cfg["reserve"]),
                "usable": _r4(cfg["total"] - cfg["reserve"]),
                "gate_now": _r2(gate_now),
                "gate_stress": _r2(gate_stress),
                "gate_stress_max": _r2(gate_max),
                "status": verdict,
            },
            "by_scope": by_scope,
            "headroom": _r2(binding_v),
            "binding": binding,
            "lots": lots,
            # 额度为正但不足一手（M04 差 219 元）也算「开不了」——金额为正会被误读成能开
            "openable": bool(lots > 0 and verdict != "block"),
            "per_lot_margin": _r2(per_lot) if per_lot else None,
            "verdict": verdict,
        }

    # 合约 → (账户, 品种组)：加仓点查额度直接查这张索引，不在前端/irs 侧重算归组
    symbols: dict[str, dict] = {}
    for p in ctx["positions"]:
        symbols.setdefault(p["name"], {"acct": p["acct"], "scope": scope_of(p)})

    return {
        "stress_drop": eff_stress,
        "mode": "sync",
        "accounts": accounts,
        "symbols": symbols,
        "summary": {
            "verdict": (worst if judged else "skip"),
            "judged_accounts": judged,
            "blocked_accounts": sorted(blocked),
            "headroom_total": _r2(total_headroom),
        },
    }


def evaluate_live() -> dict:
    """独立算一次预算（没有看板上下文时用：irs 加仓点、单点核查）。

    只跑 0% 与「生效压力档」两次 `run()`——不需要 grid，也不需要首个追保点二分。
    ⚠️ 与 `compute()` 走的是同一套 `load_context` / `run` / `evaluate`，
    口径不会漂移；但两次调用的行情时点可能不同，跨时点比较没有意义。
    """
    from server_fast.app.irs.service import risk_scenario

    ctx = risk_scenario.load_context()
    now = risk_scenario.run(ctx, "sync", 0.0)
    _, stress = stress_run(ctx, ctx["params"])
    out = evaluate(ctx, ctx["params"], now, stress)
    out["as_of"] = ctx["data_as_of"].isoformat() if ctx["data_as_of"] else None
    return out


def scope_feasibility(scope: str, acct: Optional[str] = None,
                      block: Optional[dict] = None) -> dict:
    """按品种组查额度（加仓点用）：剩余额度 / 可开手数 / 卡在哪个约束。

    `acct` 不传时取「该组有保证金占用的账户」；有多个则全列，不猜。
    """
    blk = block or evaluate_live()
    accounts = blk["accounts"]
    if acct is not None:
        targets = [acct] if acct in accounts else []
    else:
        targets = [a for a, c in accounts.items()
                   if any(s["scope"] == scope and s["margin"] > 0 for s in c["by_scope"])]
    out = {"scope": scope, "scope_name": SCOPE_NAMES.get(scope, scope),
           "as_of": blk.get("as_of"), "found": bool(targets), "accounts": []}
    for a in targets:
        card = accounts[a]
        row = next((s for s in card["by_scope"] if s["scope"] == scope), None)
        out["accounts"].append({
            "account": a,
            "verdict": card["verdict"],
            "binding": card["binding"],
            "headroom": card["headroom"],
            "gate_now": card["total"]["gate_now"],
            "gate_stress": card["total"]["gate_stress"],
            "scope_headroom": row["headroom"] if row else None,
            "pct": row["pct"] if row else None,
            "cap": row["cap"] if row else None,
            "lots": row["lots"] if row else 0,
            "per_lot_margin": row["per_lot"] if row else None,
        })
    if not targets:
        out["note"] = f"没有账户持有 {SCOPE_NAMES.get(scope, scope)} 的保证金持仓"
    return out


def headroom_of(symbol: str, acct: Optional[str] = None) -> dict:
    """单个合约的加仓可行性（给 irs 加仓点用）：额度 + 可开手数 + 卡在哪个约束。

    内部走一次 `compute()`，与看板同一个计算函数 ⇒ 额度与加仓点必同时点。
    `lots` = 该品种组还能开几手（按该组当前每手保证金）；新开一个「该组还没有的品种」
    时 per_lot 未知，此时 lots=0 并注明原因，不猜。
    """
    from server_fast.app.irs.service import risk_scenario

    ctx = risk_scenario.load_context()
    _, stress = stress_run(ctx, ctx["params"])
    blk = evaluate(ctx, ctx["params"], risk_scenario.run(ctx, "sync", 0.0), stress)
    hit = blk["symbols"].get(symbol)
    target = acct or (hit["acct"] if hit else None)
    card = blk["accounts"].get(target) if target else None
    as_of = ctx["data_as_of"].isoformat() if ctx["data_as_of"] else None
    base = {"symbol": symbol, "account": target, "as_of": as_of,
            "verdict": None, "lots": 0, "headroom": None, "binding": None,
            "scope": None, "scope_name": None, "scope_headroom": None,
            "gate_now": None, "gate_stress": None, "per_lot_margin": None}
    if card is None:
        return {**base, "found": False,
                "note": "该合约不在任何账户持仓中，且未指定账户"}
    if hit and acct and hit["acct"] != acct:
        return {**base, "found": False,
                "note": f"{symbol} 实际持有在 {hit['acct']}，与指定账户 {acct} 不一致"}

    scope = hit["scope"] if hit else None
    row = next((s for s in card["by_scope"] if s["scope"] == scope), None)
    return {
        **base,
        "found": True,
        "verdict": card["verdict"],
        "scope": row["scope"] if row else None,
        "scope_name": row["name"] if row else None,
        "scope_headroom": row["headroom"] if row else None,
        "headroom": card["headroom"],
        "binding": card["binding"],
        "gate_now": card["total"]["gate_now"],
        "gate_stress": card["total"]["gate_stress"],
        "lots": row["lots"] if row else 0,
        "per_lot_margin": row["per_lot"] if row else None,
        "note": ("" if row and row["per_lot"]
                 else "该品种组当前无在仓合约，无法由现有持仓反推每手保证金"),
    }
