# -*- coding: utf-8 -*-
"""bds 期权合约字典同步服务（`bds_option_info`）。

设计见 `docs/bds期权合约字典方案_2026-10-27.md`。本模块解决四件行情接口解决不了的事：

| 悬案 | 行情接口 | 本模块 |
|---|---|---|
| 合约乘数 | 不给（`option_finance_board` 无此列） | 逐合约给（A 类 10180 而非品种级 10000） |
| 到期日 | 要自己算，且 2027 年必崩（日历只到 2026-12-31） | 交易所给的 `最后交易日` |
| A 类调整合约 | 代码分隔符 `A`，我们正则不认 | 名称末尾 `A` + 乘数成对给出 |
| ETF 券商码 | 无字符串关系 | 8 位码直接就是 openctp 的 `合约ID` |

## 三条设计红线

**① 只 UPSERT，永不 DELETE。** 数据源只返回**当前挂牌**合约（实测最早一条就是次日），
全量覆盖会让表越刷越短，已退市合约的历史行情行就再也查不到规格。

**② 置退市要带行数哨兵。** 一次残缺拉取（如只回来 3,000 行）若照常执行
`UPDATE ... SET is_listed=0`，会把 2 万多张在挂合约全部误标退市。

**③ `symbol` 是派生值，不是身份。** 唯一键用 openctp 原生 `contract_id`；
`symbol` 只建普通索引 + 同步时查重告警——唯一约束会让一次拼装规则错误演变成
整批同步失败，而我们需要的是「暴露但不阻塞」。

## ETF 标准码拼装（实测 510500/202612 共 58 张 58/58 命中）

```
symbol    = CP + YYMM + ('A' 若合约名以 A 结尾否则 'M') + f"{int(round(nominal*1000)):05d}"
nominal   = strike × multiplier ÷ 品种默认乘数
```

- **行权价必须 5 位零填充**：`C2612M06250`，不是 `C2612M6250`
- **`A` 不能统一成 `M`**：`C2612A07500`（10180）与 `C2612M07500`（10000）名义价同为
  7.500 且并存，合并即撞码
- 品种默认乘数优先取 `Config.OPTIONS_MARCH`（权威），缺失时回退「同标的下非 A 类
  合约的乘数」——后者天然排除 A 类污染，实测两者在 510500 上都给 10000

## 🔴 `symbol` 的唯一性边界（2026-10-27 全量入库实测发现，方案文档未覆盖）

ETF 标准码沿用交易所「**合约简称**」口径、**不含标的 6 位**（`P2611M06250` 而非
`510500P2611M06250`），这是为了与既有 `bds_option_config.symbol` / `bds_option_market.symbol`
保持一致。代价是：9 个 ETF 标的的行权价区间大量重叠（`588000` 科创50 与 `588080`
科创板50 的 1.350 档位完全一样），全量入库后**跨标的必然撞码**（实测 422 组）。

| 重复类型 | 实测 | 性质 | 处理 |
|---|---|---|---|
| **同标的**（品种ID + 标的）内重复 | **0 组** | 拼装规则真出错 | 告警 `symbol_dup` |
| **跨标的**重复 | 422 组 | 交易所简称口径固有 | 只计数 `cross_underlying_dup`，不告警 |

⇒ **正确的不变式是「`(品种, 标的, symbol)` 唯一」，不是「`symbol` 唯一」。**
所有按标准码查字典的入口（`lookup_symbols` / `ensure_contracts`）都带 `option_name`
参数做品种过滤——**ETF 不传品种上下文就会串到别的标的**。这也是为什么唯一索引
建在 `contract_id` 上而不是 `symbol` 上。
"""
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import OptionInfo

logger = logging.getLogger("uvicorn.error")

# openctp 静态合约信息接口（akshare `option_contract_info_ctp` 的底层数据源）
CTP_INSTRUMENTS_URL = "https://dict.openctp.cn/instruments?types=option"

# 置退市的行数哨兵：低于此数视为「拉取残缺」，跳过置 0 只告警。
# 实测全量 25,910 行；留 20,000 的余量给「品种大量到期」的正常收缩。
LISTED_SENTINEL_MIN_ROWS = 20000

# 入库分批大小。25,910 行一块提交会长时间持锁且失败全丢，分批后单批失败只丢一批。
_BATCH_SIZE = 1000

