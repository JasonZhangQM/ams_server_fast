# -*- coding: utf-8 -*-
"""持仓风险敞口与情景压力测试计算服务（v2.0，2026-10-28）。

方案：docs/持仓风险敞口与情景压力测试方案_v3_2026-10-28.md
数据基座：`bds_option_info`（合约规格）+ `bds_option_market`（行情日快照）。

三个入口：
- load_context():      读持仓/账户/合约字典/行情快照/保证金率，构造计算上下文
- compute():           全量计算（持仓明细 + 情景矩阵 + 首个追保点），不写库
- run_and_persist():   compute + UPSERT bills_scenario_grid（任务化重算，铁律 4.8）

五个口径（v2.1 修订，勿在未通读方案前改动）：

1. 风险度双口径：真实=保证金/净资产（扣融资），交易所=保证金/客户权益（含融资）；
   预警看真实口径，追保判定（breached/首个追保点）用交易所口径——期货公司只看它。
2. **净值按盯市价**：净值 = 系统净值 + Σ(方向 × 模型市值 − 系统市值)。
   盯市价 = `settle > price`（当日结算价优先；IO/ETF 无 settle 时退回最新价）。
   旧公式「市值 − |成本|」在 2026-10-27 市值重算后已失效，会重复扣减。
3. **保证金结算价另用一条链**：`settle > pre_settle > price`。
   交易所公式（ETF 12% / CFFEX 15% / SHFE 期货保证金）写的是「前结算价」，
   与盯市口径不是一回事，两者不可混用。
4. 保证金是下跌/上涨的反向变量：义务仓结算价取 max(盯市价, 内在价值)，对风险侧保守。
5. **方向取字典 `cp` + 账单成本符号**：负成本 = 义务仓。短 call 的下行「收益」是假象，
   必须有上行情景档才能度量风险；到期合约直接剔除，不按 days=0 硬算。

行情新鲜度：允许 T−1（收盘后同步），T−2 起拒绝计算并抛出明确错误。
"""
import math
from datetime import date
from typing import Optional

from sqlalchemy import func

from server_fast.app.bds.models import OptionConfig, OptionInfo, OptionMarket, TradeDate
from server_fast.app.bds.services.market_data import option_market, option_info
from server_fast.app.bills.models import (
    Group,
    GroupAcc,
    RiskParam,
    ScenarioGrid,
)
from server_fast.common.db import SessionLocal

ENGINE_VER = "v2.0"

# 情景档位：负数=上涨（短 call 的风险方向），0%=即期基准
UPSIDE_STEPS = [-0.20, -0.10, -0.05]
DOWNSIDE_STEPS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
STEPS = sorted(UPSIDE_STEPS + DOWNSIDE_STEPS)

# 三种下跌模式：值=该组标的是否参与情景（上涨档同样用这张表）
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

RISK_FREE_RATE = 0.015
# IV 反解失败时的兜底波动率（仅用于即期重估，不参与保证金）
DEFAULT_SIGMA = 0.20


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def intrinsic(S: float, K: float, cp: str) -> float:
    """期权内在价值：认购 max(S−K,0)，认沽 max(K−S,0)。"""
    return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)


def bs_price(S: float, K: float, T: float, sigma: float, cp: str,
             r: float = RISK_FREE_RATE) -> float:
    """欧式期权 BS 价。T<=0 或 sigma<=0 退化为内在价值。"""
    if T <= 0 or sigma <= 0:
        return intrinsic(S, K, cp)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if cp == "C":
        return max(S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2), 0.0)
    return max(K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1), 0.0)


def bs_delta(S: float, K: float, T: float, sigma: float, cp: str,
             r: float = RISK_FREE_RATE) -> float:
    """BS delta：认购 ∈(0,1)，认沽 ∈(−1,0)。退化时用 0/±1 阶跃。"""
    if T <= 0 or sigma <= 0:
        if cp == "C":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if cp == "C" else _norm_cdf(d1) - 1.0


