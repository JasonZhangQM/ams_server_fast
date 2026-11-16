# -*- coding: utf-8 -*-
"""风险预算：档位锚定双防线（2026-11-16 重写，整体替换旧 gate 体系）。

方案：docs/规则手册明细_2026-11-16.md §双防线

两道防线（情景引擎与 ② 档位压力表同源，**修正锚：情景价 = min(档位价, 现价)**；
2026-11-16 二次定稿：防线由「中档R/低档E」下调为「低档R/极低档E」，阈值默认 1）：

    低档防线    R = margin(到低档) ÷ net(到低档)      ≤ budget_tier_r（默认 1）
    极低档防线  E = margin(到极低档) ÷ eq(到极低档)   ≤ budget_tier_e（默认 1）

- 分母口径：R = 净值（不含融资，自有资金口径）；E = 客户权益（含融资，
  交易所追保口径）。极低档允许靠融资扛，但不能穿仓。
- 极低档缺失（pp_el 为空/非正）的标的：其项下持仓在本防线情景中**冻结**
  （保证金照计、不施跌幅）+ warnings 点名——防线只做得到数据支持的事，不静默放行。
- 修正锚：已跌破该档的标的按现价零跌幅计——防线语义是「从现在再跌到各自
  档位」；「跌破档位后继续下跌」不在覆盖范围。
- 输出只有**余缺**：gap = margin_情景 − cap × 分母_情景。gap>0 ⇒ 缺口
  （超限保证金 gap；需入金 = gap ÷ cap，入金后分母变大恰使风险度回到 cap）；
  gap≤0 ⇒ 剩余空间 = −gap（还能承载的保证金增量，未扣新开仓自身的情景亏损）。
  **不算可开手数**（2026-11-16 用户定稿）。
- 判据：任一道防线 gap>0 ⇒ verdict=breach（binding 记卡在哪道）；品种组
  超限 ⇒ over；现货-only / 空账户 ⇒ skip。**超限只禁开、不自动强减**（沿旧铁律）。
- 组合层（summary.lines）= **全组合口径**：分子分母含全部账户（skip 账户的权益
  也进分母，现货兜底杠杆账户；2026-11-16 用户定稿——账户之间资金可相互调配），
  与 ② 档位压力表组合行同式同源（build_levels 复用同一次情景 run，不重复计算）；
  verdict / breached_accounts 仍只看逐账户。
- 品种组额度（cap_scope）保留：管结构集中度，与两道防线正交。
- 参数：budget_tier_r / budget_tier_e，级联 账户 → `*` → 代码默认 1
  （2026-11-16 用户定稿：1 = 底线贴线，不再留 0.95 缓冲）。
  旧 total / reserve / gate_now / gate_stress / lots 参数与输出已整体下线。

四条铁律（改动前先读本文件头）：

1. 判据吃**到档情景**，不是现价风险度。用当前风险度判"能不能加仓"等于
   "最便宜的时候最不敢买"——正是 2026-10-23 的错误。
2. 超限只**禁开**，不自动强减。减仓归手册 R4 管，与 R2（不许开新的）分家。
3. 现货-only 账户跳过预算判定：M03 持仓率 100% 但没有追保风险，红标只会钝化告警。
4. 前端不得自行拼装余缺：两次请求跨时点 = 10-23 错误的微缩版。irs 侧要额度，
   一律 `from server_fast.app.irs.service.budget import evaluate_live`（🔒4.12 允许）。
"""
import re
from typing import Optional

from sqlalchemy import text

from server_fast.app.bills.constants import attachable_sql
from server_fast.app.irs.models import UnderlyingMonitor
from server_fast.common.db import SessionLocal

# 零配置默认值：表里只存「偏离默认的账户」，M02 这种 100 元账户不用配任何行。
# 阈值是**参数不是目标**：1.0 = 用户底线（贴线，2026-11-16 由 0.95 收紧判定余量口径改定）。
DEFAULT_BUDGET = {
    # 账户级品种组上限（占该账户净值）
    "pct": {
        "index_fut": 0.40,
        "gold": 0.25,
        "opt_short": 0.25,
        "etf_short": 0.25,
        "equity": 0.60,
    },
    "tier_r": 1.0,  # 低档防线：margin(低档)/net(低档) 上限
    "tier_e": 1.0,  # 极低档防线：margin(极低档)/eq(极低档) 上限
}

SCOPE_NAMES = {
    "index_fut": "股指期货",
    "gold": "黄金",
    "opt_short": "股指期权义务仓",
    "etf_short": "ETF期权义务仓",
    "equity": "现货",
    "other": "未归类",
}

