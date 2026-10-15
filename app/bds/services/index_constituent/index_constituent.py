# -*- coding: utf-8 -*-
"""指数成分股同步 / 历史回填。

口径（详见 docs/指数成分股历史回填方案_v2.1_2026-10-14.md）：

- **指数清单由调用方显式传入**（页面勾选），不再自动遍历 `Config.INDEX_CODE`；
  未知代码 / `data_source='yfinance'` 的指数一律抛 `ValueError`（router 转 400），不静默跳过。
- **变更键 = 纯 symbol 集合**（weight 只写不比对）。本表按"调仓事件"存储，仅在成分股
  **名单发生变化**的交易日留存快照，期间权重漂移不另存；查询某日成分 = 取
  `trade_date <= D` 的最大快照。
- **起点优先级**：`start_date`（调用方指定）> 库内 `MAX(trade_date)+1`（增量）>
  `max(listed_date, FLOOR_DATE)`（首次全量）。
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
from sqlalchemy import func
from sqlalchemy.orm import Session

from server_fast.config import settings
from server_fast.common.db import SessionLocal
from server_fast.common.utils import call_with_timeout, upsert_df_to_db
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import IndexConstituent, TradeDate

logger = logging.getLogger("uvicorn.error")

# 回填全局下限：沪深300 发布年，A 股成分股数据较完整的起点
FLOOR_DATE = date(2005, 1, 1)

# gm 限速：仅在实际调过 gm 后 sleep（实测 gm 单次响应约 120ms，故无需更长的固定间隔）
SLEEP_MIN, SLEEP_MAX = 0.2, 0.35

# 连续失败上限：达到即中止该指数，避免 gm 终端离线 / 触发流控时白刷数千次
MAX_CONSEC_FAIL = 10

# 单次 gm 调用超时（gm 终端未启动时会无限阻塞）
GM_TIMEOUT = 30

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


def _incremental_start(session: Session, index_code: str) -> Optional[date]:
    """库内该指数最新 trade_date 的下一天；库中无数据时返回 None。"""
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
    """首次全量起点 = max(listed_date, FLOOR_DATE)；未配置 listed_date 则取 FLOOR_DATE。"""
    listed = _to_date(info.get("listed_date"))
    return max(listed, FLOOR_DATE) if listed else FLOOR_DATE


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


def _save_snapshot(engine, df: pd.DataFrame) -> int:
    """把该日全量成分股写入（含当日 weight，即便为 0），走 UK 幂等 upsert。"""
    out = df[["index", "symbol", "weight", "trade_date"]].copy()
    out = out.rename(columns={"index": "index_code"})
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y-%m-%d").dt.date
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
    :param start_date: 回填起点；不传则各指数各自从库内最新 trade_date+1 增量
        （库中无该指数数据则从 max(listed_date, 2005-01-01) 起）
    :param on_progress: 进度回调 on_progress(index_code, trade_date, done, total)——
        done/total 为**跨指数累计**，供写 bds_sync_task 用
    :param stop_event: threading.Event，置位后跳出循环（当次 gm 调用不可中断）
    :return: {index_code: {"saved","skipped","no_data","failed","aborted"}}
        saved=留存快照数 / skipped=名单未变或已存跳过 / no_data=gm 空返回 /
        failed=调用或入库失败 / aborted=提前中止（被停止或连续失败超限）
    """
    targets = _resolve_indices(index_codes)
    engine = settings.DB_ENGINE

    with SessionLocal() as session:
        trade_dates = _load_trade_dates(session)
        plan = _build_plan(session, targets, start_date, trade_dates)

    total = sum(todo for *_, todo in plan)
    progress = {"done": 0}
    counters: dict = {}

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
            engine, code, this_start, trade_dates, on_progress, stop_event, progress, total)

        # gm 致命错误 → 后续指数不再跑，直接结束
        if counters[code].get("gm_fatal"):
            logger.error("[致命] gm 终端不可用，中止剩余指数回填")
            break

    # logger.info("指数成分股同步结束：%s", counters)
    return counters


def _backfill_inner(engine, index_code: str, start_date: date, trade_dates: list,
                    on_progress: Optional[Callable], stop_event,
                    progress: dict, total: int) -> dict:
    """单指数核心：从 start_date 起遍历交易日，若成分名单变化则留存快照。

    断点续跑：先取该指数已存交易日集合，命中即跳过（零 gm 调用），后端重启后自动续补。
    """
    result = {"saved": 0, "skipped": 0, "no_data": 0, "failed": 0, "aborted": False}

    # ---- 1. 已存交易日 + 比对种子（取 start_date **之前**的最新快照的 symbol 集合）----
    with SessionLocal() as session:
        saved_dates = {
            r[0] for r in session.query(IndexConstituent.trade_date)
            .filter(IndexConstituent.index_code == index_code)
            .distinct()
            .all()
        }
        # 种子只取"窗口之前"的最新快照：保证与顺序构建一致，不因窗口起点凭空多存一条；
        # 窗口内无更早快照时 seed=None ⇒ 窗口首日必然留存（查询"某日成分"才有依据）
        prior_dates = [d for d in saved_dates if d < start_date]
        seed = None
        if prior_dates:
            seed_date = max(prior_dates)
            seed = {
                r[0] for r in session.query(IndexConstituent.symbol)
                .filter(IndexConstituent.index_code == index_code,
                        IndexConstituent.trade_date == seed_date)
                .all()
            }

    consec_fail = 0

    def _tick(td: date):
        progress["done"] += 1
        if on_progress is not None:
            on_progress(index_code, td, progress["done"], total)

    # ---- 2. 逐日遍历 ----
    for td in trade_dates:
        if td < start_date:
            continue
        if stop_event is not None and stop_event.is_set():
            result["aborted"] = True
            break

        # 已存交易日：跳过，零 gm 开销（仍计入进度，保证断点续跑后进度能跑满）
        if td in saved_dates:
            result["skipped"] += 1
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
                logger.error("[致命] gm 终端不可用，中止 %s 回填：%s", index_code, raw[:120])
                break
            result["failed"] += 1
            consec_fail += 1
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
            _tick(td)
            continue

        current = set(df["symbol"])
        if current == seed:
            # 名单未变 ⇒ 不留快照（weight 漂移不另存）
            result["skipped"] += 1
            _tick(td)
            continue

        try:
            _save_snapshot(engine, df)
        except Exception as e:
            result["failed"] += 1
            logger.error("->%s %s 入库失败：%s", index_code, td, e)
            _tick(td)
            continue

        result["saved"] += 1
        seed = current
        saved_dates.add(td)
        logger.info("->%s %s 成分名单变化，留存快照（%d 只）", index_code, td, len(current))
        _tick(td)

    logger.info("->%s 完成：%s", index_code, result)
    return result
