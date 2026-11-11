# -*- coding: utf-8 -*-
"""推演工作台（POST /irs/simulate）：多标的假设价 → 组合风险/收益联动推演。

与 um_level_scenario（单标的五档情景）同源不同用：
- 那边：固定五档阶梯、单标的驱动，抽屉里常驻展示；
- 这里：**每标的任意假设价**（前端滑块），多标的并行改动，一次 run 出组合结果。

口径（与 level-scenario 完全一致，均在响应 warnings 原样回给前端）：
1. pct = 假设价 / 监测标的主表 price − 1；shift = −pct。
2. 单标的驱动：只对该标的项下持仓施加 shift，其余持仓 shift=0（价格冻结，
   但保证金仍按基准价计入账户分母）。
3. 持仓按同百分比平移（beta=1 近似），跨市场基差/贴水/折溢价漂移不计。
4. 期权即期 BS 重估、到期内在价值双时点（horizon 决定摘要主口径）；
   逐仓明细与分账户表固定同时给即期/到期两列 Δ 盈亏；义务仓保证金逐档重算。
5. 分账户风险指标复用 level-scenario 的 _account_rows（净值/权益/保证金/双口径风险度），
   Δ 统计账户全部持仓以便与 combo.loss 对账；账户当前盈亏取 bills_group_acc 账面值。
6. 纯现算：不写库、不建任务。

可查可核的两条锚点（探针 scripts/_probe_um_simulate.py 验收，不改运行时行为）：
- 锚点 1：overrides={} 时 baseline_combo 与 GET /irs/risk-board 同口径一致；
- 锚点 2：单标的假设价=某档位价时，subjects[sym] 与 level-scenario 同档 combo 一致。

预算读数（budget.evaluate）：恒为**现价基准**（0% 与 +15% 全组同跌、horizon=now），
不随滑块假设价变化——run() 的 shifts 与全组 drop 语义互斥，组合应力留待 M3。

依赖方向（🔒4.12）：irs 内部复用；bills 只取 constants 的归属白名单片段，
跨模块取数一律用表名字符串 SQL，不 import bills 模型（避免循环导入）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.app.bills.constants import attachable_sql
from server_fast.app.irs.models import UnderlyingMonitor
from server_fast.app.irs.service import budget as budget_mod
from server_fast.app.irs.service import risk_scenario
from server_fast.app.irs.service import um_level_scenario as lvl
from server_fast.app.irs.service import um_today, um_version
from server_fast.app.irs.service.budget import STRESS_DROP
from server_fast.app.irs.service.um_plan import effective_trigger

HORIZONS = ("now", "expiry")


def _subject_by_symbol(db: Session) -> dict[str, tuple[str | None, str | None]]:
    """合约 → 归属标的（代码, 名称）；与 um_level_scenario._owned_rows 同口径。

    显式归属 underlying_symbol 优先，代码本身即标的时自动命中；INNER JOIN 保证
    未归属合约不出现在结果里（前端显示「—」）。bills_group_symbol 无账户维度，
    故按 symbol 映射后套到所有账户行（同合约跨账户归属同一个标的）。
    """
    sql = text(f"""
        SELECT g.symbol, COALESCE(g.underlying_symbol, g.symbol) AS um_symbol, m.name
        FROM bills_group_symbol g
        JOIN irs_underlying_monitor m
          ON m.symbol = COALESCE(g.underlying_symbol, g.symbol)
        WHERE {attachable_sql('g')}
    """)
    return {r["symbol"]: (r["um_symbol"], r["name"])
            for r in db.execute(sql).mappings().all()}


def _risk_exposure_by_position(db: Session) -> dict[tuple[str, str], float | None]:
    """人工风险敞口按 (账户, 合约) 聚合，唯一人工入口是 bills_group。

    与逐仓「当前盈亏」同粒度，同合约跨账户各取各的值、不重复计；
    排除 account='合计'（🔒1.5）。NULL = 未设置，前端显示「—」，
    不参与任何模型推算，也不随假设价变化。
    """
    sql = text(f"""
        SELECT b.account, b.symbol, SUM(b.risk_exposure) AS exposure
        FROM bills_group b
        WHERE b.account <> '合计' AND {attachable_sql('b')}
        GROUP BY b.account, b.symbol
    """)
    return {(r["account"], r["symbol"]): (lvl._f(r["exposure"]) or None)
            for r in db.execute(sql).mappings().all()}


def build(db: Session, overrides: dict[str, float],
          horizon: str = "now", iv_mult: float = 1.0) -> dict:
    """多标的假设价推演；纯现算，不写库。非法标的/价格抛 ValueError → 400。"""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon 只支持 now / expiry：{horizon}")
    if not overrides:
        overrides = {}

    # ---- 1. 校验标的与假设价 ----
    vms: dict[str, UnderlyingMonitor] = {}
    for sym, price_at in overrides.items():
        um = db.query(UnderlyingMonitor).filter(UnderlyingMonitor.symbol == sym).first()
        if um is None:
            raise ValueError(f"标的不存在：{sym}")
        price_now = lvl._f(um.price)
        if price_now <= 0:
            raise ValueError(f"标的 {sym} 最新价为空或为零，无法按假设价推演")
        if float(price_at) <= 0:
            raise ValueError(f"标的 {sym} 假设价必须为正数：{price_at}")
        vms[sym] = um

    # ---- 2. 风险上下文（一次） ----
    try:
        ctx = risk_scenario.load_context()
    except Exception as exc:  # noqa: BLE001  行情陈旧/合约未认领等统一转 400
        raise ValueError(f"风险上下文不可用：{exc}") from exc

    warnings: list[str] = list(lvl.FIXED_NOTES) + list(ctx.get("warnings") or [])

    # 归属标的与人工敞口：与改价与否无关，初始页（overrides={}）也要有值。
    # subject 原先只取「本次改价由哪个标的驱动」，未改价的冻结行全是 None，列名误导。
    subject_of = _subject_by_symbol(db)
    exposure_of = _risk_exposure_by_position(db)

    # ---- 3. 每标的：归属持仓 → shifts（同合约被两个标的命中时后者忽略并告警） ----
    shifts: dict[tuple, float] = {}
    key_subject: dict[tuple, str] = {}
    subject_info: dict[str, dict] = {}
    for sym, price_at in overrides.items():
        um = vms[sym]
        price_now = lvl._f(um.price)
        pct = float(price_at) / price_now - 1
        owned = lvl._owned_rows(db, sym)
        in_pos_syms = {r["symbol"] for r in owned if lvl._f(r["p_total"]) != 0}
        target = [p for p in ctx["positions"] if p.get("symbol") in in_pos_syms]
        if in_pos_syms and not target:
            raise ValueError(
                f"标的 {sym} 有 {len(in_pos_syms)} 条在仓归属合约，但在风险上下文中未命中任何持仓；"
                "请检查归属列、合约认领与行情新鲜度。"
            )
        keys: list[tuple] = []
        for p in target:
            k = (p["acct"], p.get("symbol"))
            if k in shifts and abs(shifts[k] - (-pct)) > 1e-12:
                warnings.append(
                    f"持仓 {k[0]}/{k[1]} 同时被 {key_subject.get(k, '?')} 与 {sym} 改价命中，"
                    "同合约只能有一个假设价，后者的跌幅已忽略。"
                )
                continue
            shifts[k] = -pct
            key_subject[k] = sym
            keys.append(k)
        basis, basis_warns = lvl._price_basis(price_now, target)
        warnings.extend(basis_warns)
        subject_info[sym] = {
            "price_now": price_now, "price_at": float(price_at), "pct": pct,
            "keys": keys, "owned": owned, "price_basis": basis,
        }

    # ---- 4. 五次 run：情景双时点 / 0% 基准 / 预算两跑（现价口径基准） ----
    # shifts={} 与 shifts=None 语义不同：None 走全组同跌模式，{} 才是「全冻结」。
    other_horizon = "expiry" if horizon == "now" else "now"
    scenario = risk_scenario.run(ctx, "sync", 0.0, horizon=horizon,
                                 iv_mult=iv_mult, shifts=shifts)
    # 逐仓明细同时给「即期Δ / 到期Δ」两列：再跑一次另一时点（纯内存，不查库）。
    # 保证金只吃标的价格 S、与 horizon 无关，故 0% 基准仍只跑主时点一次。
    scenario_other = risk_scenario.run(ctx, "sync", 0.0, horizon=other_horizon,
                                       iv_mult=iv_mult, shifts=shifts)
    base = risk_scenario.run(ctx, "sync", 0.0, horizon=horizon,
                             iv_mult=iv_mult, shifts={})
    b_now = risk_scenario.run(ctx, "sync", 0.0)
    b_stress = risk_scenario.run(ctx, "sync", STRESS_DROP)
    budget = budget_mod.evaluate(ctx, ctx["params"], b_now, b_stress)

    dm = {(d["acct"], d["name"]): d for d in scenario["detail"]}
    dm_other = {(d["acct"], d["name"]): d for d in scenario_other["detail"]}
    bm = {(d["acct"], d["name"]): d for d in base["detail"]}

    # ---- 5. 逐仓明细（全部在仓持仓，含冻结行 —— Σdelta_pnl 可与 combo.loss 对账） ----
    # 账面浮盈按 (账户, 合约) 聚合：与 risk_scenario.compute 的逐仓口径一致，
    # 同合约多行（不同 category）不会漏算/重复计。静态值，不随假设价变化。
    pf_pos: dict[tuple, float] = {}
    for p in ctx["positions"]:
        pk = (p["acct"], p["name"])
        pf_pos[pk] = pf_pos.get(pk, 0.0) + lvl._f(p.get("pf_now"))

    rows: list[dict] = []
    for p in ctx["positions"]:
        k = (p["acct"], p["name"])
        d = dm.get(k)
        if d is None:
            warnings.append(f"持仓 {k[0]}/{p.get('symbol')} 未出现在情景明细中，已跳过。")
            continue
        psym = p.get("symbol")
        shift = shifts.get((p["acct"], psym), 0.0)
        base_price = p.get("S0") if p["kind"] in ("call", "put") else p.get("px")
        d_other = dm_other.get(k)
        if d_other is None:
            warnings.append(f"持仓 {k[0]}/{psym} 未出现在另一时点明细中，对照Δ盈亏按 0 处理。")
        pnl_main = lvl._r2(-d["loss"])
        pnl_other = lvl._r2(-lvl._f(d_other.get("loss"))) if d_other else 0.0
        # 两列固定按即期/到期排列，不受顶部时点切换影响；主时点只决定摘要口径。
        pnl_now, pnl_expiry = ((pnl_main, pnl_other) if horizon == "now"
                               else (pnl_other, pnl_main))
        subj, subj_name = subject_of.get(psym, (None, None))
        rows.append({
            "acct": p["acct"],
            "symbol": psym,
            "name": p["name"],
            "kind": p["kind"],
            "cp": p.get("cp"),
            "strike": p.get("strike"),
            "qty": p.get("qty"),
            "mult": p.get("mult"),
            "subject": subj,
            "subject_name": subj_name,
            "changed": (p["acct"], psym) in shifts,
            "price_now": base_price,
            "price_at": (base_price or 0) * (1 - shift),
            "shift": shift,
            "pf_now": round(pf_pos.get(k, 0.0), 2),
            "delta_pnl": pnl_main,
            "delta_pnl_now": pnl_now,
            "delta_pnl_expiry": pnl_expiry,
            "margin_at": round(lvl._f(d["margin"]), 2),
            "margin_delta": round(lvl._f(d["margin"]) - lvl._f(bm.get(k, {}).get("margin")), 2),
            "risk_exposure": exposure_of.get((p["acct"], psym)),
        })

    # ---- 5b. 分账户风险指标（与 underlying-monitors 档位 Tab 同构） ----
    # 账户层净值/权益/保证金/双口径风险度全部由 run() 现算，前端禁自算（🔒4.13）。
    # 与档位 Tab 的差别：那里 Δ 只统计「该标的项下持仓」，这里可多标的并行改价，
    # 故统计账户全部持仓 ⇒ Σ分账户 Δ 可与 combo.loss 完整对账（冻结行 loss 恒 0）。
    accounts = lvl._account_rows(scenario)
    pf_acct = lvl._pf_by_account(db)

    def _acct_deltas(res: dict) -> dict[str, float]:
        out: dict[str, float] = {}
        for d in res["detail"]:
            out[d["acct"]] = out.get(d["acct"], 0.0) - lvl._f(d["loss"])
        return out

    main_acct = _acct_deltas(scenario)
    other_acct = _acct_deltas(scenario_other)
    base_margin = {a["account"]: a["margin_at"] for a in lvl._account_rows(base)}
    for a in accounts:
        d_main = main_acct.get(a["account"], 0.0)
        d_other = other_acct.get(a["account"], 0.0)
        # 两列固定按即期/到期排列（与逐仓明细一致），不受顶部时点切换影响
        pnl_now, pnl_expiry = ((d_main, d_other) if horizon == "now"
                               else (d_other, d_main))
        a["pf_now"] = lvl._r2(pf_acct.get(a["account"], 0.0))
        a["delta_pnl"] = lvl._r2(d_main)
        a["delta_pnl_now"] = lvl._r2(pnl_now)
        a["delta_pnl_expiry"] = lvl._r2(pnl_expiry)
        a["margin_delta"] = round(a["margin_at"] - base_margin.get(a["account"], a["margin_at"]), 2)

    combo = lvl._combo(scenario)
    baseline_combo = lvl._combo(base)

    # ---- 6. 每标的假设态小结（zone_at / 假设价剧本命中 / 该标的合计） ----
    subjects: dict[str, dict] = {}
    for sym, info in subject_info.items():
        um = vms[sym]
        keys_set = set(info["keys"])
        s_rows = [r for r in rows if (r["acct"], r["symbol"]) in keys_set]
        delta_pnl = round(sum(r["delta_pnl"] for r in s_rows), 2)
        margin_at = round(sum(r["margin_at"] for r in s_rows), 2)
        margin_delta = round(sum(r["margin_delta"] for r in s_rows), 2)
        in_pos = [r for r in info["owned"] if lvl._f(r["p_total"]) != 0]
        pf_now = round(sum(lvl._f(r["pf_total"]) for r in in_pos), 2)

        pp_dict = {field: getattr(um, field) for _, field, _ in lvl.LEVELS}
        ver = um_version.effective_version(db, sym)
        pp = ({"pp_el": ver.pp_el, "pp_l": ver.pp_l, "pp_m": ver.pp_m,
               "pp_h": ver.pp_h, "pp_eh": ver.pp_eh} if ver is not None else {})
        plans_at = [{
            "seq": p.seq, "trade_symbol": p.trade_symbol, "b_s": p.b_s,
            "o_c": p.o_c, "reason": p.reason,
            "effective_trigger": effective_trigger(p, pp),
            "hit_at": bool(um_today._is_hit(p, pp, info["price_at"])),
        } for p in um_today._unfinished_plans(db, sym)]

        subjects[sym] = {
            "name": um.name,
            "price_now": info["price_now"],
            "price_at": info["price_at"],
            "pct": round(info["pct"], 6),
            "zone_now": um_version.compute_zone(pp_dict, info["price_now"]),
            "zone_at": um_version.compute_zone(pp_dict, info["price_at"]),
            "plans_at": plans_at,
            "pf_now": pf_now,
            "pf_at_level": round(pf_now + delta_pnl, 2),
            "delta_pnl": delta_pnl,
            "margin_at": margin_at,
            "margin_delta": margin_delta,
            "positions": s_rows,
            "price_basis": info["price_basis"],
        }

    # ---- 7. 未归属持仓显式报数 ----
    unmapped = lvl._unmapped_in_position(db)
    if unmapped["rows"]:
        warnings.append(
            f"另有 {unmapped['rows']} 条在仓合约未归属到任何标的（市值 {unmapped['value_total']}），"
            "不计入任何假设价情景（冻结）。"
        )

    warnings.extend([
        "逐仓「当前盈亏」为账单账面浮盈（静态，不随假设价变化），Δ盈亏为模型盯市增量；"
        "两者基准不同，不可相加对账。",
        "逐仓「风险敞口」为人工确认值（bills_group，按账户+合约拆分），空=未设置；"
        "不是模型推算，也不随假设价变化。",
        "逐仓明细同时给出即期/到期两时点Δ盈亏；组合摘要、改价小结与预算仍按顶部所选时点；"
        "保证金逐档重估，与即期/到期时点无关。",
        "「标的」列为持仓的归属监测标的（含未改价的冻结行）；高亮底色才表示本次被改价命中。",
        "分账户「账户当前盈亏」为账单账户层面全部持仓账面浮盈（静态），两列 Δ 盈亏为模型盯市增量"
        "（账户全部持仓，Σ=组合 Δ盈亏），两者基准不同、不可相加对账；保证金变化相对 0% 基准；"
        "追保只判交易所口径，真实口径 ≥1 而交易所口径 <1 时须单独留意；空账户不展示。",
    ])

    return {
        "as_of": str(ctx.get("calc_date") or ""),
        "data_as_of": ctx.get("data_as_of"),
        "stale_days": ctx.get("stale_days"),
        "engine_ver": risk_scenario.ENGINE_VER,
        "horizon": horizon,
        "iv_mult": iv_mult,
        "changed_symbols": list(overrides.keys()),
        "combo": combo,
        "baseline_combo": baseline_combo,
        "budget": {
            "stress_drop": budget["stress_drop"],
            "summary": budget["summary"],
            "note": "预算读数为现价基准（0% 与 +15% 全组同跌、horizon=now），不随滑块假设价变化。",
        },
        "subjects": subjects,
        "position_rows": rows,
        "accounts": accounts,
        "warnings": warnings,
    }