LINE_NAMES = {"r": "低档防线(R)", "e": "极低档防线(E)"}

# 期货品种 → 预算品种组（与 risk_scenario.FUT_UND_KEY 用途不同：那个给情景分组，这个给额度）
BUDGET_SCOPE = {
    "IC": "index_fut", "IF": "index_fut", "IH": "index_fut", "IM": "index_fut",
    "au": "gold",
}
# 期权义务仓按保证金公式族归组（option_market.margin_type_of），不按代码前缀猜
MTYPE_SCOPE = {"cffex": "opt_short", "etf": "etf_short"}
# shex（商品期权）一律并入其标的品种组 —— 否则黄金期货与黄金卖 put 各占一套预算，
# 等于把同一个风险记了两遍账（旧方案 §改造 1 的 🔴）

# 防线键 → 档位字段：R 防线吃 pp_l（低档）、E 防线吃 pp_el（极低档）
TIER_FIELDS = {"r": "pp_l", "e": "pp_el"}

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

    `params` 是 `risk_scenario._load_params()` 的 `{(scope, key): value}`。
    表里只存偏离默认的行；`scope` 既可以是账户名，也可以是 `*`（全局）。
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
        "tier_r": pick("budget_tier_r", DEFAULT_BUDGET["tier_r"]),
        "tier_e": pick("budget_tier_e", DEFAULT_BUDGET["tier_e"]),
    }


def _check_cap(v: float, key: str) -> float:
    """防线阈值合法性：必须落在 (0, 1]。1.0 = 用户底线（贴线），>1 无意义。"""
    if not 0 < v <= 1:
        raise ValueError(f"{key} 必须落在 (0, 1]，当前 {v}")
    return v


# ---------------------------------------------------------------------------
# 到档情景构造：修正锚（情景价 = min(档位价, 现价)）+ 持仓归属 → shifts
# ---------------------------------------------------------------------------
def tier_shifts(ctx: dict, field: str, db=None) -> tuple[dict, list[str], dict]:
    """全监测标的的到档 shifts：{(账户, 合约): 跌幅}，供 run(shifts=...) 用。

    口径（与 um_level_scenario / um_simulate 完全同源）：
    - pct = min(档位价, 现价) / 现价 − 1（≤0）；shift = −pct = max(0, 1 − 档位价/现价)。
      已跌破该档的标的 shift=0（修正锚，2026-11-16 用户定稿）。
    - 持仓归属：bills_group_symbol 显式 underlying_symbol 优先、同代码自动命中
      （与 um_simulate._subject_by_symbol 同式）；只计 p_total ≠ 0 的在仓行。
    - 数据质量门槛（做不到就是假保证，绝不静默跳过）：
      * 标的现价无效 / 档位价无效 → 其项下持仓冻结（保证金照计）+ warnings 点名；
      * 未归属任何监测标的的在仓持仓 → 冻结 + warnings 汇总市值；
      * 同一合约归属多个标的 → 先命中者生效 + warning（与 um_simulate 同规则）。
    """
    if field not in TIER_FIELDS.values():
        raise ValueError(f"档位字段只支持 {sorted(TIER_FIELDS.values())}：{field}")
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        monitors = {}
        for m in db.query(UnderlyingMonitor).all():
            monitors[m.symbol] = {
                "name": m.name,
                "price": float(m.price or 0),
                "pp": float(getattr(m, field) or 0),
            }

        # 合约 → 归属标的（显式归属优先；只取在仓行）。同合约多标的命中在此点名。
        sql = text(f"""
            SELECT g.symbol AS csym, COALESCE(g.underlying_symbol, g.symbol) AS um_symbol
            FROM bills_group_symbol g
            JOIN irs_underlying_monitor m
              ON m.symbol = COALESCE(g.underlying_symbol, g.symbol)
            WHERE {attachable_sql('g')}
              AND g.p_total IS NOT NULL AND g.p_total <> 0
            ORDER BY g.symbol
        """)
        attr: dict[str, str] = {}
        seen: dict[str, set] = {}
        for r in db.execute(sql).mappings().all():
            attr.setdefault(r["csym"], r["um_symbol"])  # 先命中生效
            seen.setdefault(r["csym"], set()).add(r["um_symbol"])
        conflicts = sorted(c for c, ss in seen.items() if len(ss) > 1)

        shifts: dict[tuple, float] = {}
        frozen_invalid: list[str] = []   # 归属标的现价/档位无效 → 冻结
        anchored = 0                     # 已跌破该档、按现价零跌幅的持仓数
        applied = 0
        for p in ctx["positions"]:
            um_sym = attr.get(p.get("name"))
            if um_sym is None:
                continue  # 未归属，末尾统一报数
            mon = monitors.get(um_sym)
            if mon is None or mon["price"] <= 0 or mon["pp"] <= 0:
                frozen_invalid.append(f"{um_sym}({p.get('name')})")
                continue
            if mon["pp"] >= mon["price"] - 1e-9:
                # 修正锚：现价已跌破该档（含恰等）⇒ 情景价=现价，零跌幅，不反弹
                anchored += 1
                continue
            key = (p["acct"], p.get("name"))
            if key in shifts:
                continue
            # pp < price ⇒ shift ∈ (0,1)，正值 = 跌幅（run() 的 shifts 语义）
            shifts[key] = 1.0 - mon["pp"] / mon["price"]
            applied += 1

        # 未归属持仓：冻结 + 汇总市值（口径同 um_level_scenario._unmapped_in_position，
        # 这里用引擎持仓的近似名义值，仅作 warning 量级提示）
        unmapped_value = 0.0
        unmapped_n = 0
        for p in ctx["positions"]:
            if attr.get(p.get("name")) is not None:
                continue
            unmapped_n += 1
            if p["kind"] in ("call", "put"):
                unmapped_value += abs(float(p.get("sys_value") or 0))
            else:
                unmapped_value += abs(float(p.get("px") or 0) * p.get("mult", 1) * p.get("qty", 0))

        warns: list[str] = []
        if conflicts:
            warns.append(f"合约同时归属多个标的（取先命中）：{', '.join(conflicts[:10])}")
        if frozen_invalid:
            warns.append(
                f"{'、'.join(sorted(set(frozen_invalid))[:10])} 的现价或 {field} 档位价无效，"
                "其项下持仓在本情景中冻结（保证金照计），不参与跌幅施加。")
        if anchored:
            warns.append(
                f"{anchored} 条持仓的归属标的现价已低于该档位，按修正锚以现价计（零跌幅）："
                "情景价 = min(档位价, 现价)。")
        if unmapped_n:
            warns.append(
                f"另有 {unmapped_n} 条在仓合约未归属任何监测标的（名义约 {unmapped_value:,.0f}），"
                "冻结（保证金照计）；防线只覆盖监测标的体系内的风险。")

        meta = {"field": field, "applied": applied, "anchored": anchored,
                "frozen_invalid": len(frozen_invalid), "unmapped": unmapped_n,
                "unmapped_value": round(unmapped_value, 2)}
        return shifts, warns, meta
    finally:
        if own_db:
            db.close()


