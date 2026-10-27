# -*- coding: utf-8 -*-
"""持仓风险敞口与情景压力测试计算服务。

移植自 scripts/risk_scenario_20261025.py（方案见
docs/持仓风险敞口与情景压力测试方案_2026-10-25.md），三个入口：

- load_context():      读持仓/账户/合约字典/风险参数，构造可复用的计算上下文
- compute():           全量计算（持仓明细 + 分账户情景矩阵 + 首个追保点），不写库
- run_and_persist():   compute + UPSERT bills_scenario_grid（供任务化重算，铁律 4.8）

四个关键口径（方案 §1，不得改动前不通读方案）：
1. 风险度双口径：真实=保证金/净资产（扣融资），交易所=保证金/客户权益（含融资）；
   预警以真实口径为准，追保判定（breached/首个追保点）用交易所口径——期货公司只看它。
2. 期权基准用市值不是成本：price=0 的降级让空头义务被记成"负资产"，
   修正净值 = 系统净值 − (期权真实市值 − |成本|)。
3. 保证金是下跌的反向变量：卖 put 虚值转实值后收内在价值全额，IO ×3.1、au ×2.4；
   义务仓结算价取 max(当前市值, 内在价值)，对风险侧保守。
4. breached 判定 = 交易所口径 >=1 或 净资产<=0；真实口径超 100% 是预警不是追保。
"""
import math
from datetime import date
from typing import Optional

from sqlalchemy import func

from server_fast.app.bds.models import OptionConfig, OptionInfo
from server_fast.app.bds.services.market_data import option_market, option_info
from server_fast.app.bills.models import (
    Group,
    GroupAcc,
    RiskParam,
    ScenarioGrid,
)
from server_fast.common.db import SessionLocal

ENGINE_VER = "v1.0"

# 情景跌幅档（0% 是"持有到期+标的不动"口径，put 作废赚回时间价值，非 bug）
STEPS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]

# 三种下跌模式：值=该组标的是否参与下跌
MODES = {
    "sync": {"csi300": 1, "csi500": 1, "csi500etf": 1, "astock": 1, "nasdaq": 1, "au": 1},
    "astock_only": {"csi300": 1, "csi500": 1, "csi500etf": 1, "astock": 1, "nasdaq": 0, "au": 0},
    "index_only": {"csi300": 1, "csi500": 1, "csi500etf": 1, "astock": 0, "nasdaq": 0, "au": 0},
}
MODE_NAMES = {
    "sync": "A 同步同幅",
    "astock_only": "B 仅A股（黄金/纳指不动）",
    "index_only": "C 仅股指（个股/黄金/纳指不动）",
}

# 期货品种 → 情景分组键（品种代码前两位）
FUT_UND_KEY = {"IC": "csi500", "IF": "csi300", "IH": "csi300", "IM": "csi500", "au": "au"}

