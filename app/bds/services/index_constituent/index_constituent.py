# -*- coding: utf-8 -*-
"""指数成分股同步 / 历史回填。

口径（详见 docs/指数成分股历史回填方案_v2.1_2026-10-14.md）：

- **指数清单由调用方显式传入**（页面勾选），不再自动遍历 `Config.INDEX_CODE`；
  未知代码 / `data_source='yfinance'` 的指数一律抛 `ValueError`（router 转 400），不静默跳过。
- **变更键 = 纯 symbol 集合**（weight 只写不比对）。本表按"调仓事件"存储，仅在成分股
  **名单发生变化**的交易日留存快照，期间权重漂移不另存；查询某日成分 = 取
  `trade_date <= D` 的最大快照。
- **起点优先级**：`start_date`（调用方指定）> **核查水位 `checked_to+1`**（增量）>
  库内 `MAX(trade_date)+1`（水位未初始化的库，兼容）> `listed_date`（首次全量）。
- **核查水位**（P0-0，表 `bds_index_constituent_sync`）：本表按「名单变化」落行，名单
  没变的交易日**没有行可挂**，所以「已核查到哪天」必须单独存。`checked_to` 记录**已处理**
  到哪天（含失败日），`gap_days` / `first_gap` 记录其中失败的日。没有它，增量只能从
  「末次变化日+1」重跑：399006 每次增量白跑约 85 个交易日。
  ⚠️ **写入顺序硬约束**：先 commit 快照，再写水位。反过来（先推水位再落快照）在崩溃时
  会永久丢一次名单变化——水位说核查过、快照却不存在，且不会自愈。
- **伪快照拦截**（`_judge_change`）：宽基指数成分数恒定，`len(名单) ≠ 应有数` 就是 gm
  抖动（新旧并存 / 缺股 / 回退）的直接证据。超量或缺 2 只以上一律拒存并计入 `dirty_days`；
  缺 1 只放行（拒存会退回更旧的名单，错误反而放大）。基准取「最近一个满编快照」而非上一个
  快照——抖动区内相邻比对会连锁误判。
- **入库**走 `upsert_df_to_db`（`ON DUPLICATE KEY UPDATE weight`），天然幂等。
"""
import bisect
import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import pandas as pd
from gm.api import stk_get_index_constituents
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from server_fast.config import settings
from server_fast.common.db import SessionLocal
from server_fast.common.utils import call_with_timeout, upsert_df_to_db
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import IndexConstituent, IndexConstituentSync, TradeDate

logger = logging.getLogger("uvicorn.error")

# gm 限速：仅在实际调过 gm 后 sleep（实测 gm 单次响应约 120ms，故无需更长的固定间隔）
SLEEP_MIN, SLEEP_MAX = 0.2, 0.35

# 连续失败上限：达到即中止该指数，避免 gm 终端离线 / 触发流控时白刷数千次
MAX_CONSEC_FAIL = 10

# 单次 gm 调用超时（gm 终端未启动时会无限阻塞）
GM_TIMEOUT = 30

# 水位写库节流：skipped/no_data 累积到该条数才 flush；saved/failed 一律立即 flush
WATERMARK_FLUSH_EVERY = 50

# ---------------------------------------------------------------------------
# gm 致命错误检测（与 bds/router.py 的批量同步用同一口径）
# ---------------------------------------------------------------------------
_GM_FATAL_CODE_RE = re.compile(r'"status"\s*:\s*(\d+)')
_GM_FATAL_HINTS = ('1001', '无法连接到终端服务', '终端服务', 'TimeoutError', '连接超时',
                   '无法连接', 'gm 终端', '请检查 gm 终端')


def _is_gm_fatal(raw_error: str) -> bool:
    """判断 gm 错误是否为终端断连等致命错误（需立即中止所有指数回填）。"""
    if not raw_error:
        return False
    m = _GM_FATAL_CODE_RE.search(raw_error)
    if m and m.group(1) in ('1001', '1002'):
        return True
    raw_lower = raw_error.lower()
    return any(h.lower() in raw_lower for h in _GM_FATAL_HINTS)


# ---------------------------------------------------------------------------
# 参数解析 / 计划预演（router 与主入口共用同一套口径）
# ---------------------------------------------------------------------------