def tier_run(ctx: dict, field: str, horizon: str = "now", iv_mult: float = 1.0,
             db=None) -> dict:
    """按档位字段跑一次到档情景并打标；所有防线情景的唯一入口。

    打标 `_tier` 供 evaluate() 校验，杜绝「情景传错档」——与旧 `_stress_drop`
    哨兵同构。horizon 恒 now（预算只看即期口径）。
    """
    from server_fast.app.irs.service import risk_scenario

    shifts, warns, meta = tier_shifts(ctx, field, db)
    out = risk_scenario.run(ctx, "sync", 0.0, horizon=horizon, iv_mult=iv_mult,
                            shifts=shifts)
    out["_tier"] = field
    out["_tier_warnings"] = warns
    out["_tier_meta"] = meta
    return out


def _r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


def _r4(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 4)


def _line(label: str, denom: str, cap: float, margin_at: float,
          denom_at: float) -> dict:
    """单道防线读数。gap 线性可加：Σ账户 gap = 组合 gap（cap 同值时）。"""
    ratio = (margin_at / denom_at) if denom_at > 0 else None  # 分母非正 = 穿仓，不编造
    gap = margin_at - cap * denom_at
    return {
        "label": label,
        "denom": denom,  # net（R 防线）/ eq（E 防线）
        "cap": _r4(cap),
        "margin_at": _r2(margin_at),
        "denom_at": _r2(denom_at),
        "ratio": _r4(ratio),
        "gap": _r2(gap),
        # 需入金：入金 X 后分母变大，恰使风险度回到 cap（X = gap/cap）
        "fund_needed": _r2(gap / cap) if gap > 0 else 0.0,
        # 剩余空间：还能承载的保证金增量（未扣新开仓自身的情景亏损）
        "left": _r2(-gap) if gap <= 0 else 0.0,
    }