# 需要写入/更新的列（不含 id 与 create_time：前者自增、后者只在插入时由 DB 默认值给）
_WRITE_COLS = (
    "contract_id", "symbol", "contract_name", "exchange", "product_id",
    "underlying_contract_id", "cp", "strike", "nominal_strike", "multiplier",
    "tick_size", "list_date", "last_trade_date", "delivery_date",
    "expire_year_month", "is_listed", "last_seen_date", "sync_time",
)

# 数据源用 1/2 表示认购/认沽（实测 au2612C1000 与 50ETF购 都是 '1'）
_CP_BY_TYPE = {"1": "C", "2": "P"}


# ---------------------------------------------------------------------------
# 品种台账反查
# ---------------------------------------------------------------------------

def _ledger_entries() -> list[dict]:
    """把 `MAP_OPTION_LEDGER` 摊平成可检索的行。"""
    out = []
    for option_name, (und_key, product_id, ctp_underlying) in dbsCfg.MAP_OPTION_LEDGER.items():
        out.append({
            "option_name": option_name,
            "und_key": und_key,
            "product_id": product_id,
            "ctp_underlying": ctp_underlying,
        })
    return out


def resolve(option_name: str) -> Optional[dict]:
    """品种名 → 台账条目 `{option_name, und_key, product_id, ctp_underlying}`。

    未登记返回 `None` 而**不抛错**：92 个品种里只有 5 个有映射，其余照存不映射，
    这样将来加品种是纯增量（改台账即可，不用迁移任何历史行）。
    """
    for e in _ledger_entries():
        if e["option_name"] == option_name:
            return e
    return None


def option_name_of(product_id: str, underlying_contract_id: str) -> Optional[str]:
    """反向查：openctp 的 `(品种ID, 标的合约ID)` → 我们的品种名。

    ⚠️ `ETF_O` 一个品种 ID 覆盖 9 个标的，只按 `product_id` 匹配会把 510050 的
    合约全部串成 510500 ⇒ ETF 必须再比 `ctp_underlying`。
    """
    und = (underlying_contract_id or "").strip()
    for e in _ledger_entries():
        if e["product_id"] != product_id:
            continue
        if e["ctp_underlying"] is None or e["ctp_underlying"] == und:
            return e["option_name"]
    return None


def und_key_of(option_name: str) -> Optional[str]:
    """品种名 → 情景分组键（csi300 / csi500etf / au）。"""
    e = resolve(option_name)
    return e["und_key"] if e else None


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------

def _dec(v) -> Optional[Decimal]:
    """空串 / `'-'` / `nan` 统一成 None；其余转 Decimal（不抛异常）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "--", "None", "nan", "NaN", "null"):
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _int(v) -> Optional[int]:
    d = _dec(v)
    return None if d is None else int(d)


def _day(v) -> Optional[date]:
    """`'2026-11-24'` → date；空或非法返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10] if "-" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def _default_multiplier_of_option(option_name: str) -> Optional[int]:
    """品种默认乘数：`Config.OPTIONS_MARCH` 里的 multiplier（权威）。

    只对已登记品种有效；未登记返回 None，由调用方回退到「非 A 类合约乘数」。
    """
    for item in dbsCfg.OPTIONS_MARCH:
        if item["option_name"] == option_name:
            return int(item["multiplier"])
    return None