def implied_vol(px: float, S: float, K: float, T: float, cp: str) -> Optional[float]:
    """由实际权利金反解 IV（二分法）。价格不合法/无法收敛返回 None，不猜。"""
    if px <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    lower = intrinsic(S, K, cp)
    upper = S if cp == "C" else K * math.exp(-RISK_FREE_RATE * T)
    if px <= lower + 1e-9 or px >= upper - 1e-9:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, mid, cp) > px:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _load_params(db) -> dict:
    """读风险参数：{(scope, key): value}，同键取生效日 <= 今天中最新的一条。

    v2.0 起只消费 `margin_rate`；quote/vol/underlying_price 已随 2026-10-28 清理删除。
    """
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


def _pos_px(*values) -> Optional[float]:
    """按顺序取第一个 >0 的价格（NULL 与 0 同视为无价）。"""
    for v in values:
        if v is not None and float(v) > 0:
            return float(v)
    return None


def _trade_date_window(db, today: date) -> tuple[date, date]:
    """返回 (最近交易日 ref, 允许的最早行情日 allowed_min)。

    - 今天是交易日：允许 T−1（收盘后同步）；ref=今天，allowed_min=上一交易日
    - 今天非交易日：ref=最近交易日，allowed_min=ref
    """
    recent = (
        db.query(TradeDate.trade_date)
        .filter(TradeDate.trade_date <= today)
        .order_by(TradeDate.trade_date.desc())
        .limit(2)
        .all()
    )
    if not recent:
        raise ValueError("bds_trade_date 日历为空，无法判定行情新鲜度")
    ref = recent[0][0]
    allowed_min = recent[1][0] if ref == today and len(recent) > 1 else ref
    return ref, allowed_min


def _stale_days(db, market_date: date, ref: date) -> int:
    """行情落后几个交易日（0=当日，1=T−1）。"""
    if market_date >= ref:
        return 0
    return int(db.query(func.count(TradeDate.trade_date)).filter(
        TradeDate.trade_date > market_date, TradeDate.trade_date <= ref).scalar() or 0)


def _load_latest_market(db, contract_ids: list[str]) -> dict[str, OptionMarket]:
    """每合约取最新一日快照；按 trade_date 升序后写覆盖先写。"""
    if not contract_ids:
        return {}
    rows = (
        db.query(OptionMarket)
        .filter(OptionMarket.contract_id.in_(contract_ids))
        .order_by(OptionMarket.contract_id, OptionMarket.trade_date)
        .all()
    )
    return {r.contract_id: r for r in rows}


