# -*- coding: utf-8 -*-
"""bds 期权行情同步服务（`bds_option_market`，配置驱动）。

设计要点见 `docs/期权行情基础数据方案_2026-10-27.md`：

1. **配置驱动**：读 `bds_option_config` 的启用行，按 `(option_name, end_month)` 分组调接口。
   没有这张配置表，"一键同步配置的全部期权"无处落地（持仓表是静态属性、监测表是全量铺开）。
2. **接口只给「品种×到期月」全量**，没有单合约查询 ⇒ 实现上必然是「拉整批 → 按配置过滤」。
   好处是以后往清单加同月合约不用重新请求。
3. **按日快照**：唯一键 `(symbol, trade_date)`，同日重跑幂等覆盖、跨日自然成为时间序列。
4. **三列价格分开**：`price`（最新/收盘）、`pre_settle`（前结）、`settle`（结算价，仅商品期权可得）。
   `irs_option_monitor` 把三者挤在一列，导致 au 存收盘价 11.50 而真实结算价 12.18。
5. **降级链只在本文件一处**：结算价缺失时下游按 `COALESCE(settle, pre_settle, price)` 降级。
   展示层如实留空——拿前结价冒充结算价会误导。

三个数据源的能力差异（实测 2026-10-26，这是表设计的第一约束）：

| 数据源 | 结算价 | delta | 成交量/持仓量 | 备注 |
|---|---|---|---|---|
| `option_finance_board`（IO） | ❌ | ❌ | ✅ volume / position | `lastprice` 盘中≠结算价 |
| `option_finance_board`（510500） | ❌（仅前结价） | ❌ | ❌（「数量」列恒等于行数） | 带完整时间戳 |
| 上期所 `.dat` 直连（au） | ✅ | ✅（德尔塔） | ✅ | 一次返回全部品种全部到期月 |

**au 为什么不走 akshare（2026-10-27 实测）**：`option_hist_shfe` 用包内本地
`calendar.json` 判交易日，该日历最大日期停在 **2025-12-31** ⇒ 2026 年任何日期都
「非交易日」返回空 DataFrame，且其内部 `except: return pd.DataFrame()` 把网络失败
也一并吞成空 ⇒ 表现为**静默 0 行**。故改为直连原始 `.dat`，交易日一律以
`bds_trade_date`（已覆盖到 2026-12-31）为准，不依赖任何第三方日历。
不要改 akshare 包内的 json，升级即失效。
"""
import calendar
import logging
import re
from datetime import date, datetime, time as dtime
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from server_fast.common.db import SessionLocal
from server_fast.app.bds.models import OptionConfig, OptionMarket, TradeDate
from server_fast.app.bds.services.market_data import option_info
from server_fast.app.bds.config import Config as dbsCfg

logger = logging.getLogger("uvicorn.error")

# 上期所商品期权合约：au2612P840 = 标的au + 月份2612 + C/P + 行权价840
_SHFE_CODE_RE = re.compile(r"^(?P<prod>[a-zA-Z]+)(?P<ym>\d{4})(?P<cp>[CP])(?P<k>\d+(?:\.\d+)?)$")
# 上交所/深交所 ETF 期权：510500P2611M06250 → 交易所标准码 P2611M06250
#   分隔符有 **M 与 A 两种**：A 是标的除权后交易所对存续合约的「调整合约」
#   （510500C2612A07500：乘数 10180、行权价 7.367、名义价 7.500）。
#   2026-10-27 之前只认 M ⇒ 510500/202612 的 58 张里有 28 张被判「代码格式不符」
#   静默跳过（实测）。A 与 M 不能合并：C2612A07500 与 C2612M07500 名义价同为 7.500
#   但乘数不同（10180 vs 10000），合并即撞码。
_ETF_CODE_RE = re.compile(r"^\d{6}(?P<code>[CP]\d{4}[MA]\d{5})$")

# 无效数值的统一取值集合（akshare 各接口用空串/'-'/'nan' 表示缺失，不统一）
_BAD_TOKENS = {"", "-", "--", "None", "nan", "NaN", "null"}

# 上期所期权日行情原始文件（含全部品种，一次请求服务所有商品期权分组）
SHFE_OPTION_DAY_URL = "https://www.shfe.com.cn/data/tradedata/option/dailydata/kx{ymd}.dat"
_SHFE_HEADERS = {"User-Agent": "Mozilla/4.0 (compatible; MSIE 5.5; Windows NT)"}
# 交易所把「小计」「合计」与真实合约混在同一个数组里
_SHFE_SUMMARY_IDS = {"", "小计", "合计", "总计"}

