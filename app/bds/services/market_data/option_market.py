# -*- coding: utf-8 -*-
"""bds 期权行情同步服务（`bds_option_market`，配置驱动）。

设计要点见 `docs/bds期权行情表与查询页需求方案_2026-10-26.md`：

1. **配置驱动**：读 `bds_option_config` 的启用行，按 `(option_name, end_month)` 分组调接口。
   没有这张配置表，"一键同步配置的全部期权"无处落地（持仓表是静态属性、监测表是全量铺开）。
2. **接口只给「品种×到期月」全量**，没有单合约查询 ⇒ 实现上必然是「拉整批 → 按配置过滤」。
   好处是以后往清单加同月合约不用重新请求。
3. **按日快照**：唯一键 `(symbol, trade_date)`，同日重跑幂等覆盖、跨日自然成为时间序列。
4. **三列价格分开**：`price`（最新/收盘）、`pre_settle`（前结）、`settle`（结算价，仅商品期权可得）。
   `irs_option_monitor` 把三者挤在一列，导致 au 存收盘价 11.50 而真实结算价 12.18。
5. **降级链只在本文件一处**：结算价缺失时下游按 `COALESCE(settle, pre_settle, price)` 降级。
   展示层如实留空——拿前结价冒充结算价会误导。

三个 akshare 接口的能力差异（实测 2026-10-26，这是表设计的第一约束）：

| 数据源 | 结算价 | delta | 成交量/持仓量 | 备注 |
|---|---|---|---|---|
| `option_finance_board`（IO） | ❌ | ❌ | ✅ volume / position | `lastprice` 盘中≠结算价 |
| `option_finance_board`（510500） | ❌（仅前结价） | ❌ | ❌（「数量」列恒等于行数） | 带完整时间戳 |
| `option_hist_shfe`（au） | ✅ | ✅（德尔塔） | ✅ | 一次返回全部到期月 |
"""
import logging
import re
from datetime import date, datetime, time as dtime
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from server_fast.common.db import SessionLocal
from server_fast.app.bds.models import OptionConfig, OptionMarket, TradeDate
from server_fast.app.irs.config import Config as IrsCfg

logger = logging.getLogger("uvicorn.error")

# 上期所商品期权合约：au2612P840 = 标的au + 月份2612 + C/P + 行权价840
_SHFE_CODE_RE = re.compile(r"^(?P<prod>[a-zA-Z]+)(?P<ym>\d{4})(?P<cp>[CP])(?P<k>\d+(?:\.\d+)?)$")
# 上交所/深交所 ETF 期权：510500P2611M06250 → 交易所标准码 P2611M06250
_ETF_CODE_RE = re.compile(r"^\d{6}(?P<code>[CP]\d{4}M\d{5})$")