def load_context() -> dict:
    """读全部底数，返回计算上下文。

    期权：
    - 账单码（`bills_group.symbol`）→ `bds_option_config.broker_symbol` → `contract_id`
    - 规格（cp/strike/multiplier/last_trade_date）只从 `bds_option_info` 取
    - 行情（settle/pre_settle/price/price_ud/delta_opt）只从 `bds_option_market` 取
    - 方向：`成本为负 = 义务仓`（券商把卖出开仓记在多头且成本为负，或记在空头）
    - 已到期（last_trade_date < today）：剔除并进 warnings，不参与推演
    """
    db = SessionLocal()
    try:
        today = date.today()
        params = _load_params(db)
        warnings: list[str] = []

        # 账户：系统净值 = acc_aset − diff_br；fin = diff_br（融资余额）
        acc_rows = db.query(GroupAcc).filter(GroupAcc.account != "合计").all()
        accounts = {}
        for a in acc_rows:
            acc_aset = float(a.acc_aset or 0)
            diff_br = float(a.diff_br or 0)
            accounts[a.account] = {
                "system_net": acc_aset - diff_br,  # 铁律 1.6
                "fin": diff_br,
            }

        # 同步清单：账单原始码 → 配置行（只判断「这张合约要不要算」，不承载规格）
        cfg_rows = db.query(OptionConfig).all()
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
        market = _load_latest_market(db, [c.contract_id for c in cfg_rows])

        pos_rows = (
            db.query(Group)
            .filter(Group.account != "合计", Group.p_total.isnot(None), Group.p_total != 0)
            .all()
        )

        positions = []
        expired: list[str] = []
        market_dates: set[date] = set()
        und_px: dict[str, float] = {}
        for g in pos_rows:
            if g.account not in accounts:
                accounts[g.account] = {"system_net": 0.0, "fin": 0.0}
            # ⚠️ 必须前缀匹配：库里实值是「期权交易 / 期货交易 / 证券交易」
            if g.category.startswith("期权"):
                p = _option_position(g, contracts, cfg_info, market, params,
                                     today, warnings)
                if p is None:
                    expired.append(g.symbol)
                    continue
                if p["und_key"] in und_px and abs(und_px[p["und_key"]] - p["S0"]) > 1e-6:
                    warnings.append(
                        f"{p['und_key']} 同日标的价不一致：{und_px[p['und_key']]} vs {p['S0']}")
                und_px.setdefault(p["und_key"], p["S0"])
                market_dates.add(p["market_date"])
                positions.append(p)
            elif g.category.startswith("期货"):
                root = g.symbol.split(".")[-1].rstrip("0123456789")
                und_key = FUT_UND_KEY.get(root, root)
                mrate = params.get((f"{g.symbol.split('.')[0]}.{root}", "margin_rate"))
                if mrate is None:
                    raise ValueError(f"期货 {g.symbol} 缺保证金率参数（bills_risk_param）")
                positions.append({
                    "kind": "fut", "acct": g.account, "name": g.symbol,
                    "mult": int(g.multiplier or 1), "qty": int(g.p_total),
                    "px": float(g.price or 0), "mrate": mrate,
                    "und_key": und_key, "symbol": g.symbol,
                })
            else:
                code = g.symbol.split(".")[-1]
                positions.append({
                    "kind": "stock", "acct": g.account, "name": g.symbol,
                    "mult": 1, "qty": int(g.p_total), "px": float(g.price or 0),
                    "und_key": SEC_UND_KEY.get(code, "astock"), "symbol": g.symbol,
                })

        # 新鲜度哨兵：所有在仓期权必须同一天，且不早于允许的最早行情日
        data_as_of = stale = None
        if market_dates:
            if len(market_dates) > 1:
                raise ValueError(
                    f"在仓期权的行情日期不一致：{sorted(market_dates)}，拒绝混日计算")
            data_as_of = market_dates.pop()
            ref, allowed_min = _trade_date_window(db, today)
            if data_as_of < allowed_min:
                raise ValueError(
                    f"期权行情过期：最新快照 {data_as_of}，允许不早于 {allowed_min}"
                    f"（T−1）。请先同步期权行情再重算")
            stale = _stale_days(db, data_as_of, ref)
            if stale == 1:
                warnings.append(f"期权行情为 T−1（{data_as_of}），按上一交易日快照计算")

        return {"params": params, "accounts": accounts, "positions": positions,
                "calc_date": today, "data_as_of": data_as_of, "stale_days": stale,
                "expired": expired, "warnings": warnings}
    finally:
        db.close()