# 只有「当日快照」才写的列。历史快照新增行留空、已存在的行不覆盖：
# - `price_ud`/`value_i`/`value_t`：由「同步时点标的现价」派生，对历史行无意义
# - `volume`：当日**累计**成交量，盘前取到的恒为 0（实测 2026-10-27 08:07 盘前同步
#   把 10-26 的 IO 成交量 1431/2527/4587 全刷成 0），历史行必须保留原值
_TODAY_ONLY_COLS = ("price_ud", "value_i", "value_t", "volume")


class ShfeDataNotReady(Exception):
    """当日 `.dat` 已存在，但结算数据尚未生成（盘前占位文件）。

    实测 2026-10-27 08:02 的文件：au 有 777 行合约、`CLOSEPRICE`/`SETTLEMENTPRICE`
    全为空串，`update_date` 是当天 08:02。这种文件不是「无数据」而是「还没算完」，
    必须与「取数失败」区分开：前者退一个交易日、后者必须报错。
    """


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
    - `SHFE.au2612P840`     → `au2612P840`（账单前缀可去；旧库里的 `SHEX.` 写法已统一为 `SHFE.`）
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
    """按到期规则推算到期日（遇节假日顺延至下一交易日）。

    🔄 **2026-10-27 实现迁入 bds**：此前本函数只是一层转发，真正实现在
    `irs.service.option_monitor` 里 —— 基础数据的算法放在研究模块、而 bds 反过来
    依赖 irs，方向是反的（违反「依赖只能向下」）。现在实现归 bds，irs 反向引用本函数。

    ⚠️ 它已**降级为回退/交叉校验，不是权威**：权威是 `bds_option_info.last_trade_date`
    （交易所给的）。保留算法只为两件事——① 字典缺行时兜底；② `_cross_check_delisted()`
    用它对字典值做二次核对（`fu_o` 上确实差过一周，61/63 一致）。

    :param end_month: 到期年月，格式 "YYYYMM"（如 "202608"）
    :param rule: 到期日规则码（`Config.RULE_EXERCISE_DATE` 的键：R1/R2/R3）
    """
    # 未知规则校验：R3 配置值为 None（占位），需排除在 None 校验之外
    if rule != "R3" and dbsCfg.RULE_EXERCISE_DATE.get(rule) is None:
        raise ValueError(f"unknown rule_exercise_date: {rule}")

    year = int(end_month[:4])
    month = int(end_month[4:])

    # R3：商品期权，标的期货合约交割月（end_month）前第一月的倒数第五个交易日
    if rule == "R3":
        # 计算"前第一月"：若 month==1 则跨年至上年 12 月
        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1
        prev_first = date(prev_year, prev_month, 1)
        prev_last = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
        # 查询该月所有交易日，按降序取倒数第 5 个
        with SessionLocal() as session:
            trade_dates = session.query(TradeDate.trade_date).filter(
                TradeDate.trade_date >= prev_first,
                TradeDate.trade_date <= prev_last,
            ).order_by(TradeDate.trade_date.desc()).limit(5).all()
        if len(trade_dates) < 5:
            raise ValueError(
                f"TradeDate 日历数据不足：{prev_year}-{prev_month:02d} 交易日不足 5 条"
            )
        return trade_dates[4][0]

    # R1/R2：按 (week_n, weekday) 规则计算
    week_n, weekday = dbsCfg.RULE_EXERCISE_DATE[rule]  # 第几个星期(1-based), 星期几(0=周一)

    # 找到该月第 week_n 个星期 weekday 的日期
    # date(year, month, 1).weekday() 得到 1 号的星期几(0=周一)
    first_weekday = date(year, month, 1).weekday()
    # 1 号到目标星期几的偏移天数（可能为负，表示需到下一周）
    offset = (weekday - first_weekday) % 7
    target_day = 1 + offset + (week_n - 1) * 7
    delisted = date(year, month, target_day)

    # 查询 TradeDate 日历，若非交易日则顺延至下一交易日
    with SessionLocal() as session:
        # 判断 delisted 是否为交易日
        is_trade = session.query(TradeDate.trade_date).filter(
            TradeDate.trade_date == delisted
        ).first()
        if not is_trade:
            # 顺延至下一交易日
            next_trade = session.query(TradeDate.trade_date).filter(
                TradeDate.trade_date > delisted
            ).order_by(TradeDate.trade_date.asc()).first()
            if next_trade:
                delisted = next_trade[0]
            # 若日历中无更晚的交易日（极端情况），保留原计算日期
    return delisted