# 无效数值的统一取值集合（akshare 各接口用空串/'-'/'nan' 表示缺失，不统一）
_BAD_TOKENS = {"", "-", "--", "None", "nan", "NaN", "null"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _dec(v) -> Optional[Decimal]:
    """把 akshare 的空串/'-'/'nan' 统一归一成 None，其余转 Decimal。

    不吞异常以外的判断：无法解析的串直接返回 None（由调用方决定跳过还是降级），
    避免 `Decimal('')` 之类抛 InvalidOperation 打断整批。
    """
    if v is None:
        return None
    s = str(v).strip()
    if s in _BAD_TOKENS:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _int(v) -> Optional[int]:
    d = _dec(v)
    return None if d is None else int(d)


def normalize_symbol(sym: str) -> str:
    """把各家代码归一成「交易所标准期权代码」。

    - `CFFEX.IO2611-P-4000` → `IO2611-P-4000`（账单前缀可去）
    - `SHEX.au2612P840`     → `au2612P840`（账单前缀可去）
    - `510500P2611M06250`   → `P2611M06250`（akshare 带回标的代码前缀）
    - `SHQQ.10012562`       → **原样返回**：券商内部码与交易所码无任何字符串关系，
      去前缀只会得到一个既不是券商码也不是交易所码的垃圾串。
    """
    if not sym:
        return ""
    s = str(sym).strip()
    if "." in s:
        head, tail = s.split(".", 1)
        # SHQQ/SZQQ 是券商柜台代码，不能靠去前缀得到交易所码
        if head.upper() not in ("SHQQ", "SZQQ"):
            s = tail
    m = _ETF_CODE_RE.match(s)
    return m.group("code") if m else s


def _calc_delisted_date(end_month: str, rule: str) -> date:
    """转发 `option_monitor._calc_delisted_date`（唯一权威算法，禁止二次实现）。

    懒导入的理由：`option_monitor` 模块顶层 `from gm.api import *`，把它提到本模块
    顶层会让 bds 服务在导入期就拉起 gm SDK。算法本身只有一处定义，这里不复制。
    """
    from server_fast.app.irs.service.option_monitor import _calc_delisted_date as impl
    return impl(end_month, rule)


def _resolve_trade_date(session, closed_only: bool) -> date:
    """判定本次行情属于哪个交易日。

    - `closed_only=True`（商品期权）：`option_hist_shfe` 是**日频结算数据**，必须收盘后
      （≥15:00）才有当日数据，否则取上一交易日。
    - `closed_only=False`（股指/ETF）：`option_finance_board` 返回**实时盘口**，
      开盘（≥09:30）后即为当日行情；盘前拿到的是昨收，标成今天会失鲜。
    """
    now = datetime.now()
    today = now.date()
    is_trade_day = session.query(TradeDate.trade_date).filter(
        TradeDate.trade_date == today).first() is not None
    if is_trade_day:
        if closed_only and now.time() >= dtime(15, 0):
            return today
        if not closed_only and now.time() >= dtime(9, 30):
            return today
    prev = session.query(TradeDate.trade_date).filter(
        TradeDate.trade_date < today).order_by(TradeDate.trade_date.desc()).first()
    if prev is None:
        raise ValueError("bds_trade_date 日历中找不到可用交易日")
    return prev[0]


# ---------------------------------------------------------------------------
# 标的价（降级链唯一入口）
# ---------------------------------------------------------------------------

def _fetch_underlying_price(und_symbol: str, und_key: str) -> Optional[Decimal]:
    """取标的现价。三条通道依次降级，全部失败返回 None（不阻断整批）。

    1. **gm `current`** —— 系统标准通道（`index_realtime.py` / `option_monitor.py` 同款）
    2. **新浪 hq.sinajs.cn** —— gm token 失效或终端未启动时的兜底；东财接口在本机被代理拦截，
       实测 `stock_zh_index_spot_em` / `fund_etf_spot_em` 均 ProxyError，故不用
    3. 返回 None —— 上游把 `price_ud`/`value_i`/`value_t` 留空，**不猜**
    """
    # ① gm
    try:
        from gm.api import current
        from server_fast.common.utils import call_with_timeout
        data = call_with_timeout(current, timeout=10)([und_symbol], fields=["symbol", "price"])
        if data and data[0].get("price") is not None:
            return _dec(data[0]["price"])
    except Exception as e:  # noqa: BLE001 —— 任何异常都应降级，不能中断同步
        logger.warning("[option_market] gm 取标的价失败 %s: %s", und_symbol, e)

    # ② 新浪
    try:
        import requests
        code = _sina_code(und_symbol, und_key)
        if not code:
            return None
        r = requests.get("https://hq.sinajs.cn/list=" + code,
                         headers={"Referer": "https://finance.sina.com.cn"}, timeout=8)
        r.encoding = "gbk"
        m = re.search(r'"([^"]*)"', r.text)
        if not m or not m.group(1):
            return None
        parts = m.group(1).split(",")
        # 期货（nf_）格式：名称,时间,开,高,低,现价,...  ⇒ 下标 5
        # 股票/指数格式：名称,今开,昨收,现价,...          ⇒ 下标 3
        idx = 5 if code.startswith("nf_") else 3
        if len(parts) <= idx:
            return None
        return _dec(parts[idx])
    except Exception as e:  # noqa: BLE001
        logger.warning("[option_market] 新浪取标的价失败 %s: %s", und_symbol, e)
    return None


def _sina_code(und_symbol: str, und_key: str) -> Optional[str]:
    """标的代码 → 新浪 hq 代码。"""
    if und_key == "csi300":
        return "sh000300"
    if und_key == "csi500etf":
        return "sh510500"
    if und_key == "au":
        # SHFE.au2612 → nf_AU2612
        tail = und_symbol.split(".", 1)[-1]
        m = re.match(r"^[a-zA-Z]+(\d{4})$", tail)
        return f"nf_AU{m.group(1)}" if m else None
    return None


# ---------------------------------------------------------------------------
# 单组同步
# ---------------------------------------------------------------------------

def _variety_meta(option_name: str) -> dict:
    """从 irs 配置取品种元信息（乘数 / 到期规则 / 品种类型）。"""
    for item in IrsCfg.OPTIONS_MARCH:
        if item["option_name"] == option_name:
            return item
    raise ValueError(f"未知期权品种（不在 IrsCfg.OPTIONS_MARCH 中）: {option_name}")


# 品种类型 → 保证金公式族。三套公式不可互套（MEMORY 铁律），故存「族」不存费率。
# 为什么按品种派生而不是逐合约存：同一品种内所有合约的公式族完全一致
# （实测 csi300→cffex×3、csi500etf→etf×2、au→shex），它是品种属性不是合约属性。
MARGIN_TYPE_BY_OPTION_TYPE = {"股指期权": "cffex", "ETF期权": "etf", "商品期权": "shex"}


def margin_type_of(option_name: str) -> Optional[str]:
    """akshare 品种名 → 保证金公式族（cffex/etf/shex）。未知品种返回 None。"""
    try:
        meta = _variety_meta(option_name)
    except ValueError:
        return None
    return MARGIN_TYPE_BY_OPTION_TYPE.get(meta["option_type"])


def _sync_group(session, option_name: str, end_month: str,
                cfgs: list[OptionConfig]) -> dict:
    """同步一个 (品种, 到期月) 分组。返回本组统计。"""
    import akshare as ak

    meta = _variety_meta(option_name)
    is_commodity = meta["option_type"] == "商品期权"
    multiplier = int(meta["multiplier"])
    rule = meta["rule_exercise_date"]

    trade_date = _resolve_trade_date(session, closed_only=is_commodity)

    if is_commodity:
        df = ak.option_hist_shfe(symbol=option_name, trade_date=trade_date.strftime("%Y%m%d"))
        source = "akshare_shfe"
    else:
        if not end_month or end_month == "ALL":
            raise ValueError(f"{option_name} 必须在配置里指定 end_month（接口按到期月拉取）")
        df = ak.option_finance_board(symbol=option_name, end_month=end_month)
        source = "akshare_board"

    if df is None or df.empty:
        return {"rows": 0, "insert": 0, "update": 0, "skip": 0,
                "note": f"{option_name}/{end_month} 接口返回空"}

    # 配置清单：交易所标准码 → 配置行（过滤用，同时提供 broker_symbol）
    wanted: dict[str, OptionConfig] = {}
    for c in cfgs:
        wanted[normalize_symbol(c.symbol)] = c

    # 标的价：同 und_key 只取一次（au 按合约月份各取一次）
    ud_cache: dict[str, Optional[Decimal]] = {}

    def _ud(und_symbol: str, key: str) -> Optional[Decimal]:
        if und_symbol not in ud_cache:
            ud_cache[und_symbol] = _fetch_underlying_price(und_symbol, key)
        return ud_cache[und_symbol]

    parsed: list[dict] = []
    skip_reasons: dict[str, int] = {}

    def _bump(reason: str):
        """记一次跳过（价格 0/空、代码格式不符、解析异常）。

        计数只按原因累计，总数在收尾时求和——避免两处各数一次导致对不上。
        """
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    for _, row in df.iterrows():
        try:
            if is_commodity:
                raw = str(row["合约代码"]).strip()
                m = _SHFE_CODE_RE.match(raw)
                if not m:
                    _bump("合约代码格式不符")
                    continue
                code = raw
                ym = m.group("ym")
                cp = m.group("cp")
                strike = _dec(m.group("k"))
                und_symbol = f"{meta['underlying_symbol']}{ym}"  # SHFE.au2612
                em = f"20{ym}"
                price = _dec(row.get("收盘价"))
                pre_settle = _dec(row.get("前结算价"))
                settle = _dec(row.get("结算价"))
                delta = _dec(row.get("德尔塔"))
                volume = _int(row.get("成交量"))
                oi = _int(row.get("持仓量"))
                quote_time = None
            elif option_name == "沪深300股指期权":
                code = str(row["instrument"]).strip()      # IO2611-P-4000
                parts = code.split("-")
                if len(parts) != 3:
                    _bump("合约代码格式不符")
                    continue
                ym = parts[0][-4:]                          # IO2611 → 2611
                cp = parts[1]
                strike = _dec(parts[2])
                und_symbol = meta["underlying_symbol"]
                em = f"20{ym}"
                price = _dec(row.get("lastprice"))
                pre_settle = None
                settle = None
                delta = None
                volume = _int(row.get("volume"))
                oi = _int(row.get("position"))
                quote_time = None
            else:  # ETF 期权（510500 等）
                raw = str(row["合约交易代码"]).strip()      # 510500P2611M06250
                code = normalize_symbol(raw)                # P2611M06250
                if not _ETF_CODE_RE.match(raw):
                    _bump("合约代码格式不符")
                    continue
                cp = code[0]
                ym = code[1:5]
                strike = _dec(row.get("行权价"))
                und_symbol = meta["underlying_symbol"]
                em = f"20{ym}"
                price = _dec(row.get("当前价"))
                pre_settle = _dec(row.get("前结价"))
                settle = None
                delta = None
                # 「数量」列恒等于返回行数（实测 26 行 → 全为 26），不是成交量，宁缺勿造
                volume, oi = None, None
                quote_time = _parse_quote_time(row.get("日期"))

            # 只保留配置清单里的合约
            if code not in wanted:
                continue
            # 0 值/空值拦截：盘前点同步会把价格写成 0
            if price is None or price <= 0:
                _bump("价格为空或<=0")
                continue
            if strike is None or strike <= 0:
                _bump("行权价为空或<=0")
                continue

            cfg = wanted[code]
            option_type = "call" if cp.upper() == "C" else "put"
            delisted = _calc_delisted_date(em, rule)
            ud = _ud(und_symbol, cfg.und_key)

            value_i = None
            value_t = None
            if ud is not None:
                intrinsic = (strike - ud) if option_type == "put" else (ud - strike)
                value_i = intrinsic if intrinsic > 0 else Decimal("0")
                tv = price - value_i
                value_t = tv if tv > 0 else Decimal("0")

            parsed.append(dict(
                symbol=code,
                broker_symbol=cfg.broker_symbol,
                trade_date=trade_date,
                underlying_symbol=cfg.underlying_symbol,
                und_key=cfg.und_key,
                option_type=option_type,
                price_strike=strike,
                delisted_date=delisted,
                days_left=(delisted - trade_date).days,
                multiplier=multiplier,
                price=price,
                pre_settle=pre_settle,
                settle=settle,
                price_ud=ud,
                value_i=value_i,
                value_t=value_t,
                delta_opt=delta,
                volume=volume,
                open_interest=oi,
                quote_time=quote_time,
                source=source,
                sync_time=datetime.now(),
            ))
        except Exception as e:  # noqa: BLE001 —— 单行失败不中断整批
            _bump("解析异常")
            logger.warning("[option_market] 单行解析失败 %s: %s", option_name, e)

    # ---- UPSERT（唯一键 symbol + trade_date）----
    insert = update = 0
    spec_filled = 0
    if parsed:
        symbols = [p["symbol"] for p in parsed]
        exist = {
            r.symbol: r for r in session.query(OptionMarket).filter(
                OptionMarket.trade_date == trade_date,
                OptionMarket.symbol.in_(symbols)).all()
        }
        for p in parsed:
            cur = exist.get(p["symbol"])
            if cur is None:
                session.add(OptionMarket(**p))
                insert += 1
            else:
                for k, v in p.items():
                    setattr(cur, k, v)
                update += 1

        # ---- 顺手回填合约规格（只填空值，绝不覆盖已有值）----
        # 清单行允许只填代码与品种（观察型合约），规格在这里按接口原文补齐——
        # 比人工填可靠：au2612P840 的到期日曾手填成 11-23，权威算法给的是 11-24。
        # 反过来，已有值可能是人工**修正**过的，所以只在为 None 时写。
        mt = MARGIN_TYPE_BY_OPTION_TYPE.get(meta["option_type"])
        for p in parsed:
            cfg = wanted.get(p["symbol"])
            if cfg is None:
                continue
            touched = False
            if cfg.cp is None:
                cfg.cp = "C" if p["option_type"] == "call" else "P"
                touched = True
            if cfg.strike is None and p["price_strike"] is not None:
                cfg.strike = p["price_strike"]
                touched = True
            if cfg.multiplier is None and p["multiplier"] is not None:
                cfg.multiplier = p["multiplier"]
                touched = True
            if cfg.expiry_date is None and p["delisted_date"] is not None:
                cfg.expiry_date = p["delisted_date"]
                touched = True
            if cfg.margin_type is None and mt:
                cfg.margin_type = mt
                touched = True
            if touched:
                spec_filled += 1
        if spec_filled:
            logger.info("[option_market] %s/%s 回填合约规格 %d 条",
                        option_name, end_month, spec_filled)
        session.commit()

    stat = {"rows": len(parsed), "insert": insert, "update": update,
            "skip": sum(skip_reasons.values()), "spec_filled": spec_filled}
    if skip_reasons:
        stat["skip_reasons"] = skip_reasons
    return stat


def _parse_quote_time(v) -> Optional[datetime]:
    """ETF 接口「日期」列是完整时间戳 `20261026154253`。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s.isdigit():
        return None
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def build_sync_plan() -> dict:
    """同步前置信息：启用张数、品种×到期月组合数、涉及品种。"""
    with SessionLocal() as session:
        rows = session.query(OptionConfig).filter(OptionConfig.is_enabled == 1).all()
        groups = {(r.option_name, r.end_month or "") for r in rows}
        return {
            "symbol_count": len(rows),
            "group_count": len(groups),
            "option_names": sorted({r.option_name for r in rows}),
            "groups": [{"option_name": n, "end_month": m} for n, m in sorted(groups)],
        }


def sync_one_group(option_name: str, end_month: str) -> int:
    """同步单个「品种 × 到期月」分组，返回写入行数（新增+更新）；失败返回 -1。

    供统一同步任务（`POST /bds/sync/macro` 的 option_market 段）**按组报进度**使用。
    与 `sync_option_market()` 共用同一个 `_sync_group`，口径完全一致——区别只是把
    「循环遍历分组」搬到调用方（任务线程），这样每组都能单独计一次进度。
    """
    try:
        with SessionLocal() as session:
            rows = session.query(OptionConfig).filter(
                OptionConfig.is_enabled == 1,
                OptionConfig.option_name == option_name,
            ).all()
            cfgs = [r for r in rows if (r.end_month or "") == (end_month or "")]
            if not cfgs:
                # 配置在同步过程中被删/停用：算 0 条（无数据），不算失败
                return 0
            st = _sync_group(session, option_name, end_month, cfgs)
            return int(st.get("insert", 0) + st.get("update", 0))
    except Exception as e:  # noqa: BLE001 —— 单组失败不中断整批
        logger.error("[option_market] 单组同步失败 %s/%s: %s", option_name, end_month, e)
        return -1


def sync_option_market(on_progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """一键同步：把 `bds_option_config` 里所有启用合约的行情拉回来入库。

    :param on_progress: 回调 `(done, total, label)`，用于把「第几组/共几组」写进任务进度。
                        进度必须按**组**报而不是按行——实际耗时在接口请求上。
    :return: 结果摘要，直接写进 `bds_sync_task.result`
    """
    with SessionLocal() as session:
        cfgs = session.query(OptionConfig).filter(OptionConfig.is_enabled == 1).all()
        if not cfgs:
            raise ValueError("配置清单为空（bds_option_config 无 is_enabled=1 的行），请先配置要同步的合约")

        groups: dict[tuple[str, str], list[OptionConfig]] = {}
        for c in cfgs:
            groups.setdefault((c.option_name, c.end_month or ""), []).append(c)

        stats = {
            "symbol_total": len(cfgs),
            "group_total": len(groups),
            "group_ok": 0,
            "group_failed": 0,
            "row_insert": 0,
            "row_update": 0,
            "row_skip": 0,
        }
        errors: list[dict] = []
        per_group: list[dict] = []

        for i, ((option_name, end_month), items) in enumerate(groups.items()):
            label = f"{option_name}/{end_month or 'ALL'}（{len(items)} 个合约）"
            if on_progress:
                on_progress(i, len(groups), label)
            try:
                st = _sync_group(session, option_name, end_month, items)
                stats["group_ok"] += 1
                stats["row_insert"] += st.get("insert", 0)
                stats["row_update"] += st.get("update", 0)
                stats["row_skip"] += st.get("skip", 0)
                per_group.append({"option_name": option_name, "end_month": end_month,
                                  "config_count": len(items), **st})
            except Exception as e:  # noqa: BLE001 —— 单组失败不中断整批
                stats["group_failed"] += 1
                msg = f"{type(e).__name__}: {e}"
                errors.append({"symbol": "", "table": "option-market",
                               "message": f"{option_name}/{end_month} 失败：{msg}"})
                per_group.append({"option_name": option_name, "end_month": end_month,
                                  "config_count": len(items), "error": msg})
                logger.error("[option_market] 组同步失败 %s/%s: %s", option_name, end_month, e)

        if on_progress:
            on_progress(len(groups), len(groups), "完成")

        stats["per_group"] = per_group
        stats["errors"] = errors
        return stats
