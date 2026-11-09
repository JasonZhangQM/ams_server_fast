# -*- coding: utf-8 -*-
"""标的监测「到档情景」：标的价格到五档时，其项下持仓的盈亏 / 保证金 / 风险指标。

方案背景见 docs/档位情景盈亏方案_2026-11-02.md；本实现按 2026-11-08 风险看板迁入 irs
后的代码重写（旧文档写的 bills/services/vm_scenario.py 与 bills_scenario_grid 均已失效）。

口径（每条都会在响应的 warnings 里原样回给前端）：
1. pct = pp_x / 监测标的主表 price − 1；shift = −pct。档位取主表当前生效版，
   与列表页 pv_* 同式，不现查 effective_version。
2. 单标的驱动：只对该标的项下持仓施加 shift，其余持仓 shift=0 —— 价格冻结，
   但保证金仍按基准价计入账户分母（漏算会把账户风险度严重低估）。
3. 持仓按**同百分比**平移（beta=1 近似）。监测标的与持仓基准价常是不同市场、
   不同量纲（GC00Y 美元/盎司 ↔ 沪金 元/克；中证500 指数点 ↔ 500ETF 元），
   因此只联动百分比，绝不比较绝对价，基差/贴水/折溢价漂移不计。
4. 期权即期 BS 重估、到期内在价值双时点；义务仓保证金逐档重算（下跌的反向变量）。
5. 纯现算：不写库、不建任务、不碰 irs_scenario_grid。

依赖方向：irs → bills（🔒4.12）。本模块不 import 任何 bills 的模型对象，
跨模块取数一律用表名字符串 SQL，只复用 bills.constants 的归属白名单片段。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.app.bills.constants import attachable_sql
from server_fast.app.irs.models import UnderlyingMonitor
from server_fast.app.irs.service import risk_scenario, um_version

LEVELS = (
    ("el", "pp_el", "极低"),
    ("l", "pp_l", "低"),
    ("m", "pp_m", "中"),
    ("h", "pp_h", "高"),
    ("eh", "pp_eh", "极高"),
)
HORIZONS = ("now", "expiry")

# 固定口径声明：前端必须原样展示，不能只写在文档里
FIXED_NOTES = [
    "档位涨跌幅按监测标的最新价计算，该项下持仓按同百分比平移（beta=1 近似）；"
    "跨市场/跨品种的基差、贴水、折溢价漂移不计，全部数值为 [推算]。",
    "义务仓保证金为交易所公式估算 [推算]，未读日结单「持仓汇总」实读值；"
    "真实/交易所风险度与 breached 均为模型口径，不构成追保结论。",
    "账户层净值、保证金、风险度是账户全部持仓口径（含本情景中价格冻结的其它持仓），"
    "不是该标的单项口径。",
    "到档浮盈 = 系统账面浮动盈亏 + 模型盯市口径 Δ盈亏，属混合口径 [推算]；"
    "逐仓与分账户并列展示账面「当前盈亏」和模型口径 Δ盈亏，两者基准不同，不可相加对账。",
    "分账户「当前盈亏」= 账户层面全部持仓账面浮盈（bills_group_acc.pf_total，排除 account='合计'），"
    "与该标的项下口径无关；五档表与逐仓「当前盈亏」仍只统计该标的项下持仓，"
    "三处同名列口径不同，不可互相相加或作对账。",
]


def _f(v) -> float:
    """Decimal / None → float，None 归零。"""
    return float(v) if v is not None else 0.0


def _r2(v) -> float:
    """2 位小数并消掉 -0.0（无持仓标的的基准档会算出负零）。"""
    x = round(float(v), 2)
    return 0.0 if x == 0 else x


def _owned_rows(db: Session, symbol: str) -> list[dict]:
    """该标的项下的合约行：显式归属 OR 同代码自动命中（与 um_map.coverage 同口径）。"""
    sql = text(f"""
        SELECT g.category, g.symbol, g.p_total, g.pf_total, g.price, g.multiplier,
               g.value_total, g.risk_exposure, g.pl_all, g.pfl_all
        FROM bills_group_symbol g
        WHERE (g.underlying_symbol = :s OR g.symbol = :s)
          AND {attachable_sql('g')}
        ORDER BY g.symbol
    """)
    return [dict(r) for r in db.execute(sql, {"s": symbol}).mappings().all()]


def _pf_by_account(db: Session) -> dict:
    """账户层面全部持仓的账面浮盈：直接读账户汇总表 bills_group_acc.pf_total。

    与标的归属无关（用户 2026-11-09 定：分账户「当前盈亏」按账户层面取数）。
    只读探针已核对：该值与 bills_group 按 account 汇总 pf_total 逐账户差 0，
    账户键与 load_context() 的 acct 完全对齐。必须排除 account='合计'（🔒1.5），
    该行是各账户之和，重复计会把整表放大一倍。
    """
    sql = text("""
        SELECT account, pf_total
        FROM bills_group_acc
        WHERE account <> '合计'
    """)
    return {r["account"]: _f(r["pf_total"])
            for r in db.execute(sql).mappings().all()}


def _pf_by_position(db: Session, symbol: str) -> dict:
    """该标的项下在仓持仓的账面浮盈按 (账户, 合约) 拆分。

    bills_group 唯一键 account+category+symbol、每行带 pf_total；按 (account, symbol)
    求和后与 load_context() 的 (acct, symbol) 键一一对应（只读探针已核对：无重复键、
    全命中）。过滤条件必须与 _pf_by_account 完全一致，保证
    Σ逐仓 == Σ分账户 == 标的合计 pf_now。
    """
    sql = text(f"""
        SELECT b.account, b.symbol, SUM(b.pf_total) AS pf_now
        FROM bills_group b
        JOIN bills_group_symbol g
          ON g.category = b.category AND g.symbol = b.symbol
        WHERE (g.underlying_symbol = :s OR g.symbol = :s)
          AND {attachable_sql('g')}
          AND b.account <> '合计'
          AND b.p_total IS NOT NULL AND b.p_total <> 0
        GROUP BY b.account, b.symbol
    """)
    return {(r["account"], r["symbol"]): _f(r["pf_now"])
            for r in db.execute(sql, {"s": symbol}).mappings().all()}


def _unmapped_in_position(db: Session) -> dict:
    """在仓但未归属的合约行 —— 人工归属漏配会静默少算，必须显式报数。"""
    sql = text(f"""
        SELECT g.category, g.symbol, g.p_total, g.value_total
        FROM bills_group_symbol g
        WHERE g.p_total IS NOT NULL AND g.p_total <> 0
          AND {attachable_sql('g')}
          AND g.underlying_symbol IS NULL
          AND g.symbol NOT IN (SELECT symbol FROM irs_underlying_monitor)
        ORDER BY g.symbol
    """)
    rows = [dict(r) for r in db.execute(sql).mappings().all()]
    return {
        "rows": len(rows),
        "value_total": round(sum(_f(r["value_total"]) for r in rows), 2),
        "symbols": [r["symbol"] for r in rows][:20],
    }


def _price_basis(price: float, target_positions: list[dict]) -> tuple[dict, list[str]]:
    """并列监测标的价与引擎基准价；只有量纲可比（比值 0.5~2）时才算 gap_pct。"""
    warns: list[str] = []
    engine = []
    comparable = []
    for p in target_positions:
        base = p.get("S0") if p["kind"] in ("call", "put") else p.get("px")
        if base is None or base <= 0:
            continue
        row = {"symbol": p.get("symbol"), "und_key": p.get("und_key"), "base": float(base)}
        engine.append(row)
        ratio = float(base) / price
        if 0.5 <= ratio <= 2.0:
            comparable.append(float(base))
    gap_pct = None
    if comparable:
        gap_pct = round((sum(comparable) / len(comparable)) / price - 1, 6)
        if abs(gap_pct) > 0.01:
            warns.append(
                f"监测标的价 {price} 与风险引擎标的基准价均值偏差 {gap_pct * 100:.2f}%，"
                "档位涨跌幅仍按监测标的价算，请核对行情时点。"
            )
    if not comparable and engine:
        warns.append(
            "本标的与其项下持仓基准价属不同市场/量纲（如外盘金价 ↔ 沪金、指数点 ↔ ETF 元），"
            "仅按同百分比联动，绝对价不可比。"
        )
    return {
        "um_price": price,
        "engine_prices": engine,
        "comparable_count": len(comparable),
        "gap_pct": gap_pct,
    }, warns


def _detail_map(res: dict) -> dict:
    """run() 的 detail 没有 symbol 列，但 name 就是合约代码（现场探针已核对）。"""
    return {(d["acct"], d["name"]): d for d in res["detail"]}


def _account_rows(res: dict) -> list[dict]:
    """账户层全部指标；空账户（无净值无保证金）不展示。"""
    out = []
    for acct, a in sorted(res["accounts"].items()):
        if a["net"] == 0 and a["margin"] == 0:
            continue
        out.append({
            "account": acct,
            "net_at": round(a["net"], 2),
            "eq_at": round(a["eq"], 2),
            "margin_at": round(a["margin"], 2),
            "real": a["real"],
            "exch": a["exch"],
            "coverage": (a["net"] / a["margin"] if a["margin"] > 0 else None),
            "breached": a["breached"],
        })
    return out


def _combo(res: dict) -> dict:
    """组合层：real 直接取 run()，交易所口径按 Σ客户权益 现算（与 compute 的 ALL 行同式）。"""
    eq_total = sum(a["eq"] for a in res["accounts"].values())
    return {
        "net_at": round(res["net"], 2),
        "margin_total": round(res["margin"], 2),
        "real": res["real"],
        "exch": (res["margin"] / eq_total if eq_total > 0 else None),
        "loss": round(res["loss"], 2),
    }


def build(db: Session, symbol: str, horizon: str = "now", iv_mult: float = 1.0) -> dict:
    """单标的五档 × 双时点情景；纯现算，不写库。"""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon 只支持 now / expiry：{horizon}")
    um = db.query(UnderlyingMonitor).filter(UnderlyingMonitor.symbol == symbol).first()
    if um is None:
        raise ValueError(f"标的不存在：{symbol}")
    price = _f(um.price)
    if price <= 0:
        raise ValueError(f"标的 {symbol} 最新价为空或为零，无法计算档位涨跌幅")

    levels_pp = {key: _f(getattr(um, field)) for key, field, _ in LEVELS
                 if getattr(um, field) is not None and _f(getattr(um, field)) > 0}
    owned = _owned_rows(db, symbol)
    owned_by_symbol = {r["symbol"]: r for r in owned}
    target_symbols = set(owned_by_symbol)
    # 归属表含已平仓历史行，只有 p_total != 0 的才要求进入风险上下文
    in_pos_symbols = {r["symbol"] for r in owned if _f(r["p_total"]) != 0}

    try:
        ctx = risk_scenario.load_context()
    except Exception as exc:  # noqa: BLE001  行情陈旧/合约未认领等统一转 409
        raise ValueError(f"风险上下文不可用：{exc}") from exc

    target_positions = [p for p in ctx["positions"] if p.get("symbol") in target_symbols]
    # 键控哨兵：有在仓归属行却一条都没命中 ⇒ 绝不静默少算
    if in_pos_symbols and not target_positions:
        raise ValueError(
            f"标的 {symbol} 有 {len(in_pos_symbols)} 条在仓归属合约，但在风险上下文中未命中任何持仓；"
            "请检查归属列、合约认领与行情新鲜度。"
        )
    shift_keys = {(p["acct"], p.get("symbol")) for p in target_positions}

    warnings = list(FIXED_NOTES) + list(ctx.get("warnings") or [])
    missing = in_pos_symbols - {p.get("symbol") for p in target_positions}
    if missing:
        warnings.append(
            f"有 {len(missing)} 条在仓归属合约未进入风险上下文（{', '.join(sorted(missing))[:160]}），"
            "本情景未计入，请检查合约认领与行情。"
        )
    price_basis, basis_warns = _price_basis(price, target_positions)
    warnings.extend(basis_warns)
    unmapped = _unmapped_in_position(db)
    if unmapped["rows"]:
        warnings.append(
            f"另有 {unmapped['rows']} 条在仓合约未归属到任何标的（市值 {unmapped['value_total']}），"
            "不计入任何档位情景。"
        )
    if not target_positions:
        warnings.append(f"标的 {symbol} 当前无在仓持仓，仅展示档位价，盈亏与保证金为空。")
    # 分账户账面浮盈：账户层面口径，与标的归属无关，故不再与 pf_now_total 对账
    pf_by_acct = _pf_by_account(db)
    pf_now_total = sum(_f(r["pf_total"]) for r in owned if _f(r["p_total"]) != 0)
    # 逐仓账面浮盈：只统计真正进入风险上下文的键，漏配/未认领会显式报差
    pf_by_pos = _pf_by_position(db, symbol)
    pf_pos_gap = round(sum(v for k, v in pf_by_pos.items() if k in shift_keys)
                       - pf_now_total, 2)
    if abs(pf_pos_gap) > 0.01:
        warnings.append(
            f"逐持仓账面浮盈合计与标的账面浮盈相差 {pf_pos_gap} 元，"
            "部分在仓归属行可能未进入风险上下文，逐仓「当前盈亏」仅供参考。"
        )
    if len(levels_pp) < len(LEVELS):
        warnings.append("部分档位价为空，这些档位不参与计算。")

    # 基准档（pct=0）：既用于 margin_delta，也是「与 risk-board 0% 一致」的验收锚点
    base_by_horizon = {
        h: risk_scenario.run(ctx, "sync", 0.0, horizon=h, iv_mult=iv_mult,
                             shifts={k: 0.0 for k in shift_keys})
        for h in HORIZONS
    }

    def assemble(res: dict, base: dict, res_other: dict, pf_by_acct: dict,
                 pf_by_pos: dict, pp: float, pct: float, h: str) -> dict:
        dm = _detail_map(res)
        bd = _detail_map(base)
        dm_other = _detail_map(res_other)
        positions = []
        for p in target_positions:
            key = (p["acct"], p["name"])
            d = dm.get(key)
            b = bd.get(key)
            if d is None:
                warnings.append(f"持仓 {p['acct']}/{p.get('symbol')} 未出现在情景明细中，已跳过。")
                continue
            if key not in dm_other:
                warnings.append(
                    f"持仓 {p['acct']}/{p.get('symbol')} 未出现在另一时点明细中，对照 Δ盈亏按 0 处理。")
            margin_at = _f(d["margin"])
            margin_base = _f(b["margin"]) if b else 0.0
            g = owned_by_symbol.get(p.get("symbol"), {})
            positions.append({
                "account": p["acct"],
                "category": g.get("category"),
                "symbol": p.get("symbol"),
                "name": p.get("name"),
                "kind": p["kind"],
                "cp": p.get("cp"),
                "strike": p.get("strike"),
                "days": p.get("days"),
                "qty": p.get("qty"),
                "mult": p.get("mult"),
                "delta": p.get("delta"),
                "sigma": p.get("sigma"),
                "mtype": p.get("mtype"),
                "und_key": p.get("und_key"),
                "price_now": p.get("S0") if p["kind"] in ("call", "put") else p.get("px"),
                "price_at": ((p.get("S0") if p["kind"] in ("call", "put") else p.get("px")) or 0) * (1 + pct),
                "value_now": round(_f(p.get("sys_value")), 2),
                "pf_now": _r2(pf_by_pos.get((p["acct"], p.get("symbol")), 0.0)),
                "delta_pnl": _r2(-_f(d["loss"])),
                "other_delta_pnl": _r2(-_f(dm_other.get(key, {}).get("loss"))),
                "margin_at": round(margin_at, 2),
                "margin_delta": round(margin_at - margin_base, 2),
                "risk_exposure": _f(g.get("risk_exposure")) or None,
            })
        target_margin = sum(_f(p["margin_at"]) for p in positions)
        base_margin = sum(_f(p["margin_at"]) - _f(p["margin_delta"]) for p in positions)
        delta_pnl = -res["loss"]
        in_pos = [r for r in owned if _f(r["p_total"]) != 0]
        pf_now = sum(_f(r["pf_total"]) for r in in_pos)
        exposure = sum(_f(r["risk_exposure"]) for r in in_pos)
        pf_at = pf_now + delta_pnl

        # 逐账户 Δ 盈亏：只聚合该标的项下的持仓（其余持仓 shift=0，loss 恒 0）
        target_keys = {(p["acct"], p["name"]) for p in target_positions}

        def _acct_deltas(detail: list[dict]) -> dict:
            out: dict = {}
            for d in detail:
                if (d["acct"], d["name"]) in target_keys:
                    out[d["acct"]] = out.get(d["acct"], 0.0) - _f(d["loss"])
            return out

        main_acct = _acct_deltas(res["detail"])
        other_acct = _acct_deltas(res_other["detail"])
        accounts = _account_rows(res)
        for a in accounts:
            a["pf_now"] = _r2(pf_by_acct.get(a["account"], 0.0))
            a["delta_pnl"] = _r2(main_acct.get(a["account"], 0.0))
            a["other_delta_pnl"] = _r2(other_acct.get(a["account"], 0.0))
        return {
            "pp": round(pp, 4),
            "pct": round(pct, 6),
            "shift": round(-pct, 6),
            "horizon": h,
            "positions": positions,
            "total": {
                "delta_pnl": _r2(delta_pnl),
                "pf_now": _r2(pf_now),
                "pf_at_level": _r2(pf_at),
                "margin_at": round(target_margin, 2),
                "margin_delta": round(target_margin - base_margin, 2),
                "risk_exposure": round(exposure, 2) if exposure else None,
                "drawdown_ratio": (round(abs(pf_at) / exposure, 4) if exposure > 0 else None),
                "pf_at_note": "[推算] 账面浮盈 + 模型口径 Δ盈亏，混合口径",
            },
            "combo": _combo(res),
            "accounts": accounts,
        }

    def summary(res: dict, base: dict, pp: float, h: str) -> dict:
        """另一时点只给汇总，避免响应翻倍。"""
        dm = _detail_map(res)
        bd = _detail_map(base)
        margin_at = sum(_f(dm.get((p["acct"], p["name"]), {}).get("margin")) for p in target_positions)
        margin_base = sum(_f(bd.get((p["acct"], p["name"]), {}).get("margin")) for p in target_positions)
        return {
            "delta_pnl": _r2(-res["loss"]),
            "margin_at": round(margin_at, 2),
            "margin_delta": round(margin_at - margin_base, 2),
            "combo": _combo(res),
            "breached_accounts": [a for a, v in res["accounts"].items() if v["breached"]],
        }

    levels: dict = {}
    compare: dict = {}
    other = "expiry" if horizon == "now" else "now"
    for key, field, label in LEVELS:
        if key not in levels_pp:
            continue
        pp = levels_pp[key]
        pct = pp / price - 1
        shifts = {k: -pct for k in shift_keys}
        res_main = risk_scenario.run(ctx, "sync", 0.0, horizon=horizon,
                                    iv_mult=iv_mult, shifts=shifts)
        res_other = risk_scenario.run(ctx, "sync", 0.0, horizon=other,
                                     iv_mult=iv_mult, shifts=shifts)
        item = assemble(res_main, base_by_horizon[horizon], res_other, pf_by_acct,
                        pf_by_pos, pp, pct, horizon)
        item["label"] = label
        levels[key] = item
        compare[key] = {
            "horizon": other,
            **summary(res_other, base_by_horizon[other], pp, other),
        }

    pp_dict = {field: getattr(um, field) for _, field, _ in LEVELS}
    return {
        "as_of": str(ctx.get("calc_date") or ""),
        "data_as_of": ctx.get("data_as_of"),
        "stale_days": ctx.get("stale_days"),
        "engine_ver": risk_scenario.ENGINE_VER,
        "symbol": um.symbol,
        "name": um.name,
        "horizon": horizon,
        "iv_mult": iv_mult,
        "zone": um_version.compute_zone(pp_dict, um.price),
        "price_basis": price_basis,
        "mapping": {
            "owned_group_rows": len(owned),
            "in_position_group_rows": len([r for r in owned if _f(r["p_total"]) != 0]),
            "matched_positions": len(target_positions),
            # 归属表含大量已平仓历史行，只回前 20 条供排查，完整数另行给出
            "target_symbols": sorted(target_symbols)[:20],
            "target_symbols_total": len(target_symbols),
            "unmapped": unmapped,
        },
        "level_order": [k for k, _, _ in LEVELS if k in levels],
        "level_labels": {k: lab for k, _, lab in LEVELS},
        "levels": levels,
        "horizon_compare": compare,
        "warnings": warnings,
    }