def _option_position(g: Group, contracts: dict, cfg_info: dict, market: dict,
                     params: dict, today: date,
                     warnings: list[str]) -> Optional[dict]:
    """构造单个期权持仓。已到期返回 None（计入 expired）。"""
    c = contracts.get(g.symbol)
    if c is None:
        raise ValueError(f"期权 {g.symbol} 不在同步清单里（bds_option_config），拒绝估算")
    entry = option_info.resolve(c.option_name) or {}
    spec = cfg_info.get(c.contract_id)
    if spec is None:
        raise ValueError(
            f"期权 {g.symbol}（contract_id={c.contract_id}）在合约字典"
            f"（bds_option_info）里找不到：请先刷新合约字典再重算")
    mtype = option_market.margin_type_of(c.option_name)
    und_key = entry.get("und_key")
    if not mtype or not und_key:
        raise ValueError(
            f"期权 {g.symbol} 的品种「{c.option_name}」未在品种台账登记，"
            f"无法确定保证金公式族/情景分组")

    expiry = spec.last_trade_date
    if expiry < today:
        warnings.append(f"{g.symbol} 已于 {expiry} 到期，剔除出情景推演")
        return None

    mkt = market.get(c.contract_id)
    if mkt is None:
        raise ValueError(
            f"期权 {g.symbol}（contract_id={c.contract_id}）没有本地行情快照"
            f"（bds_option_market），拒绝用假设价估算")

    # 盯市价（净值口径）与保证金价（交易所公式口径）是两条不同的降级链
    px_equity = _pos_px(mkt.settle, mkt.price)
    px_margin = _pos_px(mkt.settle, mkt.pre_settle, mkt.price)
    if px_equity is None or px_margin is None:
        raise ValueError(f"期权 {g.symbol} 行情三列价格全空/全 0（{mkt.trade_date}）")
    S0 = _pos_px(mkt.price_ud)
    if S0 is None:
        raise ValueError(
            f"期权 {g.symbol} 的快照 {mkt.trade_date} 缺标的价 price_ud，"
            f"拒绝混用其它日期的标的价")

    # shex 义务仓保证金要用标的期货的保证金率（scope='{交易所}.{品种}'）
    fut_m_rate = None
    if mtype == "shex":
        und_code = g.symbol.split(".")[-1].rstrip("0123456789PC")
        fut_m_rate = params.get((f"{g.symbol.split('.')[0]}.{und_code}", "margin_rate"))
        if fut_m_rate is None:
            raise ValueError(
                f"{g.symbol} 缺期货保证金率参数"
                f"（bills_risk_param: {g.symbol.split('.')[0]}.{und_code}）")

    cp = (spec.cp or "P").upper()
    sign = -1 if float(g.cost_total or 0) < 0 else 1
    qty = abs(int(g.p_total))
    days = max((expiry - today).days, 0)
    T = days / 365.0
    sigma = implied_vol(px_equity, S0, float(spec.strike), T, cp)
    if sigma is None:
        sigma = DEFAULT_SIGMA
        warnings.append(f"{g.symbol} 权利金反解 IV 失败，改用默认 σ={DEFAULT_SIGMA:.0%}")
    delta = float(mkt.delta_opt) if mkt.delta_opt is not None else \
        bs_delta(S0, float(spec.strike), T, sigma, cp)

    return {
        "kind": "call" if cp == "C" else "put", "cp": cp, "sign": sign,
        "acct": g.account, "name": g.symbol, "symbol": g.symbol,
        "contract_id": c.contract_id, "mult": int(spec.multiplier), "qty": qty,
        "strike": float(spec.strike), "expiry": expiry, "days": days,
        "S0": S0, "sigma": sigma, "delta": delta,
        "px_equity": px_equity, "px_margin": px_margin,
        "fut_m_rate": fut_m_rate,
        "sys_value": float(g.value_total or 0),
        "mtype": mtype, "und_key": und_key, "market_date": mkt.trade_date,
    }


def _option_value(p: dict, S: float, horizon: str, iv_mult: float) -> float:
    """期权单位权利金：即期=BS 重估，到期=内在价值。"""
    if horizon == "expiry" or p["days"] <= 0:
        return intrinsic(S, p["strike"], p["cp"])
    return bs_price(S, p["strike"], p["days"] / 365.0, p["sigma"] * iv_mult, p["cp"])


def _option_margin(p: dict, S: float) -> float:
    """卖出期权义务仓保证金：结算价取 max(保证金价, 内在价值)。

    公式族（不可互套）：
    - cffex: (结算价 + max(15%×S − 虚值, 7%×S)) × mult
    - etf:   (结算价 + max(12%×S − 虚值, 7%×S)) × mult
    - shex:  结算价×mult + max(期货保证金 − 虚值额/2, 期货保证金×0.5)

    虚值方向随合约方向翻转：认购=K−S，认沽=S−K。
    """
    strike, mult = p["strike"], p["mult"]
    settle = max(p["px_margin"], intrinsic(S, strike, p["cp"]))
    otm = max(strike - S, 0.0) if p["cp"] == "C" else max(S - strike, 0.0)
    if p["mtype"] == "cffex":
        per = (settle + max(0.15 * S - otm, 0.07 * S)) * mult
    elif p["mtype"] == "etf":
        per = (settle + max(0.12 * S - otm, 0.07 * S)) * mult
    else:  # shex
        fut_m = S * mult * p["fut_m_rate"]
        per = settle * mult + max(fut_m - otm * mult / 2, fut_m * 0.5)
    return per * p["qty"]