def _resolve_trade_date(session, closed_only: bool) -> date:
    """判定本次行情属于哪个交易日。

    - `closed_only=True`（商品期权）：上期所 `.dat` 是**日频结算数据**，必须收盘后
      （≥15:00）才有当日数据，否则取上一交易日。结算数据晚于 15:00 发布的窗口由
      `ShfeDataNotReady` 再退一天兜住（见 `fetch_shfe_option_day`）。
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


def _prev_trade_date(session, day: date) -> date:
    """取 `day` 之前最近的交易日（`bds_trade_date` 为唯一日历）。"""
    prev = session.query(TradeDate.trade_date).filter(
        TradeDate.trade_date < day).order_by(TradeDate.trade_date.desc()).first()
    if prev is None:
        raise ValueError(f"bds_trade_date 日历中找不到 {day} 之前的交易日")
    return prev[0]


# ---------------------------------------------------------------------------
# 上期所直连（商品期权唯一通道）
# ---------------------------------------------------------------------------

def fetch_shfe_option_day(trade_date: date, option_name: str) -> list[dict]:
    """直连上期所期权日行情 `.dat`，返回该**交易日 × 品种**的合约行（交易所原始字段名）。

    字段取自交易所原文，不做中文改名——映射表在 `_sync_group` 里集中处理，避免
    「看起来还是 akshare」的假象。常用字段：

    | 交易所字段 | 含义 | 落到 `bds_option_market` |
    |---|---|---|
    | `INSTRUMENTID` | `au2612P840` | `symbol` |
    | `CLOSEPRICE` | 收盘价 | `price` |
    | `PRESETTLEMENTPRICE` | 前结算价 | `pre_settle` |
    | `SETTLEMENTPRICE` | 结算价 | `settle` |
    | `DELTA` | 德尔塔 | `delta_opt` |
    | `VOLUME` / `OPENINTEREST` | 成交量 / 持仓量 | `volume` / `open_interest` |

    三种异常必须分开，不能像 akshare 那样统一吞成空表：

    - `FileNotFoundError`：该日无文件（404）⇒ 日历与交易所不一致，必须暴露
    - `ShfeDataNotReady`：文件在但结算价全空 ⇒ 盘前占位文件，调用方退一个交易日
    - `ValueError`：非 JSON / 品种名不匹配 ⇒ 配置或接口变更，必须暴露

    回补历史直接复用本函数（一次一天），不需要第二套取数逻辑。
    """
    import requests

    ymd = trade_date.strftime("%Y%m%d")
    url = SHFE_OPTION_DAY_URL.format(ymd=ymd)
    r = requests.get(url, headers=_SHFE_HEADERS, timeout=30)
    if r.status_code == 404:
        raise FileNotFoundError(f"上期所 {ymd} 无期权日行情文件：{url}")
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError as e:
        raise ValueError(
            f"上期所 {ymd} 返回非 JSON（Content-Type={r.headers.get('Content-Type')}）: {e}") from e

    rows = payload.get("o_curinstrument") or []
    picked = [
        x for x in rows
        if str(x.get("PRODUCTNAME", "")).strip() == option_name
        and str(x.get("INSTRUMENTID", "")).strip() not in _SHFE_SUMMARY_IDS
    ]
    if not picked:
        raise ValueError(f"上期所 {ymd} 文件里没有品种「{option_name}」的合约（PRODUCTNAME 不匹配）")
    # 结算价是日终产物：全品种全部合约都空 ⇒ 当天还没结算完，不是「今天没有行情」
    if not any(str(x.get("SETTLEMENTPRICE", "")).strip() for x in picked):
        raise ShfeDataNotReady(
            f"上期所 {ymd}「{option_name}」结算价全空"
            f"（文件 update_date={payload.get('update_date')}，盘前占位）")
    return picked


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
    """取品种元信息（乘数 / 到期规则 / 品种类型）。

    🔄 2026-10-27 起读 **bds 自己的** `Config.OPTIONS_MARCH`（此前读 `IrsCfg` 的副本，
    造成 bds → irs 的反向依赖）。irs 现在反向引用同一份。
    """
    for item in dbsCfg.OPTIONS_MARCH:
        if item["option_name"] == option_name:
            return item
    raise ValueError(f"未知期权品种（不在 Config.OPTIONS_MARCH 中）: {option_name}")


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


def _cross_check_delisted(code: str, em: str, rule: str,
                          info_date: date) -> Optional[str]:
    """到期日交叉校验：字典（交易所）vs 自研算法。

    不一致时**采用字典值**、只记告警，不静默采用任何一方。理由：
    - 字典是交易所给的，权威（`au`/`IO`/`HO`/`MO`/ETF 实测 61/63 一致）
    - 但 openctp 是第三方聚合源，`fu_o` 上确实与算法差一周 ⇒ 校验不能省

    算法抛异常（2027 年合约超出 `bds_trade_date` 日历）时什么都不做：
    那正是字典存在的意义（它让这条路径不再依赖日历）。
    """
    try:
        calc = _calc_delisted_date(em, rule)
    except Exception:  # noqa: BLE001 —— 日历不足属预期，不是错误
        return None
    if calc != info_date:
        return f"{code}: 到期日 字典={info_date} 算法={calc}（已采用字典值）"
    return None


def _sync_group(session, option_name: str, end_month: str,
                cfgs: list[OptionConfig]) -> dict:
    """同步一个 (品种, 到期月) 分组。返回本组统计。

    **规格（乘数 / 到期日）从 `bds_option_info` 逐合约取**，不再用品种级默认值
    与自研推算 —— 这是 P2 的核心改动，解决两个既有缺陷：

    | 缺陷 | 旧行为 | 新行为 |
    |---|---|---|
    | A 类调整合约 | 用品种乘数 10000，市值差 1.8% | 用字典的 10180 |
    | 2027 年合约 | `_calc_delisted_date()` 抛 `日历数据不足` | 用字典的 `last_trade_date`，不碰日历 |

    字典缺失时回退旧逻辑（品种乘数 + 算法），并记 `info_miss` 告警——
    宁可降级也不让整组同步失败。
    """
    meta = _variety_meta(option_name)
    is_commodity = meta["option_type"] == "商品期权"
    variety_multiplier = int(meta["multiplier"])
    rule = meta["rule_exercise_date"]
    underlying_symbol = meta["underlying_symbol"]

    # und_key 从台账取（P3 后配置表不再存这一列），台账缺失才回退配置行
    ledger = option_info.resolve(option_name) or {}
    und_key = ledger.get("und_key") or (cfgs[0].und_key if cfgs else None)

    trade_date = _resolve_trade_date(session, closed_only=is_commodity)

    if is_commodity:
        # 商品期权唯一通道：直连上期所 .dat（akshare 通道对 2026 年全失效，见文件头）
        source = "shfe_direct"
        try:
            records = fetch_shfe_option_day(trade_date, option_name)
        except ShfeDataNotReady as e:
            # 15:00 ~ 交易所结算数据发布之间的窗口：退一个交易日。
            # 写的是**真实的历史快照**（同一 trade_date 幂等覆盖），不会造出假数据。
            prev = _prev_trade_date(session, trade_date)
            logger.warning("[option_market] %s，改用上一交易日 %s", e, prev)
            trade_date = prev
            records = fetch_shfe_option_day(trade_date, option_name)
    else:
        import akshare as ak
        if not end_month or end_month == "ALL":
            raise ValueError(f"{option_name} 必须在配置里指定 end_month（接口按到期月拉取）")
        df = ak.option_finance_board(symbol=option_name, end_month=end_month)
        source = "akshare_board"
        records = [] if df is None or df.empty else df.to_dict("records")

    if not records:
        return {"rows": 0, "insert": 0, "update": 0, "skip": 0,
                "note": f"{option_name}/{end_month} 接口返回空"}

    # 本次同步写的是不是「今天的快照」。盘前/非交易日跑同步时 trade_date 会是上一交易日，
    # 此时标的现价与派生列无意义（见下方 ud 处的说明）。
    is_today_snapshot = trade_date == datetime.now().date()

    # 配置清单：交易所标准码 → 配置行（过滤用，同时提供 broker_symbol）
    wanted: dict[str, OptionConfig] = {}
    for c in cfgs:
        wanted[normalize_symbol(c.symbol)] = c

    # 逐合约规格：从字典取乘数与权威到期日。
    # ⚠️ 必须传 option_name 做品种过滤：ETF 标准码不含标的 6 位，9 个 ETF 标的会拼出
    # 同码（`C2612M01350` 同时属于 588000 与 588080），不过滤就会串到别的标的。
    info_map = option_info.lookup_symbols(list(wanted.keys()), option_name=option_name)
    spec_warnings: list[str] = []

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

    for row in records:
        try:
            if is_commodity:
                # 字段用上期所原名（见 fetch_shfe_option_day 的映射表）
                raw = str(row["INSTRUMENTID"]).strip()
                m = _SHFE_CODE_RE.match(raw)
                if not m:
                    _bump("合约代码格式不符")
                    continue
                code = raw
                ym = m.group("ym")
                cp = m.group("cp")
                strike = _dec(m.group("k"))
                und_symbol = f"{underlying_symbol}{ym}"  # SHFE.au2612
                em = f"20{ym}"
                price = _dec(row.get("CLOSEPRICE"))
                pre_settle = _dec(row.get("PRESETTLEMENTPRICE"))
                settle = _dec(row.get("SETTLEMENTPRICE"))
                delta = _dec(row.get("DELTA"))
                volume = _int(row.get("VOLUME"))
                oi = _int(row.get("OPENINTEREST"))
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
                und_symbol = underlying_symbol
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
            # 规格（乘数 / 到期日）优先取字典：它是这两个事实的权威源。
            # 字典缺失才回退旧逻辑，且回退必须留痕 —— 「静默用品种乘数」正是
            # A 类合约差 1.8% 市值的成因。
            spec = info_map.get(code)
            if spec:
                multiplier = int(spec["multiplier"])
                delisted = spec["last_trade_date"]
                warn = _cross_check_delisted(code, em, rule, delisted)
                if warn:
                    spec_warnings.append(warn)
            else:
                multiplier = variety_multiplier
                delisted = _calc_delisted_date(em, rule)
                spec_warnings.append(
                    f"{code}: 字典无此合约，回退品种乘数 {variety_multiplier} + 算法到期日")
            # 标的现价是**同步那一刻**的值，只对「当日快照」成立。
            # 盘前跑同步拿到的是上一交易日的收盘快照（trade_date=昨天），此时再取现价
            # 就是把今天的价贴到昨天那一行上——实测 10-27 08:04 把 10-26 的
            # price_ud 从 862.58 改成 865.36，连带 value_i/value_t 一起失真。
            # 历史快照一律不取，写库时跳过这三列（保留原值，新行为 NULL）。
            ud = _ud(und_symbol, und_key) if is_today_snapshot else None
            if not is_today_snapshot:
                # 同理：盘前拿到的「成交量」属于下一交易日，写成 0 会把历史行的量抹掉。
                # 宁可留空让人知道「这一列没回补」，也不能填 0 假装当天无成交。
                volume = None

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
                underlying_symbol=underlying_symbol,
                und_key=und_key,
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
                    # 历史快照不覆盖「只有当日才有意义」的列：那是同步时点的现价/当日累计量
                    if not is_today_snapshot and k in _TODAY_ONLY_COLS:
                        continue
                    setattr(cur, k, v)
                update += 1
        # ⚠️ 2026-10-27 起**没有「顺手回填 config 规格」这一步了**：config 的规格列
        #    （cp/strike/multiplier/expiry_date/margin_type）已物理删除，规格的权威源
        #    是 `bds_option_info`（上面 `info_map` 已经逐合约取过）。原先那段回填
        #    「只填空值、绝不覆盖已有值」的逻辑，恰恰是「手填 11-23 覆盖算法 11-24」
        #    那次事故的通道 —— 删掉它本身就是收益，不是损失。
        session.commit()

    stat = {"rows": len(parsed), "insert": insert, "update": update,
            "skip": sum(skip_reasons.values())}
    if skip_reasons:
        stat["skip_reasons"] = skip_reasons
    if spec_warnings:
        # 到期日交叉校验不一致 / 字典缺失回退，都要能追到具体合约
        stat["warnings"] = spec_warnings
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
            # 到期日交叉校验不一致 / 字典缺失回退：不是失败，但必须能被看到
            "warnings": [],
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
                stats["warnings"].extend(st.get("warnings", []))
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