def _scope_margin_by_acct(ctx: dict, run_now: dict) -> dict[tuple, float]:
    """(账户, 品种组) → 当前保证金占用（0% 基准）。品种组口径归 budget.scope_of 管。"""
    by_pos: dict[tuple, float] = {}
    for d in run_now.get("detail", []):
        key = (d["acct"], d["name"])
        by_pos[key] = by_pos.get(key, 0.0) + float(d["margin"])
    out: dict[tuple, float] = {}
    for p in ctx["positions"]:
        m = by_pos.get((p["acct"], p["name"]), 0.0)
        if m > 0:
            k = (p["acct"], scope_of(p))
            out[k] = out.get(k, 0.0) + m
    return out


def evaluate(ctx: dict, params: dict, run_now: dict, run_r: dict,
             run_e: dict) -> dict:
    """在已算好的三次 run()（0%、到低档、到极低档）之上做后处理，不重算风险。

    `run_r` / `run_e` 必须带 `_tier` 标记：tier_run() 生成，或 build_levels()
    复用压力表 l/el 两档情景 run 后打标（两条路径同式同锚定、实测逐分相等）。
    档位与防线一一对应：pp_l→r、pp_el→e，传错直接报错（哨兵同构于旧 _stress_drop）。
    三次 run 必须来自**同一次** `load_context()`，否则又跨时点。
    """
    if run_r.get("_tier") != "pp_l" or run_e.get("_tier") != "pp_el":
        raise ValueError(
            "低/极低档情景必须经 budget.tier_run('pp_l'/'pp_el') 生成并打标，"
            f"收到 r={run_r.get('_tier')} e={run_e.get('_tier')}")
    cfg_star = load_cfg(params, "*")
    _check_cap(cfg_star["tier_r"], "budget_tier_r")
    _check_cap(cfg_star["tier_e"], "budget_tier_e")
    scope_margin = _scope_margin_by_acct(ctx, run_now)
    accounts: dict[str, dict] = {}
    breached: list[str] = []
    worst = "ok"
    judged = 0
    total_margin = {"r": 0.0, "e": 0.0}
    total_denom = {"r": 0.0, "e": 0.0}

    for acct, acc in ctx["accounts"].items():
        now = run_now["accounts"][acct]
        r_at = run_r["accounts"][acct]
        e_at = run_e["accounts"][acct]
        cfg = load_cfg(params, acct)
        _check_cap(cfg["tier_r"], f"budget_tier_r({acct})")
        _check_cap(cfg["tier_e"], f"budget_tier_e({acct})")
        net0 = float(now["net"])
        margin0 = float(now["margin"])
        book_net = float(acc["system_net"])

        line_r = _line("低档", "net", cfg["tier_r"], float(r_at["margin"]), float(r_at["net"]))
        line_e = _line("极低档", "eq", cfg["tier_e"], float(e_at["margin"]), float(e_at["eq"]))

        # 品种组清单 = 有占用的组 ∪ 可开新仓的空组（现金账户不列 equity，沿旧铁律 3）
        scopes = {s for (a, s) in scope_margin if a == acct}
        scopes |= {s for s in cfg["pct"] if s != "equity"}
        by_scope = []
        for scope in sorted(scopes):
            m = scope_margin.get((acct, scope), 0.0)
            cap = cfg["pct"].get(scope)
            pct = (m / net0) if net0 > 0 else None
            headroom = (net0 * cap - m) if cap is not None else None
            by_scope.append({
                "scope": scope,
                "name": SCOPE_NAMES.get(scope, scope),
                "margin": _r2(m),
                "pct": _r4(pct),
                "cap": _r4(cap),
                "headroom": _r2(headroom),
                "status": ("unknown" if cap is None else
                           ("over" if (pct is not None and pct > cap + 1e-9) else "ok")),
            })

        # 现货-only 账户跳过预算判定（无追保风险，沿旧铁律 3）
        is_idle = (margin0 <= 0 and abs(net0) < 1e-6)
        equity_only = (margin0 <= 0 and abs(net0) > 1e-6)
        if is_idle or equity_only:
            verdict = "skip"
            binding = None
        elif line_r["gap"] > 0 or line_e["gap"] > 0:
            verdict = "breach"
            # 两道都破 → 卡在需入金更大（更紧）的那道
            binding = ("r" if float(line_r["fund_needed"] or 0)
                       >= float(line_e["fund_needed"] or 0) else "e")
        elif any(s["status"] == "over" for s in by_scope):
            verdict = "over"
            binding = next(s["scope"] for s in by_scope if s["status"] == "over")
        else:
            verdict = "ok"
            binding = None

        # 组合层余缺 = **全组合口径**（2026-11-16 用户定稿：账户之间资金可相互调配）：
        # 分子分母都含 skip 账户——skip 账户无保证金持仓（分子贡献 0），权益进分母，
        # 现货权益为杠杆账户兜底。逐账户判定（verdict / breached）不受影响。
        total_margin["r"] += float(r_at["margin"])
        total_denom["r"] += float(r_at["net"])
        total_margin["e"] += float(e_at["margin"])
        total_denom["e"] += float(e_at["eq"])
        if verdict != "skip":
            judged += 1
            if verdict == "breach":
                breached.append(acct)
                worst = "breach"
            elif verdict == "over" and worst != "breach":
                worst = "over"

        accounts[acct] = {
            "net0": _r2(net0),
            "book_net": _r2(book_net),
            "book_gap": _r2(net0 - book_net),
            "margin": _r2(margin0),
            "lines": {"r": line_r, "e": line_e},
            "by_scope": by_scope,
            "binding": binding,
            "verdict": verdict,
        }

    # 组合层余缺：全组合口径（分母含全部账户），与 ② 档位压力表组合行完全同式；
    # build_levels 里预算直接复用压力表 l/el 两档的同一次情景 run，两处天然逐分相等。
    # 阈值取全局 `*`（账户级差异化阈值时组合层只能给一份，以全局值为准并如实标注 cap）。
    summary_lines = {
        "r": _line("低档", "net", cfg_star["tier_r"],
                   total_margin["r"], total_denom["r"]),
        "e": _line("极低档", "eq", cfg_star["tier_e"],
                   total_margin["e"], total_denom["e"]),
    }

    # 合约 → (账户, 品种组)：加仓点查额度直接查这张索引，不在前端/irs 侧重算归组
    symbols: dict[str, dict] = {}
    for p in ctx["positions"]:
        symbols.setdefault(p["name"], {"acct": p["acct"], "scope": scope_of(p)})

    return {
        "engine": "tier_v1",
        "anchor": "min(档位价, 现价)",
        "thresholds": {"r": _r4(cfg_star["tier_r"]), "e": _r4(cfg_star["tier_e"])},
        "accounts": accounts,
        "symbols": symbols,
        "summary": {
            "verdict": (worst if judged else "skip"),
            "judged_accounts": judged,
            "breached_accounts": sorted(breached),
            "lines": summary_lines,
        },
    }