def _to_date(value) -> Optional[date]:
    """把 config 中的 listed_date（字符串）统一转成 date；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _resolve_indices(index_codes) -> list:
    """校验并解析指数清单：保序去重，返回 [(code, info), ...]。

    非法项一律抛 ValueError（由 router 转 400），不静默跳过——
    手动勾选即明确意图，静默跳过会让用户误以为已回填。
    """
    if not index_codes:
        raise ValueError("index_codes 不能为空，至少选择一个指数")

    cfg = dbsCfg.INDEX_CODE
    unknown, unsupported, targets, seen = [], [], [], set()
    for raw in index_codes:
        code = (raw or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        info = cfg.get(code)
        if info is None:
            unknown.append(code)
        elif info.get("data_source") == "yfinance":
            unsupported.append(code)
        else:
            targets.append((code, info))

    if unknown or unsupported:
        msgs = []
        if unknown:
            msgs.append(f"未知指数代码：{', '.join(unknown)}")
        if unsupported:
            msgs.append(f"数据源非 gm（yfinance），不支持成分股同步：{', '.join(unsupported)}")
        raise ValueError("；".join(msgs))

    if not targets:
        raise ValueError("index_codes 中没有可用指数")
    return targets


def _load_trade_dates(session: Session) -> list:
    """读取 <= 今天 的全部交易日（升序）。

    `bds_trade_date` 含未来日期（如 2026-12-31），回填必须按今天截断。
    """
    today = date.today()
    rows = (
        session.query(TradeDate.trade_date)
        .filter(TradeDate.trade_date <= today)
        .order_by(TradeDate.trade_date.asc())
        .all()
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 核查水位（P0-0）：读 / 推进 / 写回
# ---------------------------------------------------------------------------

def load_watermark(session: Session, index_code: str) -> Optional[dict]:
    """读该指数核查水位；无行返回 None（水位尚未初始化）。"""
    row = (
        session.query(IndexConstituentSync)
        .filter(IndexConstituentSync.index_code == index_code)
        .first()
    )
    if row is None:
        return None
    return {
        "checked_to": row.checked_to,
        "last_change_date": row.last_change_date,
        "constituent_count": row.constituent_count,
        "gap_days": row.gap_days or 0,
        "first_gap": row.first_gap,
        "no_data_days": row.no_data_days or 0,
        "dirty_days": row.dirty_days or 0,
        "first_dirty": row.first_dirty,
    }


def flush_watermark(engine, index_code: str, wm: dict) -> None:
    """把水位写回库（幂等 + 日期列单调增）。

    ⚠️ 调用时机：必须在快照 commit **之后**。先推水位再落快照，崩溃时会永久丢一次
    名单变化（水位说核查过、快照却不存在），且不会自愈。

    日期列用 `GREATEST(COALESCE(当前, 新值), 新值)` 保证永不回退——手工补录（start_date
    早于当前水位）或重入写旧值都不会把水位拉回去。`gap_days` / `dirty_days` 用普通赋值：
    补核查成功后需要把它清零，单调增反而不对。
    """
    if wm.get("checked_to") is None:
        return
    sql = text("""
        INSERT INTO bds_index_constituent_sync
            (index_code, checked_to, last_change_date, constituent_count,
             gap_days, first_gap, no_data_days, dirty_days, first_dirty, last_check_at)
        VALUES (:index_code, :checked_to, :last_change_date, :constituent_count,
                :gap_days, :first_gap, :no_data_days, :dirty_days, :first_dirty, NOW())
        ON DUPLICATE KEY UPDATE
            checked_to       = GREATEST(COALESCE(checked_to, VALUES(checked_to)), VALUES(checked_to)),
            last_change_date = GREATEST(COALESCE(last_change_date, VALUES(last_change_date)),
                                        VALUES(last_change_date)),
            constituent_count = VALUES(constituent_count),
            gap_days         = VALUES(gap_days),
            first_gap        = VALUES(first_gap),
            no_data_days     = GREATEST(no_data_days, VALUES(no_data_days)),
            dirty_days       = VALUES(dirty_days),
            first_dirty      = VALUES(first_dirty),
            last_check_at    = VALUES(last_check_at)
    """)
    with engine.begin() as conn:
        conn.execute(sql, {"index_code": index_code, **wm})


def _incremental_start(session: Session, index_code: str) -> Optional[date]:
    """增量起点：水位 `checked_to+1` > `MAX(trade_date)+1` > None（首次全量）。

    为什么不能只用 `MAX(trade_date)+1`：本表按「名单变化」落行，`MAX` 长期停在末次变化日，
    增量会从那天+1 一直重跑到今天（399006 曾每次白跑约 85 个交易日、纯 gm 浪费）。
    """
    wm = load_watermark(session, index_code)
    if wm and wm["checked_to"]:
        return wm["checked_to"] + timedelta(days=1)

    # 兼容水位尚未初始化的库：退回旧口径
    row = (
        session.query(func.max(IndexConstituent.trade_date))
        .filter(IndexConstituent.index_code == index_code)
        .first()
    )
    max_date = row[0] if row else None
    if max_date is None:
        return None
    return max_date + timedelta(days=1)


def _listed_start(info: dict) -> date:
    """首次全量起点 = 指数自身 listed_date；未配置则报错（INDEX_CODE 必须填上市日）。"""
    listed = _to_date(info.get("listed_date"))
    if listed is None:
        raise ValueError(f"指数 {info.get('sec_name', info)} 未配置 listed_date，请在 Config.INDEX_CODE 中补齐")
    return listed


def _build_plan(session: Session, targets: list, start_date: Optional[date],
                trade_dates: list) -> list:
    """生成执行计划 [(code, info, this_start, todo_count), ...]。

    todo_count = 该指数从 this_start 起（含）待遍历的交易日数——**含已存日**，
    因为"已存"仍要逐日比对跳过（零 gm 开销但有循环成本），计入后进度才闭合。
    """
    plan = []
    for code, info in targets:
        if start_date is not None:
            this_start = start_date
        else:
            this_start = _incremental_start(session, code) or _listed_start(info)
        # trade_dates 升序 ⇒ 用 bisect 数 >= this_start 的个数
        todo = len(trade_dates) - bisect.bisect_left(trade_dates, this_start)
        plan.append((code, info, this_start, todo))
    return plan


def plan_index_constituent_backfill(index_codes, start_date: Optional[date] = None) -> dict:
    """预演回填计划：不调 gm、不写库。

    供 router 在启动任务时预算 `total_count` 并回显生效起点；与主入口共用同一套口径。

    :return: {"per_index": [{"code","sec_name","start_date","count"}, ...], "total": int}
    """
    targets = _resolve_indices(index_codes)
    with SessionLocal() as session:
        trade_dates = _load_trade_dates(session)
        plan = _build_plan(session, targets, start_date, trade_dates)

    per_index = [
        {
            "code": code,
            "sec_name": info.get("sec_name"),
            "start_date": this_start.isoformat(),
            "count": todo,
        }
        for code, info, this_start, todo in plan
    ]
    return {"per_index": per_index, "total": sum(p["count"] for p in per_index)}


# ---------------------------------------------------------------------------
# 伪快照拦截（防新增）
# ---------------------------------------------------------------------------

def expect_count(index_code: str) -> Optional[int]:
    """该指数的应有成分股只数（`Config.INDEX_CODE[*]['constituent_count']`）。

    未配置则返回 None ⇒ 该项不做只数校验（如上证指数取全部沪市股票，无固定只数）。
    """
    return dbsCfg.INDEX_CODE.get(index_code, {}).get("constituent_count")


def _judge_change(current: set, expect: Optional[int], base: Optional[set]) -> tuple:
    """判断 gm 返回的名单能否落库。返回 `(ok, reason)`。

    背景：`bds_index_constituent` 按「名单变化」落行，而原判据只有 `current == seed`，
    于是 gm 的任何抖动（缺股 / 新旧并存 / 回退）都会被当成真调仓存下，形成伪快照。
    宽基指数成分数恒定，`len(current)` 就是最便宜的判伪信号（详见
    `docs/成分股伪快照核查_2026-10-20.md`）：

    - 超量 ⇒ 调仓生效日前后 gm 把调出与调入同时返回（T3/T5），拒存
    - 缺 2 只以上 ⇒ gm 某次返回缺股（T2），拒存
    - **缺 1 只 ⇒ 放行**（T4）：它通常是「调仓后拿到新名单但少 1 只」，拒存会让该日
      回退到更早的旧名单，错误从「缺 1 只」放大到「整份名单是旧的」
      （000905/2021-09-13 即此，见 MEMORY §6.7）

    `base` 是**最近一个满编快照**，不是上一个快照：抖动区内拿相邻快照当基准会连锁误判
    （A⊂B、B 又是 C 的超集）。`base` 未满编（建仓期 78→100、沪深300 早期 299 只）时
    gate 自动关闭，退回「有变化就落库」的原行为，无需硬编码豁免区间。
    """
    if not expect or not base or len(base) != expect:
        return True, "no_gate"

    n = len(current)
    if n > expect:
        return False, f"too_many:{n}>{expect}"
    gap = expect - n
    if gap > 1:
        return False, f"too_few:{n}<{expect}"
    if gap == 1:
        return True, "near_miss"
    return True, "ok"


# ---------------------------------------------------------------------------
# gm 调用 / 入库
# ---------------------------------------------------------------------------

def _normalize_weight(weight) -> float:
    """权重归一化：NaN/None 统一转 0.0，其余保留 4 位小数。"""
    if weight is None or pd.isna(weight):
        return 0.0
    return round(float(weight), 4)


def _call_gm(index_code: str, trade_date: date):
    """带超时保护地取指定交易日的成分股（gm 终端未启动时会阻塞，必须有超时）。"""
    return call_with_timeout(stk_get_index_constituents, timeout=GM_TIMEOUT)(
        index=index_code,
        trade_date=trade_date.strftime("%Y-%m-%d"),
    )


def _save_snapshot(engine, df: pd.DataFrame, trade_date: date) -> int:
    """把该日全量成分股写入（含当日 weight，即便为 0），走 UK 幂等 upsert。

    ⚠️ `trade_date` 必须由调用方传入**循环变量 td**，禁止用 `df` 自带的同名列：
    gm 该列返回的是「成分版本生效日」而非请求日（实测请求 2026-10-20、该列给的是 10-19），
    直接落库会把快照钉到错误日期——轻则产生与前一快照完全重复的冗余行，
    重则让 point-in-time 估值用到不该用的名单。
    """
    out = df[["index", "symbol", "weight"]].copy()
    out = out.rename(columns={"index": "index_code"})
    out["trade_date"] = trade_date
    out["weight"] = out["weight"].apply(_normalize_weight)
    return upsert_df_to_db(
        out,
        IndexConstituent.__table__.name,
        engine,
        unique_keys=["index_code", "symbol", "trade_date"],
        update_columns=["weight"],
    )


# ---------------------------------------------------------------------------
# 主入口 / 单指数核心
# ---------------------------------------------------------------------------

def upsert_index_constituent_sql(index_codes, start_date: Optional[date] = None,
                                 on_progress: Optional[Callable] = None,
                                 stop_event=None) -> dict:
    """同步/回填指定指数的成分股（遍历交易日，仅存"名单变化"的快照）。

    :param index_codes: 必填，指数代码清单（页面勾选），如 ['SHSE.000300', 'SZSE.399006']
    :param start_date: 回填起点；不传则各指数从**核查水位 `checked_to`+1** 增量
        （水位未初始化则退回库内 `MAX(trade_date)+1`；库中完全无数据则从 `listed_date` 起）
    :param on_progress: 进度回调 on_progress(index_code, trade_date, done, total, counts)——
        done/total 为**跨指数累计**；counts 为**跨指数累计**的
        `{saved, skipped, no_data, failed, dirty}`（含当前指数已跑部分），供写 bds_sync_task 用。
        ⚠️ 必须逐日回调计数：任务表的 success/no_data/error 若无实时值，长任务跑到一半时
        前端只能看到「已完成 N 天」而三个计数全是 0，看起来像卡死。
    :param stop_event: threading.Event，置位后跳出循环（当次 gm 调用不可中断）
    :return: {index_code: {"saved","skipped","no_data","failed","dirty","aborted"}}
        saved=留存快照数 / skipped=名单未变或已存跳过 / no_data=gm 空返回 /
        failed=调用或入库失败 / dirty=被只数校验拒存（疑伪快照）/ aborted=提前中止
        （被停止或连续失败超限）；
        若本次是「历史补录」且落了新快照，另带 `reval_from` / `reval_to`——
        提示该区间的指数估值需要重算（历史成分变了）。
    """
    targets = _resolve_indices(index_codes)
    engine = settings.DB_ENGINE

    with SessionLocal() as session:
        trade_dates = _load_trade_dates(session)
        plan = _build_plan(session, targets, start_date, trade_dates)

    total = sum(todo for *_, todo in plan)
    progress = {"done": 0}
    counters: dict = {}
    # 已完成指数的累计计数，供逐日回调拼「跨指数实时计数」= agg + 当前指数 result
    agg = {"saved": 0, "skipped": 0, "no_data": 0, "failed": 0, "dirty": 0}

    logger.info(
        "指数成分股同步开始：%s；start_date=%s；待跑交易日合计 %d",
        ", ".join(f"{code}@{this_start}({todo})" for code, _, this_start, todo in plan),
        start_date.strftime("%Y-%m-%d") if start_date else None,
        total,
    )

    for code, info, this_start, todo in plan:
        if stop_event is not None and stop_event.is_set():
            counters[code] = {"saved": 0, "skipped": 0, "no_data": 0, "failed": 0, "aborted": True}
            continue
        logger.info("->%s（%s）从 %s 起，待跑 %d 个交易日",
                    code, info.get("sec_name"), this_start, todo)
        counters[code] = _backfill_inner(
            engine, code, this_start, trade_dates, on_progress, stop_event, progress, total, agg)
        for k in agg:  # 该指数已结束 ⇒ 并入跨指数累计，下一个指数的回调从这里接着加
            agg[k] += counters[code].get(k, 0) or 0

        # gm 致命错误 → 后续指数不再跑，直接结束
        if counters[code].get("gm_fatal"):
            logger.error("[致命] gm 终端不可用，中止剩余指数回填")
            break

    # logger.info("指数成分股同步结束：%s", counters)
    return counters


def _backfill_inner(engine, index_code: str, start_date: date, trade_dates: list,
                    on_progress: Optional[Callable], stop_event,
                    progress: dict, total: int, agg: dict) -> dict:
    """单指数核心：从 start_date 起遍历交易日，若成分名单变化则留存快照。

    断点续跑：先取该指数已存交易日集合，命中即跳过（零 gm 调用），后端重启后自动续补。
    核查水位：每个**处理过**的交易日都推进 `checked_to`（含失败日），失败的日另记
    `gap_days` / `first_gap`；增量起点 = `checked_to+1`（见 `_incremental_start`）。
    """
    result = {"saved": 0, "skipped": 0, "no_data": 0, "failed": 0, "dirty": 0, "aborted": False}
    expect = expect_count(index_code)

    # ---- 1. 已存交易日 + 比对种子（取 start_date **之前**的最近满编快照）----
    with SessionLocal() as session:
        saved_dates = {
            r[0] for r in session.query(IndexConstituent.trade_date)
            .filter(IndexConstituent.index_code == index_code)
            .distinct()
            .all()
        }
        # 窗口内已存快照的名单：skip 命中时用它把 seed 推进到当日。
        # 不加载的话，seed 会一直停在窗口之前，与旧名单反复比对 ⇒ 误判"名单变化"，
        # 凭空多存一条与已有快照完全重复的冗余行（2026-10-20 事故根因之二）。
        window_snaps: dict = {}
        for d, sym in (session.query(IndexConstituent.trade_date, IndexConstituent.symbol)
                       .filter(IndexConstituent.index_code == index_code,
                               IndexConstituent.trade_date >= start_date)
                       .all()):
            window_snaps.setdefault(d, set()).add(sym)

        # 种子只取"窗口之前"的快照：保证与顺序构建一致，不因窗口起点凭空多存一条。
        # **优先取最近一个满编快照**：库里可能已存历史伪快照（见 docs/成分股伪快照核查_2026-10-20.md），
        # 拿脏快照当基准会让只数校验失明（base 不满编 ⇒ gate 关闭 ⇒ 脏名单全照落）。
        # 无满编快照（建仓期 78→100、沪深300 早期 299 只）才退回最近快照，此时 gate 自然关闭。
        prior_rows = (
            session.query(IndexConstituent.trade_date, func.count())
            .filter(IndexConstituent.index_code == index_code,
                    IndexConstituent.trade_date < start_date)
            .group_by(IndexConstituent.trade_date)
            .order_by(IndexConstituent.trade_date.desc())
            .all()
        )
        seed_date = prior_rows[0][0] if prior_rows else None
        if seed_date is not None and expect:
            seed_date = next((d for d, c in prior_rows if c == expect), seed_date)
        seed = None
        if seed_date is not None:
            seed = {
                r[0] for r in session.query(IndexConstituent.symbol)
                .filter(IndexConstituent.index_code == index_code,
                        IndexConstituent.trade_date == seed_date)
                .all()
            }
        wm = load_watermark(session, index_code)

    orig_checked_to = wm["checked_to"] if wm else None
    if wm is None:  # 水位未初始化（新指数）
        wm = {"checked_to": None, "last_change_date": None, "constituent_count": None,
              "gap_days": 0, "first_gap": None, "no_data_days": 0,
              "dirty_days": 0, "first_dirty": None}
    # 补核查：起点不晚于最早未确认日 ⇒ 本次重算 gap（先清零，本次再失败的重新累加）
    if wm["first_gap"] and start_date <= wm["first_gap"]:
        logger.info("->%s 补核查（起点 %s ≤ 最早未确认日 %s），gap 计数重算",
                    index_code, start_date, wm["first_gap"])
        wm["gap_days"], wm["first_gap"] = 0, None
    # 拒存日同理：重跑该区间会重新判定，计数先清零
    if wm["first_dirty"] and start_date <= wm["first_dirty"]:
        logger.info("->%s 补核查（起点 %s ≤ 最早拒存日 %s），脏日计数重算",
                    index_code, start_date, wm["first_dirty"])
        wm["dirty_days"], wm["first_dirty"] = 0, None

    consec_fail = 0
    pending = {"n": 0}

    def _flush():
        try:
            flush_watermark(engine, index_code, wm)
            pending["n"] = 0
        except Exception as e:  # 水位写失败不中断同步：快照已落，重跑会把水位补上来
            logger.error("->%s 水位写入失败（快照不受影响，重跑可补）：%s", index_code, e)

    def _advance(td: date, kind: str):
        """推进水位。kind: saved / skipped / no_data / failed / dirty。

        统一规则：**每个处理过的日都推进 `checked_to`**。因为增量起点 = 水位+1，
        不推进就会卡死在那天（某个历史日永久失败 ⇒ 水位永久停滞，比不改更糟）。
        失败日计入 `gap_days`、被拒存的伪快照日计入 `dirty_days`——两者都不会被自动
        重试，必须各自留痕（混在一起就无法区分「gm 挂了」和「gm 返回脏数据」）。
        """
        if wm["checked_to"] is None or td > wm["checked_to"]:
            wm["checked_to"] = td
        if kind == "failed":
            wm["gap_days"] += 1
            if wm["first_gap"] is None or td < wm["first_gap"]:
                wm["first_gap"] = td
        elif kind == "no_data":
            wm["no_data_days"] += 1
        elif kind == "dirty":
            wm["dirty_days"] += 1
            if wm["first_dirty"] is None or td < wm["first_dirty"]:
                wm["first_dirty"] = td
        pending["n"] += 1
        if kind in ("saved", "failed", "dirty") or pending["n"] >= WATERMARK_FLUSH_EVERY:
            _flush()

    def _mark_change(td: date, count: int):
        """记录名单变化；只有不早于当前 `last_change_date` 的变化才改写成分数。"""
        if wm["last_change_date"] is None or td >= wm["last_change_date"]:
            wm["last_change_date"] = td
            wm["constituent_count"] = count

    def _tick(td: date):
        progress["done"] += 1
        if on_progress is not None:
            # 跨指数累计 = 已完成指数(agg) + 本指数已跑(result)，逐日回传供任务表实时落库
            on_progress(index_code, td, progress["done"], total,
                        {k: agg[k] + (result.get(k, 0) or 0) for k in agg})

    # ---- 2. 逐日遍历 ----
    try:
        for td in trade_dates:
            if td < start_date:
                continue
            if stop_event is not None and stop_event.is_set():
                result["aborted"] = True
                break

            # 已存交易日：跳过 gm 调用，零开销（仍计入进度，保证断点续跑后进度能跑满）
            if td in saved_dates:
                result["skipped"] += 1
                # ⚠️ 只跳过 gm，不跳过状态推进：seed 必须跟进到当日名单，
                # 否则下一交易日会拿窗口前的旧名单比对，把"早就存过的变化"重报一次。
                # ⚠️ 但「干净基准只被干净快照替换」：窗口内已存的伪快照若把 seed 带脏，
                # 后续判据的 gate 会因 base 不满编而失明。
                if td in window_snaps:
                    snap = window_snaps[td]
                    gate_clean = expect and seed is not None and len(seed) == expect
                    if not (gate_clean and len(snap) != expect):
                        seed = snap
                _advance(td, "skipped")
                _tick(td)
                continue

            # ---- 调 gm（单日失败不中断整指数；gm 致命错误立即中止所有指数）----
            try:
                df = _call_gm(index_code, td)
            except Exception as e:
                raw = str(e)
                if _is_gm_fatal(raw):
                    # gm 终端断连等致命错误：立即中止，不等 MAX_CONSEC_FAIL
                    result["failed"] += 1
                    result["aborted"] = True
                    result["gm_fatal"] = True
                    _advance(td, "failed")
                    logger.error("[致命] gm 终端不可用，中止 %s 回填：%s", index_code, raw[:120])
                    break
                result["failed"] += 1
                consec_fail += 1
                _advance(td, "failed")
                logger.warning("->%s %s 调用失败（连续 %d/%d 次）：%s",
                               index_code, td, consec_fail, MAX_CONSEC_FAIL, e)
                _tick(td)
                time.sleep(min(1.0 * consec_fail, 5.0))
                if consec_fail >= MAX_CONSEC_FAIL:
                    result["aborted"] = True
                    logger.error("->%s 连续失败 %d 次，中止该指数（请检查 gm 终端 / 配额）",
                                 index_code, MAX_CONSEC_FAIL)
                    break
                continue

            consec_fail = 0
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))  # 仅在实际调过 gm 时限速

            if df is None or df.empty:
                result["no_data"] += 1
                logger.info("->%s %s gm 返回空数据，跳过", index_code, td)
                _advance(td, "no_data")
                _tick(td)
                continue

            current = set(df["symbol"])
            if current == seed:
                # 名单未变 ⇒ 不留快照（weight 漂移不另存）
                result["skipped"] += 1
                _advance(td, "skipped")
                _tick(td)
                continue

            # 伪快照拦截：名单「看起来变了」不等于真调仓，只数校验不通过就拒存。
            # 拒存时 seed 保持不动（仍是最后一份干净名单），后续日继续与它比对——
            # 抖动期会逐日被拒，直到 gm 恢复返回正常只数才落库。
            ok, reason = _judge_change(current, expect, seed)
            if not ok:
                result["dirty"] += 1
                logger.warning("->%s %s 名单只数异常（%s），拒存（疑 gm 伪快照）",
                               index_code, td, reason)
                _advance(td, "dirty")
                _tick(td)
                continue
            if reason == "near_miss":
                logger.info("->%s %s 名单缺 1 只（%d<%d），放行落库（拒存会退回更旧的名单）",
                            index_code, td, len(current), expect)

            try:
                _save_snapshot(engine, df, td)
            except Exception as e:
                result["failed"] += 1
                _advance(td, "failed")
                logger.error("->%s %s 入库失败：%s", index_code, td, e)
                _tick(td)
                continue

            # ⚠️ 顺序硬约束：快照已 commit（_save_snapshot 内部提交）后才推进水位
            result["saved"] += 1
            _mark_change(td, len(current))
            _advance(td, "saved")
            seed = current
            saved_dates.add(td)
            logger.info("->%s %s 成分名单变化，留存快照（%d 只）", index_code, td, len(current))
            _tick(td)
    finally:
        _flush()

    # 补录联动：起点早于原水位且落了新快照 ⇒ 历史区间成分变了，估值必须重算
    if (start_date is not None and orig_checked_to is not None
            and start_date < orig_checked_to and result["saved"] > 0):
        result["reval_from"] = start_date.isoformat()
        result["reval_to"] = (wm["checked_to"] or start_date).isoformat()
        logger.warning("->%s 历史成分变更（%s ~ %s，%d 个新快照），需重算该区间的指数估值",
                       index_code, result["reval_from"], result["reval_to"], result["saved"])

    logger.info("->%s 完成：%s（水位 checked_to=%s，gap=%d，脏日=%d）",
                index_code, result, wm["checked_to"], wm["gap_days"], wm["dirty_days"])
    return result
