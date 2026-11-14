# -*- coding: utf-8 -*-
"""标的推演（POST /irs/simulate）：多标的假设价 → 组合风险/收益联动推演。

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
   风险敞口复用 _exposure_by_account（bills_group_acc 静态值），列集合与档位 Tab 逐列一致；
   Δ 统计账户全部持仓以便与 combo.loss 对账；账户当前盈亏取 bills_group_acc 账面值。
6. 纯现算：不写库、不建任务。

可查可核的两条锚点（探针 scripts/_probe_um_simulate.py 验收，不改运行时行为）：
- 锚点 1：overrides={} 时 baseline_combo 与 GET /irs/risk-board 同口径一致；
- 锚点 2：单标的假设价=某档位价时，subjects[sym] 与 level-scenario 同档 combo 一致。

预算读数（budget.evaluate）：恒为**现价基准**（0% 与生效压力档全组同跌、horizon=now），
压力档读 `irs_risk_param` 的 `('*','budget_stress')`，经 budget.stress_run() 生成。
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
          horizon: str = "now", iv_mult: float = 1.0, *,
          ctx: dict | None = None,
          include_budget: bool = True,
          include_subjects: bool = True) -> dict:
    """多标的假设价推演；纯现算，不写库。非法标的/价格抛 ValueError → 400。

    ctx / include_budget / include_subjects 只给 build_levels() 五档批量用：
    共用一次风险上下文、跳过每档重复的预算与剧本查询；直接调 /irs/simulate
    时三个参数全走默认值，行为与改造前逐字节一致（探针双锚点不变）。
    """
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

    # ---- 2. 风险上下文（一次）----
    # build_levels() 五档复用同一 ctx：load_context 要查行情/账户/持仓，跑 5 次太贵，
    # 且同一请求内五档必须用同一份行情快照，否则跨档数值不可比。
    try:
        if ctx is None:
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
    # 预算恒为现价基准、不随假设价变化；五档批量只在 0% 基准那一跑算一次。
    budget = None
    if include_budget:
        b_now = risk_scenario.run(ctx, "sync", 0.0)
        _, b_stress = budget_mod.stress_run(ctx, ctx["params"])
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
        # 两列固定按即期/到期排列，与时点参数无关；主时点只决定摘要口径。
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
            # 与 run() 的 detail 同源，供档位压力的「保证金结构（按品种族）」分组，
            # 前端复用 marginGroupOf() 聚合，不另写一套分组规则。
            "mtype": d.get("mtype"),
            "und_key": d.get("und_key"),
        })

    # ---- 5a. 标的项下持仓敞口合计（左栏卡片 / 右栏逐仓排序的唯一数据源） ----
    # 口径与逐仓「风险敞口」列同源：exposure_of 按 (账户, 合约) 聚合 bills_group
    # （已排除 account='合计'，🔒1.5），按归属标的求和；rows 来自 ctx["positions"]，
    # 故只计在仓持仓。同一 (账户, 合约) 在 rows 里可能对应多行，去重避免重复计；
    # NULL = 未设置，不参与求和；未归属标的不进 map，前端按 0 处理、固定排最后。
    seen_exp: set[tuple] = set()
    subject_exposure: dict[str, float] = {}
    for r in rows:
        k = (r["acct"], r["symbol"])
        if k in seen_exp:
            continue
        seen_exp.add(k)
        exp = exposure_of.get(k)
        subj = r["subject"]
        if not subj or exp is None:
            continue
        subject_exposure[subj] = round(subject_exposure.get(subj, 0.0) + float(exp), 2)

    # ---- 5b. 分账户风险指标（与 underlying-monitors 档位 Tab 同构） ----
    # 账户层净值/权益/保证金/双口径风险度全部由 run() 现算，前端禁自算（🔒4.13）。
    # 与档位 Tab 的差别：那里 Δ 只统计「该标的项下持仓」，这里可多标的并行改价，
    # 故统计账户全部持仓 ⇒ Σ分账户 Δ 可与 combo.loss 完整对账（冻结行 loss 恒 0）。
    accounts = lvl._account_rows(scenario)
    pf_acct = lvl._pf_by_account(db)
    # 账户风险敞口与档位 Tab 同源（lvl._exposure_by_account → bills_group_acc），
    # 静态人工值、含账户全部持仓、与归属标的无关；NULL 保留 None ⇒ 前端显示 '-'。
    exposure_acct = lvl._exposure_by_account(db)

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
        # 两列固定按即期/到期排列（与逐仓明细一致），与时点参数无关
        pnl_now, pnl_expiry = ((d_main, d_other) if horizon == "now"
                               else (d_other, d_main))
        a["pf_now"] = lvl._r2(pf_acct.get(a["account"], 0.0))
        exp = exposure_acct.get(a["account"])
        a["risk_exposure"] = round(exp, 2) if exp is not None else None
        a["delta_pnl"] = lvl._r2(d_main)
        a["delta_pnl_now"] = lvl._r2(pnl_now)
        a["delta_pnl_expiry"] = lvl._r2(pnl_expiry)
        a["margin_delta"] = round(a["margin_at"] - base_margin.get(a["account"], a["margin_at"]), 2)

    # 组合行「保证金变化」= 到档合计保证金 − 0% 基准合计保证金；
    # 基准值同一次 run() 现取，前端不自行相减（🔒4.13）。baseline_combo 传自己的 margin ⇒ 恒 0。
    combo = lvl._combo(scenario, base["margin"])
    baseline_combo = lvl._combo(base, base["margin"])
    # 另一时点的组合读数：逐仓 / 分账户两表本来就固定给「即期Δ、到期Δ」两列，
    # 组合层原先只有主时点一份 loss，档位压力表要并列即期/到期就只能让前端自算 —— 违反 🔒4.13。
    # 这里把已跑过的 scenario_other 一并折成组合读数（保证金只吃标的价格、与 horizon 无关，
    # 故基准保证金仍用同一份 base["margin"]，不另跑 0% 基准）。
    combo_other = lvl._combo(scenario_other, base["margin"])

    # ---- 6. 每标的假设态小结（zone_at / 假设价剧本命中 / 该标的合计） ----
    # 档位压力只用组合/账户/逐仓三张表，跳过可省掉每档的 effective_version、
    # 未完成剧本、命中判定等一串查库。
    subjects: dict[str, dict] = {}
    for sym, info in (subject_info.items() if include_subjects else ()):
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
        "逐仓明细同时给出即期/到期两时点Δ盈亏，到期列仅作对照；组合摘要、改价小结与预算"
        "按主时点即期（前端已固定，不再切换）；保证金逐档重估，与即期/到期时点无关。",
        "「标的」列为持仓的归属监测标的（含未改价的冻结行）；高亮底色才表示本次被改价命中。",
        "分账户「账户当前盈亏」为账单账户层面全部持仓账面浮盈（静态），两列 Δ 盈亏为模型盯市增量"
        "（账户全部持仓，Σ=组合 Δ盈亏），两者基准不同、不可相加对账；保证金变化相对 0% 基准；"
        "追保只判交易所口径，真实口径 ≥1 而交易所口径 <1 时须单独留意；空账户不展示。",
        "排序用「标的项下持仓风险敞口合计」= 该标的项下逐仓「风险敞口」之和（人工确认值，"
        "NULL=未设置不计入），静态值、不随假设价变化；未归属标的固定排最后。",
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
        # 另一时点（horizon=now 时即到期，反之即期）的组合读数，供档位压力表并列两列 Δ 盈亏；
        # 前端按 horizon 字段定排位，不依赖本字段的顺序语义。
        "combo_other": combo_other,
        "budget": ({
            "stress_drop": budget["stress_drop"],
            "summary": budget["summary"],
            "note": "预算读数为现价基准（0% 与 +15% 全组同跌、horizon=now），不随滑块假设价变化。",
        } if budget is not None else None),
        "subjects": subjects,
        # 标的项下持仓敞口合计（见 5a）：左右两栏排序共用，不随假设价变化
        "subject_exposure": subject_exposure,
        "position_rows": rows,
        "accounts": accounts,
        "warnings": warnings,
    }


# =========================================================================
# 档位压力（POST /irs/simulate/levels）：全标的同步到同一档 × 五档
# =========================================================================
def _level_overrides(db: Session, field: str) -> tuple[dict[str, float], list[str], list[float]]:
    """该档的全部标的假设价；与 simulate 页 applyBatch() 逐字段同源同规则。

    - 档位价取**主表** pp_*：/risk 左栏档位条与标的主表同一组字段（um_today 原 today_items:192，
      不现查 effective_version），保证「点压力表」与「点顶栏批量设置」得到同一组价；
    - 现价为空/非正、档位价 NULL/非正 ⇒ 跳过（保持现价），与前端 applyBatch 一致；
    - 档位价恰等于现价 ⇒ 不写 override，与 setHypo「现价即不设」语义一致；
    - 同时返回每个标的的 pct，供压力表给幅度区间（各标的幅度不同，单值会骗人）。
    """
    out: dict[str, float] = {}
    skipped: list[str] = []
    pcts: list[float] = []
    for um in db.query(UnderlyingMonitor).all():
        price_now = lvl._f(um.price)
        pp = lvl._f(getattr(um, field, None))
        if price_now <= 0 or pp <= 0:
            skipped.append(um.symbol)
            continue
        if abs(pp - price_now) < 1e-9:
            continue
        out[um.symbol] = pp
        pcts.append(pp / price_now - 1)
    return out, skipped, pcts


# 档位压力只回逐张合约表 / 保证金结构图要用的字段，避免 5 档响应体膨胀
_LEVEL_POS_KEYS = (
    "acct", "name", "symbol", "kind", "cp", "strike", "qty", "subject",
    "subject_name", "changed", "price_now", "price_at", "pf_now",
    "delta_pnl_now", "delta_pnl_expiry", "margin_at", "margin_delta",
    "risk_exposure", "mtype", "und_key",
)


def _slim_positions(rows: list[dict]) -> list[dict]:
    return [{k: r.get(k) for k in _LEVEL_POS_KEYS} for r in rows]


def build_levels(db: Session, horizon: str = "now", iv_mult: float = 1.0) -> dict:
    """五档档位压力：全部监测标的各自到自己的 pp_el/…/pp_eh，一次算完五档。

    与 build() 是同一个引擎、同一份 ctx、同一套 shifts 口径，只是把「用户手拖
    滑块」换成五个预设档批量跑。纯现算，不写库、不建任务（irs_scenario_grid 落库链路已下线）。
    输出供 /irs/simulate 右栏的档位压力表 / 逐张合约 / 保证金结构图共用；
    组合与账户指标全部后端算，前端只做渲染与展示层分组（🔒4.13）。
    """
    if horizon not in HORIZONS:
        raise ValueError(f"horizon 只支持 now / expiry：{horizon}")

    try:
        ctx = risk_scenario.load_context()
    except Exception as exc:  # noqa: BLE001  与 /irs/simulate 同一错误口径
        raise ValueError(f"风险上下文不可用：{exc}") from exc

    def _now_expiry(res: dict) -> tuple[dict, dict]:
        """把一次 build() 的主时点 / 另一时点两个组合读数，固定排成（即期, 到期）。

        build() 的 combo 跟随 horizon 参数，combo_other 是另一时点；档位压力表的
        「即期Δ盈亏 / 到期Δ盈亏」两列与逐仓、分账户两表同规矩 —— 固定按即期在前排，
        不随请求的 horizon 换位（前端已固定传 now）。
        """
        return ((res["combo"], res["combo_other"]) if horizon == "now"
                else (res["combo_other"], res["combo"]))

    # 0% 基准（全部现价）：预算读数、账户静态卡、逐仓基准都从这一跑取
    base = build(db, {}, horizon, iv_mult, ctx=ctx)
    base_now, base_exp = _now_expiry(base)
    levels: dict[str, dict] = {}
    for key, field, label in lvl.LEVELS:
        overrides, skipped, pcts = _level_overrides(db, field)
        res = build(db, overrides, horizon, iv_mult, ctx=ctx,
                    include_budget=False, include_subjects=False)
        c_now, c_exp = _now_expiry(res)
        levels[key] = {
            "key": key,
            "label": label,
            "overrides": overrides,
            "subject_count": len(overrides),
            "skipped_symbols": skipped,
            "pct_min": round(min(pcts), 6) if pcts else None,
            "pct_max": round(max(pcts), 6) if pcts else None,
            "combo": res["combo"],
            "baseline_combo": res["baseline_combo"],
            # 相对 0% 基准的 Δ 亏损（正=亏）；压力表「组合盈亏」= −delta_loss
            "delta_loss": round(res["combo"]["loss"] - base["combo"]["loss"], 2),
            # 组合层双时点 Δ 盈亏（正=盈）：取该时点 run() 的 loss 原值、**不扣 0% 基准**。
            # 与分账户 / 逐仓两表的 delta_pnl_now / delta_pnl_expiry 完全同式
            #（那边也是 Σ −detail.loss），故 Σ账户 = 组合、Σ逐仓 = 组合，两列都可对账。
            # ⚠️ 到期列在 0% 行也不是 0：所有期权的时间价值整体衰减即该列的「Δ」，
            # 这是到期口径的定义（见 lvl.FIXED_NOTES），不是漏扣基准。
            "delta_pnl_now": lvl._r2(-c_now["loss"]),
            "delta_pnl_expiry": lvl._r2(-c_exp["loss"]),
            "accounts": res["accounts"],
            "positions": _slim_positions(res["position_rows"]),
        }

    baseline = {
        "combo": base["combo"],
        "accounts": base["accounts"],
        "positions": _slim_positions(base["position_rows"]),
        "budget": base["budget"],
        # 现价行同样给两列：即期恒 0（不重估就没有 Δ），到期 = 全组合时间价值衰减（负数）
        "delta_pnl_now": lvl._r2(-base_now["loss"]),
        "delta_pnl_expiry": lvl._r2(-base_exp["loss"]),
    }

    # 首个追保（档位口径）：按到档价升序（极低→极高）找第一个 breached 的档。
    # 只判交易所口径，与旧「全组同跌 11 档」的临界跌幅不是同一口径。
    ordered = [levels[k] for k, _, _ in lvl.LEVELS]
    first_call: dict[str, str | None] = {
        a["account"]: next((lv["label"] for lv in ordered
                            if any(x["account"] == a and x["breached"] for x in lv["accounts"])), None)
        for a in base["accounts"]
    }
    first_call["ALL"] = next((lv["label"] for lv in ordered
                              if any(x["breached"] for x in lv["accounts"])), None)

    warnings = list(base["warnings"]) + [
        "档位压力 = 全部监测标的同步到同一档，每个标的用**自己的** pp_* 档位价（主表当前生效值），"
        "与推演页顶栏「批量设置」同源；无该档位或档位价恰等现价的标的保持现价。",
        "各标的到档幅度不同，压力表给的是幅度区间而非单一涨跌幅；持仓按同百分比平移"
        "（beta=1 近似），跨市场基差/贴水/折溢价漂移不计，全部数值为 [推算]。",
        "「极高」档是上涨方向，对卖出认购是压力侧；档位名不代表风险由低到高排序。",
        "首个追保（档位口径）= 按到档价从低到高第一个触发 breached 的档；只判交易所口径，"
        "与「组合 Δ盈亏」同为模型值，不构成实际追保结论。",
    ]

    return {
        "as_of": base["as_of"],
        "data_as_of": base["data_as_of"],
        "stale_days": base["stale_days"],
        "engine_ver": base["engine_ver"],
        "horizon": horizon,
        "iv_mult": iv_mult,
        "level_order": [k for k, _, _ in lvl.LEVELS],
        "level_labels": {k: lab for k, _, lab in lvl.LEVELS},
        "levels": levels,
        "baseline": baseline,
        "first_call_by_account": first_call,
        "warnings": warnings,
    }