def evaluate_live() -> dict:
    """独立算一次预算（没有看板上下文时用：irs 加仓点、单点核查）。

    只跑 0% 与「到低档 / 到极低档」三次 `run()`——不需要 grid，也不需要首个追保点
    二分。⚠️ 与 `compute()` 走的是同一套 `load_context` / `run` / `evaluate`，
    口径不会漂移；但两次调用的行情时点可能不同，跨时点比较没有意义。
    """
    from server_fast.app.irs.service import risk_scenario

    ctx = risk_scenario.load_context()
    now = risk_scenario.run(ctx, "sync", 0.0)
    r_run = tier_run(ctx, "pp_l")
    e_run = tier_run(ctx, "pp_el")
    out = evaluate(ctx, ctx["params"], now, r_run, e_run)
    tier_warns = (r_run.get("_tier_warnings") or []) + (e_run.get("_tier_warnings") or [])
    if tier_warns:
        out["warnings"] = tier_warns
    out["as_of"] = ctx["data_as_of"].isoformat() if ctx["data_as_of"] else None
    return out


def scope_feasibility(scope: str, acct: Optional[str] = None,
                      block: Optional[dict] = None) -> dict:
    """按品种组查余缺（加仓点用）：两道防线余缺 + 卡在哪个约束。

    `acct` 不传时取「该组有保证金占用的账户」；有多个则全列，不猜。
    """
    blk = block or evaluate_live()
    accounts = blk["accounts"]
    if acct is not None:
        targets = [acct] if acct in accounts else []
    else:
        targets = sorted(a for a, c in accounts.items()
                         if any(s["scope"] == scope and float(s["margin"] or 0) > 0
                                for s in c["by_scope"]))
    out = {"scope": scope, "scope_name": SCOPE_NAMES.get(scope, scope),
           "as_of": blk.get("as_of"), "found": bool(targets), "accounts": []}
    for a in targets:
        card = accounts[a]
        row = next((s for s in card["by_scope"] if s["scope"] == scope), None)
        out["accounts"].append({
            "account": a,
            "verdict": card["verdict"],
            "binding": card["binding"],
            "lines": card["lines"],
            "scope_headroom": row["headroom"] if row else None,
            "pct": row["pct"] if row else None,
            "cap": row["cap"] if row else None,
            "status": row["status"] if row else None,
        })
    if not targets:
        out["note"] = f"没有账户持有 {SCOPE_NAMES.get(scope, scope)} 的保证金持仓"
    return out
