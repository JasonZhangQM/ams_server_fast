# -*- coding: utf-8 -*-
"""标的维度的横切口径：估值分位 + 证伪自动判定（2026-11-13 起，今日待办已下线）。

本模块曾承载「今日页（GET /irs/today）」的到价清单聚合，2026-11-13 随今日待办页一并
删除；保留两项仍被其它链路消费的能力：

- `percentiles()`：PE/PB 10 年分位（契约在 bds `percentile_store.read_percentiles()`），
  被标的监测列表、风险看板、档位 Tab 等多处调用；
- `judge_one / judge_list`：证伪候选判定（只读、不改 status）。

`_is_hit` / `_unfinished_plans` 仍被 `um_simulate`（推演页命中剧本）复用，故保留。

依赖方向：bills ↔ irs 平级、可互依（规则 4.12，2026-10-29 改）；bds 只能被依赖。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from server_fast.app.irs.models import UnderlyingPlan
from server_fast.app.irs.service.um_plan import effective_trigger

# 历史白名单（方案 P0-4 / P2-1）：读表后不再由本模块过滤，保留供审计与 um_version 注释引用。
# 落表默认 subject 由 bds 侧 subject_diagnostics() 运行时从 irs_underlying_monitor
# 推导（🔒4.12：bds 不 import irs，用表名字符串 SQL 取数），不再是 L1_SUBJECTS 常量。
INDEX_PCT_SYMBOLS = {"SHSE.000300", "SHSE.000905", "SPX"}


def percentiles() -> dict:
    """{(symbol, metric): pct_10y}，失败返回空 dict（分位是纠偏，不是列表主链）。

    列表页 / 详情抽屉 / 今日页 / 证伪判定同样复用本函数补 pe_pct/pb_pct（口径唯一，🔒4.5）。

    **2026-11-05 起改读 `irs_um_percentile`（最新截面表）**：一标的一行、≤8 行，
    由 `POST /irs/sync/um-percentile` 刷新。上一版读 `bds_valuation_percentile`
    （按日历史，26,816 行）中的「每标的最新一行」——那 2.6 万行对页面零价值，
    读取侧还要按 15 天窗口回捞再分组，随标的扩容会线性变慢。

    契约不变：仍是 0~100 数值、仍只取 `pe_ttm` / `pb_mrq` 两个 metric、
    `status != ok` 或表空 / 单标的缺失时该键不出现 ⇒ 页面显示 `—`。
    **不回落实时计算**：那等于慢问题复发加双口径。
    分位专页继续走 bds 实时计算，可作影子双跑的参照系。

    🔴 回滚：换回 `percentile_store.read_percentiles()`（历史表仍在、仍在更新），
    或再往前换回 `index_valuation.get_percentiles` / `stock_valuation.get_percentiles`。
    """
    try:
        from server_fast.app.irs.service import um_percentile
        return um_percentile.read_map()
    except Exception:
        # 表不存在 / 连接异常时不拖垮今日页主链，宁可显示 —
        return {}


def _is_hit(p: UnderlyingPlan, pp: dict, price) -> bool:
    """现价是否满足触发条件（命中 = 到价）。"""
    if price is None or p.is_cancelled:
        return False
    trig = effective_trigger(p, pp)
    if trig is None:
        return False
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    op = p.trigger_op or "lte"
    if op == "lte":
        return px <= trig
    if op == "gte":
        return px >= trig
    if op == "range":
        p2 = float(p.trigger_price2) if p.trigger_price2 is not None else None
        if p2 is None:
            return px >= trig
        return trig <= px <= p2
    return False


def _unfinished_plans(db: Session, symbol: str) -> list[UnderlyingPlan]:
    """未暂停的剧本（长期有效，全部纳入今日命中判定）。"""
    rows = db.query(UnderlyingPlan).filter(UnderlyingPlan.underlying_symbol == symbol).order_by(UnderlyingPlan.seq).all()
    return [p for p in rows if not p.is_cancelled]


# =========================================================================
# P2-2 证伪自动判定（只读，候选不改 status）
# =========================================================================

import re
from server_fast.app.irs.models import UnderlyingInvalidate, UnderlyingMonitor

# judge_expr 格式约定：{metric}{op}{value}，如 price<=3900 / pb_pct>=0.7
_OP_RE = re.compile(r"^(.*?)\s*(<=|>=|==|=|<|>|≤|≥)\s*([0-9]*\.?[0-9]+)\s*$")


def judge_one(db: Session, inv: UnderlyingInvalidate,
              pct_map: Optional[dict] = None) -> Optional[dict]:
    """对一条证伪做候选触发判定（只读，**不改 status**）。

    三类分治（方案 P2-2）：
    - price       → 当前价 UnderlyingMonitor.price
    - percentile  → 指数标的 pe_pct / pb_pct 10y 分位（um_today.percentiles()）
    - manual      → 不做
    - discount    → 暂不做（贴水率需要跨模块查）

    返回格式（可算的才有值，不可算返回 None）：
        {"candidate": True/False, "current_value": float,
         "threshold": float, "op": str, "metric": str}

    关键约束：**只写候选、不改 UnderlyingInvalidate.status**。机器给提示，人做决定。
    """
    if inv.judge in (None, "manual"):
        return None
    if not inv.judge_expr:
        return None
    m = _OP_RE.match(inv.judge_expr.strip())
    if not m:
        return None
    metric = m.group(1).strip()
    op_raw = m.group(2).strip()
    try:
        threshold = float(m.group(3))
    except (TypeError, ValueError):
        return None

    # 统一比较符（Unicode 符号归一并去掉等号前的多余空格）
    op = {"≤": "<=", "≥": ">=", "=": "=="}.get(op_raw, op_raw)
    if op not in ("<", ">", "<=", ">=", "=="):
        return None

    # 数据源解析
    current_value: Optional[float] = None
    if inv.judge == "price" and metric == "price":
        um = (db.query(UnderlyingMonitor.price)
              .filter(UnderlyingMonitor.symbol == inv.symbol).scalar())
        current_value = float(um) if um is not None else None
    elif inv.judge == "percentile" and metric in ("pe_pct", "pb_pct"):
        pct = (pct_map or percentiles()).get((inv.symbol, metric))
        current_value = float(pct) if pct is not None else None
    else:
        return None  # discount / 其他未实现

    if current_value is None:
        return None

    # 执行比较
    ops = {
        "<=": lambda a, b: a <= b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        ">": lambda a, b: a > b,
        "==": lambda a, b: abs(a - b) < 1e-9,
    }
    return {
        "candidate": bool(ops[op](current_value, threshold)),
        "current_value": current_value,
        "threshold": threshold,
        "op": op,
        "metric": metric,
    }


def judge_list(db: Session, symbol: str) -> dict[int, Optional[dict]]:
    """批量判定一个标的的全部证伪（复用一次 percentiles() 调用）。"""
    invs = (db.query(UnderlyingInvalidate)
            .filter(UnderlyingInvalidate.symbol == symbol).all())
    if not invs:
        return {}
    pct_map = percentiles()
    return {inv.id: judge_one(db, inv, pct_map) for inv in invs}