def _net0(ctx: dict, horizon: str, iv_mult: float) -> dict[str, float]:
    """按盯市口径修正系统净值，逐账户。

    旧口径「− (市值 − |成本|)」已废止：2026-10-27 起 bills_group 已按市值入账，
    v2.0 只补「模型盯市价 − 系统市值」的差额（au 的 settle vs price 即属此类）。
    """
    net0 = {a: v["system_net"] for a, v in ctx["accounts"].items()}
    for p in ctx["positions"]:
        if p["kind"] not in ("call", "put"):
            continue
        v = p["sign"] * _option_value(p, p["S0"], horizon, iv_mult) * p["mult"] * p["qty"]
        net0[p["acct"]] += v - p["sys_value"]
    return net0


def run(ctx: dict, mode: str, drop: float, horizon: str = "now",
        iv_mult: float = 1.0) -> dict:
    """单模式单情景推演。drop<0 表示上涨；horizon∈{now, expiry}。"""
    f = MODES[mode]
    accs = ctx["accounts"]
    net0 = _net0(ctx, horizon, iv_mult)
    loss = {a: 0.0 for a in accs}
    margin = {a: 0.0 for a in accs}
    detail = []
    for p in ctx["positions"]:
        shift = drop * f.get(p["und_key"], 0)
        if p["kind"] in ("call", "put"):
            S = p["S0"] * (1 - shift)
            v0 = p["sign"] * _option_value(p, p["S0"], horizon, iv_mult) * p["mult"] * p["qty"]
            v1 = p["sign"] * _option_value(p, S, horizon, iv_mult) * p["mult"] * p["qty"]
            d_loss = v0 - v1
            m = _option_margin(p, S) if p["sign"] < 0 else 0.0
        else:
            S = p["px"] * (1 - shift)
            v0 = p["px"] * p["mult"] * p["qty"]
            v1 = S * p["mult"] * p["qty"]
            d_loss = v0 - v1
            m = abs(v1) * p["mrate"] if p["kind"] == "fut" else 0.0
        loss[p["acct"]] += d_loss
        margin[p["acct"]] += m
        detail.append({"name": p["name"], "acct": p["acct"], "kind": p["kind"],
                       "mtype": p.get("mtype"), "und_key": p.get("und_key"),
                       "margin": m, "loss": d_loss})

    net_total = sum(net0.values())
    tot_loss = sum(loss.values())
    tot_margin = sum(margin.values())
    tot_net = net_total - tot_loss

    out = {
        "loss": tot_loss, "margin": tot_margin, "net": tot_net,
        "real": (tot_margin / tot_net if tot_net > 0 else None),
        "accounts": {}, "detail": detail,
    }
    for a in accs:
        n = net0[a] - loss[a]
        eq = n + accs[a]["fin"]
        real = margin[a] / n if n > 0 else None
        exch = margin[a] / eq if eq > 0 else None
        # 空账户（无持仓、无保证金、净值为 0）不参与追保判定，否则 ALL 行被带成 True
        is_idle = (margin[a] == 0 and n == 0)
        out["accounts"][a] = {
            "net": n, "eq": eq, "margin": margin[a], "real": real, "exch": exch,
            "breached": (not is_idle) and (n <= 0 or (exch is not None and exch >= 1.0)),
        }
    return out


def _breach_at(ctx: dict, mode: str, acct: str, drop: float,
               horizon: str, iv_mult: float) -> dict:
    return run(ctx, mode, drop, horizon, iv_mult)["accounts"][acct]