# 证券代码 → 情景分组键（其余证券默认 astock）
SEC_UND_KEY = {"513100": "nasdaq", "510500": "csi500etf", "510300": "csi300", "159919": "csi300"}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S: float, K: float, T: float, sigma: float, r: float = 0.015):
    """欧式认沽 BS 价与 delta。T<=0 或 sigma<=0 退化为内在价值。"""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0), (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    px = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    return max(px, 0.0), -_norm_cdf(-d1)


def _load_params(db) -> dict:
    """读风险参数：{(scope, key): value}，同键取生效日 <= 今天中最新的一条。"""
    today = date.today()
    rows = (
        db.query(RiskParam)
        .filter(RiskParam.effective_on <= today)
        .order_by(RiskParam.scope, RiskParam.param_key, RiskParam.effective_on)
        .all()
    )
    out = {}
    for r in rows:
        out[(r.scope, r.param_key)] = float(r.param_value)  # 后写覆盖先写 => 取最新生效日
    return out


def load_context() -> dict:
    """读全部底数，返回计算上下文。

    持仓分类规则：
    - 期权：卖 put（p_long>0 且成本为负）→ 先用 `broker_symbol` 匹配账单码认领配置行，
      再从 `cfg.contract_id` join `bds_option_info` 取 strike/mult/expiry，从品种台账
      取 margin_type/und_key。缺行或规格不全直接 raise（宁可算不出，不可算错）。
      ⚠️ `bds_option_config` 自 2026-10-27 起**不再存任何规格**，只存「认领键 + 同步意图」。
    - 期货：品种前缀映射 und_key；保证金率从 risk_param scope='{交易所}.{品种}' 拿
    - 证券：SEC_UND_KEY 映射，默认 astock
    """
    db = SessionLocal()
    try:
        params = _load_params(db)

        # 账户：系统净值 = acc_aset − diff_br；fin = diff_br（融资余额）
        acc_rows = (
            db.query(GroupAcc)
            .filter(GroupAcc.account != "合计")  # 铁律 1.5
            .all()
        )
        accounts = {}
        for a in acc_rows:
            acc_aset = float(a.acc_aset or 0)
            diff_br = float(a.diff_br or 0)
            accounts[a.account] = {
                "system_net": acc_aset - diff_br,  # 净值 = 账户价值 − 融资余额（铁律 1.6）
                "fin": diff_br,
            }

        # 同步清单：账单原始码 → 配置行（只用来判断「这张合约要不要算」，不再承载规格）
        # ⚠️ key 必须用 broker_symbol（账单原始码）而不是标准码：
        #    bills_group.symbol 是账单口径（`CFFEX.IO2611-P-4000` / `SHQQ.10012562`），
        #    而标准码是 `IO2611-P-4000` / `P2611M06250`，直接比会一个都匹配不上。
        #    broker_symbol 为空的（手工新增的观察合约）退回字典的标准码兜底。
        cfg_rows = db.query(OptionConfig).all()
        # 规格一次查全：config.contract_id 已是唯一身份 ⇒ 不需要任何消歧，
        # 字典行就是那一行（此前按 symbol 取要先按 品种ID→标的 两级收窄，因为 ETF 同码）。
        cfg_info: dict[str, OptionInfo] = {}
        if cfg_rows:
            cfg_info = {
                r.contract_id: r
                for r in db.query(OptionInfo).filter(
                    OptionInfo.contract_id.in_({c.contract_id for c in cfg_rows})).all()
            }
        contracts: dict[str, OptionConfig] = {}
        for c in cfg_rows:
            info_row = cfg_info.get(c.contract_id)
            key = c.broker_symbol or (info_row.symbol if info_row else None)
            if key:
                contracts[key] = c

        # 在仓持仓（account='合计' 是聚合行，必须排除）
        pos_rows = (
            db.query(Group)
            .filter(Group.account != "合计", Group.p_total.isnot(None), Group.p_total != 0)
            .all()
        )

        positions = []
        for g in pos_rows:
            if g.account not in accounts:
                accounts[g.account] = {"system_net": 0.0, "fin": 0.0}
            qty = int(g.p_total)
            price = float(g.price or 0)
            # ⚠️ 必须用**前缀**匹配：库里 category 实值是「期权交易 / 期货交易 / 证券交易」，
            #    精确比 "期权" 会让 6 张期权 + 1 张 IC 期货全部落进证券分支，且**跑完不报错**
            #    （bills_scenario_grid 从落盘起一直是 0 行，2026-10-27 才查出这个原因）。
            if g.category.startswith("期权"):
                c = contracts.get(g.symbol)
                if c is None:
                    raise ValueError(
                        f"期权 {g.symbol} 不在同步清单里（bds_option_config），拒绝估算"
                    )
                entry = option_info.resolve(c.option_name) or {}
                spec = cfg_info.get(c.contract_id)
                if spec is None:
                    raise ValueError(
                        f"期权 {g.symbol}（contract_id={c.contract_id}）在合约字典"
                        f"（bds_option_info）里找不到："
                        f"请先刷新合约字典（批量同步勾选「期权合约字典」）再重算"
                    )
                # 保证金公式族与情景分组键都是**品种级**派生（同品种内所有合约一致），
                # 不该逐合约存一份 —— 2026-10-27 起 bds_option_config 已物理删掉这两列，
                # 唯一来源是品种台账 + margin_type_of()。
                mtype = option_market.margin_type_of(c.option_name)
                und_key = entry.get("und_key")
                if not mtype or not und_key:
                    raise ValueError(
                        f"期权 {g.symbol} 的品种「{c.option_name}」未在品种台账"
                        f"（Config.OPTION_VARIETIES）登记，无法确定保证金公式族/情景分组"
                    )
                positions.append({
                    "kind": "put", "acct": g.account, "name": g.symbol,
                    # 乘数与行权价**必须成对取自字典**：A 类是 10180 + 7.367，
                    # 与品种默认的 10000 + 7.367 混用会差 1.8% 市值。
                    "mult": int(spec.multiplier), "qty": qty, "K": float(spec.strike),
                    "cost": abs(float(g.cost_total or 0)),  # 卖 put 成本记负，取绝对值
                    "mtype": mtype, "und_key": und_key,
                    "expiry": spec.last_trade_date, "symbol": g.symbol,
                })
            elif g.category.startswith("期货"):
                root = g.symbol.split(".")[-1].rstrip("0123456789")
                und_key = FUT_UND_KEY.get(root, root)
                mrate = params.get((f"{g.symbol.split('.')[0]}.{root}", "margin_rate"))
                if mrate is None:
                    raise ValueError(f"期货 {g.symbol} 缺保证金率参数（bills_risk_param）")
                positions.append({
                    "kind": "fut", "acct": g.account, "name": g.symbol,
                    "mult": int(g.multiplier or 1), "qty": qty, "px": price,
                    "mrate": mrate, "und_key": und_key, "symbol": g.symbol,
                })
            else:  # 证券（股票/ETF）
                code = g.symbol.split(".")[-1]
                positions.append({
                    "kind": "stock", "acct": g.account, "name": g.symbol,
                    "mult": 1, "qty": qty, "px": price,
                    "und_key": SEC_UND_KEY.get(code, "astock"), "symbol": g.symbol,
                })

        # 期权标的现价：优先 risk_param (und_key, underlying_price)，
        # 否则找同 und_key 的多头持仓价（au 期货多头在场时可用）
        und_price = {}
        for p in positions:
            if p["kind"] != "put":
                continue
            up = params.get((p["und_key"], "underlying_price"))
            if up:
                und_price[p["und_key"]] = up
                continue
            for q in positions:
                if q["kind"] in ("fut", "stock") and q["und_key"] == p["und_key"] and q.get("px"):
                    und_price[p["und_key"]] = q["px"]
                    break
        for p in positions:
            if p["kind"] == "put":
                if p["und_key"] not in und_price:
                    raise ValueError(
                        f"{p['name']} 的标的（{p['und_key']}）缺现价参数"
                        f"（bills_risk_param: underlying_price）"
                    )
                p["S0"] = und_price[p["und_key"]]
                # 到期天数
                if p.get("expiry"):
                    p["days"] = max((p["expiry"] - date.today()).days, 0)
                else:
                    p["days"] = 30  # 无到期日时的保守默认
                p["sigma"] = params.get((p["und_key"], "vol"), 0.20)
                p["quote"] = params.get((p["symbol"], "quote"))  # 实际报价优先于 BS
                if p["mtype"] == "shex":
                    # shex 义务仓保证金里的期货保证金率：scope='{交易所}.{品种}'
                    und_code = p["name"].split(".")[-1].rstrip("0123456789PC")
                    p["fut_m_rate"] = params.get((f"{p['name'].split('.')[0]}.{und_code}",
                                                  "margin_rate"))
                    if p["fut_m_rate"] is None:
                        raise ValueError(
                            f"{p['name']} 缺期货保证金率参数"
                            f"（bills_risk_param: {p['name'].split('.')[0]}.{und_code}）"
                        )

        return {"params": params, "accounts": accounts, "positions": positions,
                "calc_date": date.today()}
    finally:
        db.close()


def put_price(p: dict) -> float:
    """期权现值：实际报价优先，否则 BS。"""
    if p.get("quote") is not None:
        return p["quote"]
    px, _ = bs_put(p["S0"], p["K"], p["days"] / 365.0, p["sigma"])
    return px


def put_margin(p: dict, S: float) -> float:
    """卖出认沽义务仓保证金。结算价取 max(当前市值, 内在价值)——交易所按结算价收，
    情景下跌后时间价值虽衰减，用较大者对风险侧保守。

    公式族（不可互套，见方案 §7.3）：
    - cffex: (结算价 + max(15%×S − 虚值, 7%×S)) × 100
    - etf:   (结算价 + max(12%×S − 虚值, 7%×S)) × 10000
    - shex:  结算价 + max(期货保证金 − 虚值额/2, 期货保证金×0.5)，期货保证金率取参数
    """
    strike, mult, qty = p["K"], p["mult"], p["qty"]
    settle = max(put_price(p), max(strike - S, 0.0))
    otm = max(S - strike, 0.0)
    if p["mtype"] == "cffex":
        per = (settle + max(0.15 * S - otm, 0.07 * S)) * mult
    elif p["mtype"] == "etf":
        per = (settle + max(0.12 * S - otm, 0.07 * S)) * mult
    else:  # shex
        fut_m = S * mult * p["fut_m_rate"]
        per = settle * mult + max(fut_m - otm * mult / 2, fut_m * 0.5)
    return per * qty


def run(ctx: dict, mode: str, drop: float) -> dict:
    """单模式单跌幅推演。mode 为 MODES 的 key。"""
    f = MODES[mode]
    accs = ctx["accounts"]
    loss = {a: 0.0 for a in accs}
    margin = {a: 0.0 for a in accs}
    detail = []
    for p in ctx["positions"]:
        mv0 = p["px"] * p["mult"] * p["qty"] if p["kind"] != "put" else put_price(p) * p["mult"] * p["qty"]
        if p["kind"] == "put":
            S = p["S0"] * (1 - drop * f[p["und_key"]])
            mv1 = max(p["K"] - S, 0.0) * p["mult"] * p["qty"]  # 到期口径：内在价值
            loss[p["acct"]] += mv1 - mv0
            m = put_margin(p, S)
        else:
            S = p["px"] * (1 - drop * f[p["und_key"]])
            mv1 = S * p["mult"] * p["qty"]
            loss[p["acct"]] += mv0 - mv1
            m = mv1 * p["mrate"] if p["kind"] == "fut" else 0.0
        margin[p["acct"]] += m
        detail.append({"name": p["name"], "acct": p["acct"], "kind": p["kind"],
                       "margin": m, "loss": (mv1 - mv0) if p["kind"] == "put" else (mv0 - mv1)})

    # 期权市值修正：净值 = 系统净值 − (期权真实市值 − |成本|)
    adj = {a: 0.0 for a in accs}
    for p in ctx["positions"]:
        if p["kind"] == "put":
            adj[p["acct"]] += put_price(p) * p["mult"] * p["qty"] - p["cost"]
    net0 = {a: accs[a]["system_net"] - adj[a] for a in accs}

    net_total = sum(net0.values())
    tot_loss = sum(loss.values())
    tot_margin = sum(margin.values())
    tot_net = net_total - tot_loss

    out = {
        "loss": tot_loss, "margin": tot_margin, "net": tot_net,
        "real": (tot_margin / tot_net if tot_net > 0 else None),
        "accounts": {},
        "detail": detail,
    }
    for a in accs:
        n = net0[a] - loss[a]
        eq = n + accs[a]["fin"]
        real = margin[a] / n if n > 0 else None
        exch = margin[a] / eq if eq > 0 else None
        # 空账户（无持仓、无保证金、净值为 0）不参与追保判定。
        # 🔴 既有缺陷：原来 n<=0 一律判 breached，而 bills_group_acc 里有 5 个从未
        # 使用过的账户（B01/B02/B03/M01/N01，acc_aset=0）⇒ 它们常驻 True，
        # 进而把 ALL 行的 `any(breached)` 也带成 True。实测 0% 跌幅下组合真实风险度
        # 只有 0.66、交易所口径也远未到 1，却被判「已追保」。
        is_idle = (margin[a] == 0 and n == 0)
        out["accounts"][a] = {
            "net": n, "eq": eq, "margin": margin[a], "real": real, "exch": exch,
            "breached": (not is_idle) and (n <= 0 or (exch is not None and exch >= 1.0)),
        }
    return out


def _first_call(ctx: dict, mode: str, acct: str) -> dict | None:
    """单账户追保临界点（交易所口径，二分逼近）。当前已超 1 则返回 drop=0。"""
    start = run(ctx, mode, 0.0)["accounts"][acct]
    if start["exch"] is None:
        return None
    if start["exch"] >= 1.0:
        return {"drop": 0.0, "loss": 0.0, "net": start["net"], "margin": start["margin"]}
    lo, hi = 0.0, 0.95
    for _ in range(60):
        mid = (lo + hi) / 2
        x = run(ctx, mode, mid)["accounts"][acct]
        if x["net"] <= 0 or (x["exch"] is not None and x["exch"] >= 1.0):
            hi = mid
        else:
            lo = mid
    d = (lo + hi) / 2
    x = run(ctx, mode, d)["accounts"][acct]
    return {"drop": d, "net": x["net"], "margin": x["margin"]}


def compute() -> dict:
    """全量计算，不写库。返回 board 接口的完整结构。"""
    ctx = load_context()
    net0 = {}
    adj = {a: 0.0 for a in ctx["accounts"]}
    for p in ctx["positions"]:
        if p["kind"] == "put":
            adj[p["acct"]] += put_price(p) * p["mult"] * p["qty"] - p["cost"]
    for a, v in ctx["accounts"].items():
        net0[a] = v["system_net"] - adj[a]
    net_total = sum(net0.values())

    base = run(ctx, "sync", 0.0)

    # 持仓明细（含 1% 敏感度与最大亏损）
    pos_detail = []
    for p in ctx["positions"]:
        if p["kind"] == "put":
            px = put_price(p)
            _, delta = bs_put(p["S0"], p["K"], p["days"] / 365.0, p["sigma"])
            d1 = -delta * p["S0"] * 0.01 * p["mult"] * p["qty"]
            max_loss = p["K"] * p["mult"] * p["qty"] - p["cost"]
            nominal = p["S0"] * p["mult"] * p["qty"]
        else:
            d1 = p["px"] * p["mult"] * p["qty"] * 0.01
            max_loss = p["px"] * p["mult"] * p["qty"]
            nominal = max_loss
        pos_detail.append({
            "acct": p["acct"], "symbol": p["name"], "kind": p["kind"],
            "und_key": p["und_key"], "qty": p["qty"], "nominal": round(nominal, 2),
            "max_loss": round(max_loss, 2), "drop1_loss": round(d1, 2),
            "margin_now": round(base["detail"][[d["name"] for d in base["detail"]].index(p["name"])]["margin"], 2),
        })

    # 情景矩阵（三模式 × 七档 × 分账户）
    grid = {}
    for mode in MODES:
        rows = {}
        for d in STEPS:
            r = run(ctx, mode, d)
            # 全局行也要有 exch（交易所口径风险度）。
            # 🔴 这是既有缺陷的修复：原来 ALL 行只给 real 不给 exch，而
            # `run_and_persist` 对每一行都取 x["exch"] ⇒ KeyError: 'exch'。
            # 只因 category 精确匹配缺陷让 compute() 从未产出过有效 grid，
            # 这条路径一直没被走到（2026-10-27 修好 category 后才暴露）。
            eq_total = sum(x["eq"] for x in r["accounts"].values())
            row = {"ALL": {
                "loss": round(r["loss"], 2), "net": round(r["net"], 2),
                "margin": round(r["margin"], 2),
                "real": r["real"],
                "exch": (r["margin"] / eq_total if eq_total > 0 else None),
                "breached": any(
                    x["breached"] for x in r["accounts"].values()),
            }}
            # 分账户 loss = net0[a] − 情景后净值（run 按账户返回净值，反推亏损）
            for a, x in r["accounts"].items():
                row[a] = {
                    "loss": round(net0[a] - x["net"], 2),
                    "net": round(x["net"], 2), "margin": round(x["margin"], 2),
                    "real": x["real"], "exch": x["exch"], "breached": x["breached"],
                }
            rows[str(d)] = row
        grid[mode] = rows

    # 首个追保点（交易所口径；账户隔离——资金不能互救）
    first_call = {}
    for mode in MODES:
        hits = []
        for a in ctx["accounts"]:
            fc = _first_call(ctx, mode, a)
            if fc is not None and fc["drop"] is not None:
                hits.append({"account": a, **{k: (round(v, 4) if k == "drop" else round(v, 2))
                                              for k, v in fc.items() if v is not None}})
        hits = [h for h in hits if h.get("drop") is not None]
        hits.sort(key=lambda h: h["drop"])
        first_call[mode] = hits[0] if hits else None

    # 账户卡（当前）
    acct_cards = {}
    for a in ctx["accounts"]:
        x = base["accounts"][a]
        cov = net0[a] / x["margin"] if x["margin"] > 0 else None
        acct_cards[a] = {
            "net0": round(net0[a], 2), "fin": round(ctx["accounts"][a]["fin"], 2),
            "eq": round(x["eq"], 2), "margin": round(x["margin"], 2),
            "real": x["real"], "exch": x["exch"],
            "coverage": (round(cov, 4) if cov is not None else None),  # 自有资金覆盖率
        }

    return {
        "engine_ver": ENGINE_VER,
        "calc_date": ctx["calc_date"].isoformat(),
        "net_total": round(net_total, 2),
        "margin_total": round(base["margin"], 2),
        "real_total": base["real"],
        "max_exposure": round(sum(p["max_loss"] for p in pos_detail), 2),
        "accounts": acct_cards,
        "positions": pos_detail,
        "grid": grid,
        "first_call": first_call,
        "mode_names": MODE_NAMES,
        "steps": STEPS,
    }


def run_and_persist() -> dict:
    """compute + UPSERT bills_scenario_grid（任务化重算入口，审计进 result）。"""
    board = compute()
    db = SessionLocal()
    try:
        n = 0
        for mode, rows in board["grid"].items():
            for d_str, row in rows.items():
                d = float(d_str)
                for acct, x in row.items():
                    obj = (
                        db.query(ScenarioGrid)
                        .filter(
                            ScenarioGrid.calc_date == board["calc_date"],
                            ScenarioGrid.mode == mode,
                            ScenarioGrid.drop_pct == d,
                            ScenarioGrid.account == acct,
                        )
                        .first()
                    )
                    vals = dict(
                        loss_amt=x["loss"], net_after=x["net"], margin_req=x["margin"],
                        risk_ratio_real=(x["real"] if x["real"] is not None else 9999),
                        risk_ratio_exch=(x["exch"] if x["exch"] is not None else 9999),
                        breached=1 if x["breached"] else 0,
                        engine_ver=board["engine_ver"],
                    )
                    if obj is None:
                        db.add(ScenarioGrid(
                            calc_date=board["calc_date"], mode=mode, drop_pct=d,
                            account=acct, **vals))
                    else:
                        for k, v in vals.items():
                            setattr(obj, k, v)
                    n += 1
        db.commit()
        return {"board": board, "rows_upserted": n}
    finally:
        db.close()