def to_standard_code(cp: str, yymm: str, is_a: bool, nominal: Decimal) -> str:
    """拼 ETF 标准码：`CP + YYMM + (M|A) + 5 位名义价`（名义价 ×1000 零填充）。

    ⚠️ 5 位是硬约束：行情接口给的是 `510500C2612M06250`，拼成 4 位
    `C2612M6250` 会与它**永不相等**（这个错我在方案阶段犯过一次）。

    ⚠️ 全程 Decimal + `ROUND_HALF_UP`，不要走 float：A 类的名义价是
    `6.876 × 10180 ÷ 10000 = 6.999768` 这种带尾数的值，float 乘 1000 再 round 在
    边界档位上会舍错方向。
    """
    sep = "A" if is_a else "M"
    k = (Decimal(nominal) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{cp}{yymm}{sep}{int(k):05d}"


# ---------------------------------------------------------------------------
# 拉取与入库
# ---------------------------------------------------------------------------

def fetch_raw():
    """拉 openctp 全量合约表（25,910 行 × 29 列）。返回 DataFrame，失败抛异常。

    ⚠️ 不做任何异常吞并：拉不到就是拉不到，必须让任务标 failed。
    akshare 的一些接口会 `except: return 空表`，那种「静默 0 行」在本项目已经
    吃过两次亏（`option_hist_shfe` 对 2026 年全失效）。
    """
    import akshare as ak
    df = ak.option_contract_info_ctp()
    if df is None or df.empty:
        raise ValueError("openctp 合约信息接口返回空表")
    return df


def _build_rows(df, today: date) -> tuple[list[dict], dict]:
    """DataFrame → 待写入的行 + 统计。

    两遍扫描是必需的：第一遍统计「每个标的下非 A 类合约的乘数」作为默认乘数兜底，
    第二遍才能算名义行权价。单遍做不到（名义价依赖同批次其它行的乘数）。
    """
    stats = {"total": int(len(df)), "skipped": 0, "skip_reasons": {},
             "default_mult_conflict": [], "symbol_dup": []}

    def _bump(reason: str):
        stats["skipped"] += 1
        stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1

    # ---- 第一遍：非 A 类合约的乘数（按 品种ID + 标的 分组）----
    # 只统计「非 A 类」是为了让默认乘数天然排除 A 类的 10180 污染：A 类可由合约名
    # 末尾的 A 精确识别，不需要靠「出现次数多」去猜（510500 是 30 常规 vs 28 A 类，
    # 差一点就会被众数判反）。
    clean = []
    mult_counter: dict[tuple[str, str], dict[int, int]] = {}
    for _, r in df.iterrows():
        name = str(r.get("合约名称") or "").strip()
        is_a = name.endswith("A")
        clean.append((r, name, is_a))
        if is_a:
            continue
        key = (str(r.get("品种ID") or "").strip(),
               str(r.get("标的合约ID") or "").strip())
        m = _int(r.get("合约乘数"))
        if m:
            c = mult_counter.setdefault(key, {})
            c[m] = c.get(m, 0) + 1
    fallback_mult = {k: max(v.items(), key=lambda x: x[1])[0]
                     for k, v in mult_counter.items()}

    # ---- 第二遍：逐行解析 ----
    rows: list[dict] = []
    # 撞码检测分两级，因为两种「重复」性质完全不同（见文件头「symbol 的唯一性边界」）：
    #   ① 同标的（品种ID + 标的）内重复 ⇒ 拼装规则真出错，必须告警
    #   ② 跨标的重复 ⇒ ETF 标准码本就不含标的（交易所简称口径），9 个 ETF 的行权价
    #      区间大量重叠（588000 与 588080 都是 1.350 这种），必然重复，属预期
    seen_symbol: dict[tuple[str, str, str], str] = {}
    owner_of_symbol: dict[str, set] = {}
    for r, name, is_a in clean:
        try:
            contract_id = str(r.get("合约ID") or "").strip()
            if not contract_id:
                _bump("合约ID为空")
                continue
            exchange = str(r.get("交易所ID") or "").strip()
            product_id = str(r.get("品种ID") or "").strip()
            und = str(r.get("标的合约ID") or "").strip()
            cp = _CP_BY_TYPE.get(str(r.get("期权类型") or "").strip())
            strike = _dec(r.get("行权价"))
            multiplier = _int(r.get("合约乘数"))
            last_trade = _day(r.get("最后交易日"))
            if cp is None or strike is None or multiplier is None or last_trade is None:
                _bump("必填字段缺失")
                continue
            if strike <= 0 or multiplier <= 0:
                _bump("行权价或乘数<=0")
                continue

            # 到期年月：**由最后交易日派生**，不用数据源的「交割年份/交割月份」。
            # 实测（2026-10-27，DCE `a2701-C-3900`）：商品期权的「交割月份」指的是
            # **标的期货**的交割月（2701），而期权自己 2026-12-16 就到期了。
            # 直接存源字段会得到 expire_year_month=202701 与 last_trade_date=2026-12-16
            # 自相矛盾，前端「按到期月筛 202612」会漏掉整批合约。
            # ETF/股指两者本就一致（交割月=到期月），所以这个改法对它们无影响。
            expire_ym = last_trade.strftime("%Y%m")

            default_mult = None
            is_etf = exchange in ("SSE", "SZSE")
            if is_etf:
                opt_name = option_name_of(product_id, und)
                default_mult = (_default_multiplier_of_option(opt_name)
                                if opt_name else None)
                fb = fallback_mult.get((product_id, und))
                if default_mult and fb and default_mult != fb:
                    # 权威值与数据推断不一致 ⇒ 记下来，不静默选一个
                    stats["default_mult_conflict"].append(
                        {"product_id": product_id, "underlying": und,
                         "config_mult": default_mult, "observed_mult": fb})
                if default_mult is None:
                    default_mult = fb

            if is_etf and default_mult:
                # 拼码用**高精度**名义价，只把存库值量化到 4 位：
                # A 类 `6.876 × 10180 ÷ 10000 = 6.999768`，先量化成 6.9998 再 ×1000
                # 虽然这次恰好还对，但边界档位会舍错方向（见 to_standard_code）。
                nominal_raw = strike * Decimal(multiplier) / Decimal(default_mult)
                nominal = nominal_raw.quantize(Decimal("0.0001"))
                yymm = expire_ym[2:6]
                symbol = to_standard_code(cp, yymm, is_a, nominal_raw)
            else:
                # 非 ETF（或 ETF 但拿不到默认乘数）：标准码就是原生 ID，名义价即行权价
                nominal = strike
                symbol = contract_id

            key = (product_id, und, symbol)
            if key in seen_symbol and seen_symbol[key] != contract_id:
                # 同标的内撞码：拼装规则真出错了。不阻塞入库（已存的行不会被删），
                # 但必须暴露——唯一约束若建在 symbol 上会让整批同步失败，所以只告警。
                stats["symbol_dup"].append(
                    {"symbol": symbol, "product_id": product_id, "underlying": und,
                     "a": seen_symbol[key], "b": contract_id})
            seen_symbol[key] = contract_id
            owner_of_symbol.setdefault(symbol, set()).add((product_id, und))

            rows.append({
                "contract_id": contract_id,
                "symbol": symbol,
                "contract_name": name,
                "exchange": exchange,
                "product_id": product_id,
                "underlying_contract_id": und,
                "cp": cp,
                "strike": strike,
                "nominal_strike": nominal,
                "multiplier": multiplier,
                "tick_size": _dec(r.get("最小变动价位")),
                "list_date": _day(r.get("上市日期")),
                "last_trade_date": last_trade,
                "delivery_date": _day(r.get("交割日")),
                "expire_year_month": expire_ym,
                "is_listed": 1,
                "last_seen_date": today,
                "sync_time": datetime.now(),
            })
        except Exception as e:  # noqa: BLE001 —— 单行失败不中断整批
            _bump("解析异常")
            logger.warning("[option_info] 单行解析失败 %s: %s", r.get("合约ID"), e)

    stats["parsed"] = len(rows)
    # 跨标的同码（预期行为，不是缺陷）：ETF 标准码沿用交易所「合约简称」口径、不含标的
    # 6 位，而 9 个 ETF 标的的行权价区间大量重叠 ⇒ 不同标的会拼出同一个 symbol。
    # 只统计不告警；查询必须带品种上下文（见 lookup_symbols），否则会串到别的标的。
    stats["cross_underlying_dup"] = sum(
        1 for v in owner_of_symbol.values() if len(v) > 1)
    return rows, stats


def fetch_and_store(on_progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """拉全量合约表并 UPSERT 入库（累积不删）。返回结果摘要，写进任务 `result`。

    :param on_progress: 回调 `(done, total, label)`。total = 1（拉取）+ 入库批次数，
                        这样进度条能显示「正在写第几批」，而不是长时间停在一个数上。

    入库语义：
    - `contract_id` 已存在 ⇒ 更新 18 个业务列 + `is_listed=1` + `last_seen_date=今天`
    - 不存在 ⇒ 插入
    - **本次未出现、但库里有的行，一律不动**；只有全量成功且行数过哨兵时，
      才把 `last_seen_date < 今天` 的置 `is_listed=0`
    """
    today = date.today()

    df = fetch_raw()
    rows, stats = _build_rows(df, today)

    if not rows:
        raise ValueError(f"合约表解析后无有效行（原始 {stats['total']} 行，全部被跳过）")

    batch_count = (len(rows) + _BATCH_SIZE - 1) // _BATCH_SIZE
    total = 1 + batch_count

    def _report(done: int, label: str):
        if on_progress:
            on_progress(done, total, label)

    _report(0, f"拉取合约字典（{stats['total']} 行）")

    inserted = updated = 0
    with SessionLocal() as session:
        existing = set(session.scalars(select(OptionInfo.contract_id)).all())

        for i in range(batch_count):
            chunk = rows[i * _BATCH_SIZE:(i + 1) * _BATCH_SIZE]
            stmt = mysql_insert(OptionInfo.__table__).values(chunk)
            stmt = stmt.on_duplicate_key_update(
                **{c: stmt.inserted[c] for c in _WRITE_COLS if c != "contract_id"}
            )
            session.execute(stmt)
            session.commit()
            _report(1 + i, f"写入 {min((i + 1) * _BATCH_SIZE, len(rows))}/{len(rows)} 行")

        for r in rows:
            if r["contract_id"] in existing:
                updated += 1
            else:
                inserted += 1

        # ---- 置退市（行数哨兵是硬门槛）----
        delisted = 0
        sentinel_skipped = False
        if len(rows) < LISTED_SENTINEL_MIN_ROWS:
            sentinel_skipped = True
            logger.warning(
                "[option_info] 本次仅拉到 %d 行（< 哨兵 %d），跳过置退市，避免误标在挂合约",
                len(rows), LISTED_SENTINEL_MIN_ROWS)
        else:
            delisted = (session.query(OptionInfo)
                        .filter(OptionInfo.last_seen_date < today,
                                OptionInfo.is_listed == 1)
                        .update({"is_listed": 0}, synchronize_session=False))
            session.commit()

        # 撞码只告警不阻塞：symbol 是派生值，唯一约束会让一次规则错误变成整批失败
        if stats["symbol_dup"]:
            logger.warning("[option_info] 标准码撞码 %d 组（前 5：%s）",
                           len(stats["symbol_dup"]), stats["symbol_dup"][:5])
        if stats["default_mult_conflict"]:
            logger.warning("[option_info] 品种默认乘数与实测不一致 %d 处（前 5：%s）",
                           len(stats["default_mult_conflict"]),
                           stats["default_mult_conflict"][:5])

    _report(total, "完成")

    return {
        "fetch_rows": stats["total"],
        "parsed_rows": stats["parsed"],
        "skipped": stats["skipped"],
        "skip_reasons": stats["skip_reasons"],
        "insert": inserted,
        "update": updated,
        "delisted": delisted,
        "sentinel_skipped": sentinel_skipped,
        "symbol_dup": stats["symbol_dup"][:20],
        "symbol_dup_count": len(stats["symbol_dup"]),
        "cross_underlying_dup": stats["cross_underlying_dup"],
        "default_mult_conflict": stats["default_mult_conflict"][:20],
        "warnings": (
            ([f"本次仅拉到 {len(rows)} 行，低于哨兵 {LISTED_SENTINEL_MIN_ROWS}，"
              f"已跳过「置退市」以免误标在挂合约"] if sentinel_skipped else [])
            + ([f"标准码撞码 {len(stats['symbol_dup'])} 组，请检查拼装规则"]
               if stats["symbol_dup"] else [])
            + ([f"品种默认乘数与实测不一致 {len(stats['default_mult_conflict'])} 处"]
               if stats["default_mult_conflict"] else [])
        ),
    }


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------

def latest_info() -> dict:
    """字典新鲜度：最近一次拉取的日期、在挂张数、总行数、写入时刻。

    页面头部显示「字典更新于 …」用。`last_seen_date` 是「最后一次在源里出现」的日期，
    比 `sync_time` 更能说明数据本身的新旧（同步时刻可能只是重跑了一次旧数据）。
    """
    with SessionLocal() as session:
        latest = session.query(func.max(OptionInfo.last_seen_date)).scalar()
        total = session.query(func.count(OptionInfo.id)).scalar() or 0
        listed = (session.query(func.count(OptionInfo.id))
                  .filter(OptionInfo.is_listed == 1).scalar() or 0)
        sync_at = session.query(func.max(OptionInfo.sync_time)).scalar()
    return {
        "latest_seen_date": latest.isoformat() if latest else None,
        "rows": int(total),
        "listed_rows": int(listed),
        "sync_time": sync_at.isoformat() if sync_at else None,
    }


def products() -> list[dict]:
    """台账里已登记的品种清单（下拉框用，只返回有中文名的 5 个）。"""
    out = []
    for e in _ledger_entries():
        out.append({
            "option_name": e["option_name"],
            "und_key": e["und_key"],
            "product_id": e["product_id"],
            "ctp_underlying": e["ctp_underlying"],
        })
    return out


def ensure_contracts(codes: list[str], option_name: Optional[str] = None) -> dict:
    """检查一组标准码是否都在字典里，缺的返回清单（供同步前自动刷新字典用）。

    :param codes: 标准码（`IO2611-P-4000` / `P2611M06250`）
    :param option_name: **强烈建议传**。ETF 标准码不含标的，9 个标的会拼出同名码；
                        不传品种上下文时「查到了」不代表查到了正确标的的那一行。
    :return: `{"total": n, "missing": [...]}`
    """
    codes = [c for c in codes if c]
    if not codes:
        return {"total": 0, "missing": []}
    with SessionLocal() as session:
        q = session.query(OptionInfo.symbol).filter(OptionInfo.symbol.in_(codes))
        q = _scope_by_option(q, option_name)
        found = {r[0] for r in q.distinct().all()}
    return {"total": len(codes), "missing": sorted(set(codes) - found)}


def _scope_by_option(query, option_name: Optional[str]):
    """给查询加上「品种上下文」过滤。未登记品种不加任何条件（返回原 query）。

    这是 ETF 歧义的唯一正确解法：标准码本身不唯一，但 `(品种, 标的, 标准码)` 唯一
    （实测 25,910 行里同标的内零重复）。
    """
    if not option_name:
        return query
    e = resolve(option_name)
    if e is None:
        return query
    query = query.filter(OptionInfo.product_id == e["product_id"])
    if e["ctp_underlying"]:
        query = query.filter(OptionInfo.underlying_contract_id == e["ctp_underlying"])
    return query


def _row_to_dict(r) -> dict:
    """`OptionInfo` ORM 行 → 普通 dict（脱会话安全）。

    调用方通常在 `with SessionLocal()` 之外继续用这个 dict，绝不要直接返回 ORM 对象
    —— 出会话后访问未加载属性会 `DetachedInstanceError`。
    """
    return {
        "contract_id": r.contract_id,
        "symbol": r.symbol,
        "contract_name": r.contract_name,
        "exchange": r.exchange,
        "product_id": r.product_id,
        "underlying_contract_id": r.underlying_contract_id,
        "cp": r.cp,
        "strike": r.strike,
        "nominal_strike": r.nominal_strike,
        "multiplier": r.multiplier,
        "last_trade_date": r.last_trade_date,
        "expire_year_month": r.expire_year_month,
        "is_listed": r.is_listed,
    }


def lookup_contract(contract_id: str) -> Optional[dict]:
    """按 `contract_id`（openctp 的「合约ID」）精确取一行。

    这是 `POST /option-config` 的推荐入口：`contract_id` 全表唯一，
    不像 `symbol` 那样在 ETF 内部跨标的重复，调用方不需要先想清楚品种上下文。
    """
    cid = (contract_id or "").strip()
    if not cid:
        return None
    with SessionLocal() as session:
        r = session.query(OptionInfo).filter(OptionInfo.contract_id == cid).first()
        return _row_to_dict(r) if r is not None else None


def lookup_symbols(codes: list[str], option_name: Optional[str] = None) -> dict[str, dict]:
    """按标准码批量取字典行（`_sync_group` 逐合约取乘数/到期日用）。

    返回**普通 dict 而不是 ORM 对象**：调用方通常在 `with SessionLocal()` 之外使用，
    ORM 对象出会话后仍在用会触发懒加载异常（DetachedInstanceError）。

    :param option_name: 品种名。ETF 必须传，否则同码的多行会取到别的标的。
    """
    codes = [c for c in codes if c]
    if not codes:
        return {}
    with SessionLocal() as session:
        q = session.query(OptionInfo).filter(OptionInfo.symbol.in_(codes))
        q = _scope_by_option(q, option_name)
        out: dict[str, dict] = {}
        for r in q.all():
            cur = out.get(r.symbol)
            # 同一 symbol 理论上唯一；万一拼装规则出错有多行，取「最后交易日最新」那条
            if cur is None or (r.last_trade_date or date.min) > cur["last_trade_date"]:
                out[r.symbol] = _row_to_dict(r)
        return out