def _first_call_direction(ctx: dict, mode: str, acct: str, direction: int,
                          horizon: str, iv_mult: float) -> Optional[dict]:
    """单方向追保临界点。direction=+1 下跌，−1 上涨；二分逼近。"""
    def is_breached(u: float) -> bool:
        x = _breach_at(ctx, mode, acct, direction * u, horizon, iv_mult)
        # 只认 run() 的 breached 判定：空账户（n=0、margin=0）已被 is_idle 豁免，
        # 这里若直接判 net<=0 会把 B01/B02 等从未使用的账户钉在 drop=0。
        return x["breached"]

    if is_breached(0.0):
        return {"drop": 0.0, "direction": "now"}
    if not is_breached(0.95):
        return None
    lo, hi = 0.0, 0.95
    for _ in range(50):
        mid = (lo + hi) / 2
        if is_breached(mid):
            hi = mid
        else:
            lo = mid
    u = (lo + hi) / 2
    x = _breach_at(ctx, mode, acct, direction * u, horizon, iv_mult)
    return {"drop": direction * u, "direction": "down" if direction > 0 else "up",
            "net": x["net"], "margin": x["margin"]}


def _first_call(ctx: dict, mode: str, acct: str,
                horizon: str = "now", iv_mult: float = 1.0) -> Optional[dict]:
    """单账户追保临界点：同时扫描下跌与上涨，取离 0 更近的一侧。"""
    cands = [
        c for c in (
            _first_call_direction(ctx, mode, acct, 1, horizon, iv_mult),
            _first_call_direction(ctx, mode, acct, -1, horizon, iv_mult),
        ) if c is not None
    ]
    if not cands:
        return None
    if cands[0]["direction"] == "now":
        return cands[0]
    return min(cands, key=lambda c: abs(c["drop"]))


def compute(horizon: str = "now", iv_mult: float = 1.0,
            include_detail: bool = False) -> dict:
    """全量计算，不写库。horizon 默认即期（0% 情景 = 当前净值）。

    include_detail=True 时每个情景的 ALL 行额外带 `detail[]`（逐仓保证金/亏损），
    仅供前端保证金结构图使用；默认关闭以控制响应体大小。
    """
    ctx = load_context()
    net0 = _net0(ctx, horizon, iv_mult)
    base = run(ctx, "sync", 0.0, horizon, iv_mult)

    # 持仓明细：1% 敏感度、名义市值、最大亏损
    margin_now = {(d["acct"], d["name"]): d["margin"] for d in base["detail"]}
    pos_detail = []
    unbounded = False
    for p in ctx["positions"]:
        if p["kind"] in ("call", "put"):
            v0 = abs(_option_value(p, p["S0"], horizon, iv_mult)) * p["mult"] * p["qty"]
            d1 = p["sign"] * p["delta"] * 0.01 * p["S0"] * p["mult"] * p["qty"]
            if p["sign"] < 0:
                if p["cp"] == "C":
                    max_loss, unbounded = None, True  # 卖认购理论上行无上限
                else:
                    max_loss = max(p["strike"] * p["mult"] * p["qty"] - v0, 0.0)
            else:
                max_loss = v0
            nominal = p["S0"] * p["mult"] * p["qty"]
            extra = {"cp": p["cp"], "side": "short" if p["sign"] < 0 else "long",
                     "strike": p["strike"], "expiry": p["expiry"].isoformat(),
                     "days": p["days"], "px": round(p["px_equity"], 4),
                     "sigma": round(p["sigma"], 4), "delta": round(p["delta"], 4)}
        else:
            px, mult, qty = p["px"], p["mult"], p["qty"]
            d1 = px * mult * qty * 0.01
            max_loss = abs(px * mult * qty)
            nominal = max_loss
            extra = {}
        pos_detail.append({
            "acct": p["acct"], "symbol": p["name"], "kind": p["kind"],
            "und_key": p["und_key"], "qty": p["qty"], "nominal": round(nominal, 2),
            "max_loss": (round(max_loss, 2) if max_loss is not None else None),
            "drop1_loss": round(d1, 2),
            "margin_now": round(margin_now.get((p["acct"], p["name"]), 0.0), 2),
            **extra,
        })

    # 情景矩阵：三模式 × (上涨三档 + 下跌七档) × 分账户
    grid = {}
    for mode in MODES:
        rows = {}
        for d in STEPS:
            r = run(ctx, mode, d, horizon, iv_mult)
            eq_total = sum(x["eq"] for x in r["accounts"].values())
            row = {"ALL": {
                "loss": round(r["loss"], 2), "net": round(r["net"], 2),
                "margin": round(r["margin"], 2), "real": r["real"],
                "exch": (r["margin"] / eq_total if eq_total > 0 else None),
                "breached": any(x["breached"] for x in r["accounts"].values()),
                "detail": ([{**d, "margin": round(d["margin"], 2),
                             "loss": round(d["loss"], 2)} for d in r["detail"]]
                           if include_detail else None),
            }}
            for a, x in r["accounts"].items():
                row[a] = {
                    "loss": round(net0[a] - x["net"], 2),
                    "net": round(x["net"], 2), "margin": round(x["margin"], 2),
                    "real": x["real"], "exch": x["exch"], "breached": x["breached"],
                }
            rows[str(d)] = row
        grid[mode] = rows

    # 首个追保点（分账户隔离；同时看下跌与上涨）
    first_call = {}
    first_call_all = {}
    for mode in MODES:
        hits = []
        per_acct = {}
        for a in ctx["accounts"]:
            fc = _first_call(ctx, mode, a, horizon, iv_mult)
            if fc is None:
                per_acct[a] = None
                continue
            hit = {"account": a, **{
                k: (round(v, 4) if k == "drop" else (round(v, 2) if isinstance(v, float) else v))
                for k, v in fc.items()}}
            per_acct[a] = hit
            hits.append(hit)
        hits.sort(key=lambda h: abs(h["drop"]))
        first_call[mode] = hits[0] if hits else None
        first_call_all[mode] = per_acct

    acct_cards = {}
    for a in ctx["accounts"]:
        x = base["accounts"][a]
        cov = net0[a] / x["margin"] if x["margin"] > 0 else None
        acct_cards[a] = {
            "net0": round(net0[a], 2), "fin": round(ctx["accounts"][a]["fin"], 2),
            "eq": round(x["eq"], 2), "margin": round(x["margin"], 2),
            "real": x["real"], "exch": x["exch"],
            "coverage": (round(cov, 4) if cov is not None else None),
        }

    net_total = sum(net0.values())
    return {
        "engine_ver": ENGINE_VER,
        "calc_date": ctx["calc_date"].isoformat(),
        "data_as_of": (ctx["data_as_of"].isoformat() if ctx["data_as_of"] else None),
        "stale_days": ctx["stale_days"],
        "horizon": horizon,
        "iv_mult": iv_mult,
        "net_total": round(net_total, 2),
        "margin_total": round(base["margin"], 2),
        "real_total": base["real"],
        "max_exposure": round(sum(p["max_loss"] for p in pos_detail
                                  if p["max_loss"] is not None), 2),
        "max_exposure_unbounded": unbounded,
        "accounts": acct_cards,
        "positions": pos_detail,
        "grid": grid,
        "first_call": first_call,
        "first_call_all": first_call_all,
        "mode_names": MODE_NAMES,
        "steps": STEPS,
        "upside_steps": UPSIDE_STEPS,
        "warnings": ctx["warnings"] + ([
            "期权行情为 T−1，请收盘后同步后再看一次"] if ctx["stale_days"] == 1 else []),
    }


def expire_configs() -> list[str]:
    """把已到期合约的 `is_enabled` 置 0（到期是客观事实，不必人工记得关）。

    只处理「配置里启用 + 字典已到期」的行；返回被停用的合约 ID。
    """
    today = date.today()
    with SessionLocal() as db:
        rows = (
            db.query(OptionConfig)
            .join(OptionInfo, OptionInfo.contract_id == OptionConfig.contract_id)
            .filter(OptionInfo.last_trade_date < today, OptionConfig.is_enabled == 1)
            .all()
        )
        ids = [r.contract_id for r in rows]
        for r in rows:
            r.is_enabled = 0
        if ids:
            db.commit()
    return ids


def run_and_persist() -> dict:
    """compute + UPSERT bills_scenario_grid（任务化重算入口，审计进 result）。"""
    disabled = expire_configs()
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
        return {"board": board, "rows_upserted": n, "disabled_contracts": disabled}
    finally:
        db.close()
