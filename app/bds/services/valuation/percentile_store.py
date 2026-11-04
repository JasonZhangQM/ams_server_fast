# -*- coding: utf-8 -*-
"""`bds_valuation_percentile` 落表服务（2026-11-04，P2 估值分位按日落盘 B/C 阶段）。

职责：把「每标的每交易日的 3/5/10 年 PE/PB/PS/股息率分位」固化成按 `as_of` 追加的历史表，
把首屏请求链路里同步现算 2.4 s 的分位搬出请求路径。

设计要点（docs/估值分位按日落盘方案_2026-11-04.md）：
- 分位是 `f(subject, metric, window, as_of | 截至 as_of 的上游序列)` 的纯函数，`as_of`
  已把「窗口每天滑动」参数化；时间推移只追加新行，上游历史修订才覆盖旧行。
- 计算统一调公共内核 `percentile_stat.daily_percentiles`（滑动窗口 O(n log n)），
  **禁止复用 `get_series`**（其默认下界 `end-3650 天` 比 10y 对日左界晚 2~3 天，
  会静默少取 1~2 个交易日样本）。
- 指数侧「非空即有效」、个股侧按 `clean_value` 清洗，**两套规则故意不统一**。
- 无效值日照常落行，但按 metric 独立置 status 并把该 metric 的 3 个分位列全置 NULL；
  禁止整行清空（个股 dy_ttm 有 1,136 个 NULL 日，不能连 PE/PB/PS 一起丢）。
- 🔴 修订检测主判据是 Python 侧源序列指纹 `input_fp`，**绝不用 `MAX(update_time)`**：
  `market_data/daily_valuation.py` 对已存在行无条件 setattr 覆盖，重复同步同一天
  会刷时间戳但值没变 ⇒ 100% 误报。
- 写入走 `upsert_df_to_db` + **显式 `fields_pct_update` 白名单**，`note` 人工列不在白名单
  （哨兵：重算不覆盖人工值）。
- 归属 bds（🔒4.12：bds 不能被依赖、禁 import irs）。L1 的 subject 清单是 bds 侧常量，
  端点可显式传 `subjects` 覆盖；新增 irs 标的须同步维护常量或在卡片里传参。

模式：M1 增量追加 / M2 全量重建（默认、推荐）/ M3 修订区间重算。三者调用同一个纯函数，
区别只在「写哪些 as_of 行」——计算始终用全序列（窗口需要历史），全量组装约 5.4 s。
M2 另有「快跳过」：源序列指纹 + 源末日 + 窗口口径三者未变时，重写与不写逐值全等，
直接跳过该 subject（实测把第二次全量覆盖写从 57 s 降到只拉源序列的约 3 s）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Callable, Optional

import pandas as pd
from sqlalchemy import text

from server_fast.app.bds.services.valuation.percentile_stat import (
    PCT_METRICS, PCT_PREFIX, WINDOW_KEYS,
    clean_value, daily_percentiles, shift_years, window_cuts,
)
from server_fast.common.db import SessionLocal
from server_fast.common.utils import upsert_df_to_db
from server_fast.config import settings

logger = logging.getLogger("uvicorn.error")

TABLE = "bds_valuation_percentile"
MIN_WINDOW_SAMPLES = 250  # 与 stock_valuation 同源：n 低于它说明窗口实际覆盖不足

# L1 范围（推荐值，§13 第 2 项）：2 个指数 + 4 只 A 股，共 26,816 行。
# 来源 = irs 标的池里「能算出估值分位」的 6 个（GC00Y / SPX 被过滤），
# 但**这里不 import irs**（🔒4.12），端点可用 subjects 参数覆盖。
L1_SUBJECTS: tuple[str, ...] = (
    "SHSE.000300", "SHSE.000905",
    "SHSE.600036", "SHSE.600900", "SZSE.000333", "SZSE.300750",
)

UNIQUE_KEYS = ["subject_type", "symbol", "as_of"]

# 🔒4.9 显式白名单：只覆盖派生列，绝不含 id / create_time / update_time / note。
# 漏传 update_columns 会让 upsert_df_to_db 默认「除唯一键外全更」，
# 将来加人工列时就会把它悄悄覆盖掉（§10 哨兵 4）。
fields_pct_update: list[str] = [
    "current_pe", "current_pb", "current_ps", "current_dy",
    "pe_pct_3y", "pe_pct_5y", "pe_pct_10y",
    "pb_pct_3y", "pb_pct_5y", "pb_pct_10y",
    "ps_pct_3y", "ps_pct_5y", "ps_pct_10y",
    "dy_pct_3y", "dy_pct_5y", "dy_pct_10y",
    "pe_stat", "pb_stat", "ps_stat", "dy_stat",
    "status_pe", "status_pb", "status_ps", "status_dy", "status_note",
    "windows_short", "window_cut",
    "first_date", "raw_n",
    "cleaned_n_pe", "cleaned_n_pb", "cleaned_n_ps", "cleaned_n_dy",
    "source_as_of", "input_fp", "calc_at",
]

def _pct_cols() -> list[str]:
    """12 个分位列名，单一来源（避免两处手写列清单漂移）。"""
    return [f"{PCT_PREFIX[m]}_pct_{k.split('_')[1]}"
            for m in PCT_METRICS for k in WINDOW_KEYS]


PCT_COLUMNS = _pct_cols()


# ---------------------------------------------------------------------------
# 源序列
# ---------------------------------------------------------------------------

def source_end_date(db) -> Optional[date]:
    """上游估值源末日。🔒七·4：必须 ORDER BY DESC LIMIT 1，禁 MAX()（实测 17.9s vs 0.002s）。"""
    return db.execute(text(
        "SELECT trade_date FROM bds_daily_valuation ORDER BY trade_date DESC LIMIT 1"
    )).scalar()


def resolve_subject_types(db, subjects: list[str]) -> list[tuple[str, str]]:
    """把代码清单分成 (index|stock, symbol)，用上游估值表实际存在的指数代码判定。"""
    codes = set(db.execute(text(
        "SELECT DISTINCT index_code FROM bds_index_valuation")).scalars().all())
    return [("index" if s in codes else "stock", s) for s in subjects]


def load_source(db, subject_type: str, symbol: str) -> list[tuple]:
    """直接查源表**全序列**（4 指标一次拉齐），禁止走 get_series。

    指数 `bds_index_valuation` 是整体法聚合结果，个股 `bds_daily_valuation` 是原始估值；
    两者列名一致，故只有表名 / 键名不同。
    """
    table = "bds_index_valuation" if subject_type == "index" else "bds_daily_valuation"
    key = "index_code" if subject_type == "index" else "symbol"
    return db.execute(text(
        f"SELECT trade_date, pe_ttm, pb_mrq, ps_ttm, dy_ttm FROM {table} "
        f"WHERE {key} = :c ORDER BY trade_date"
    ), {"c": symbol}).all()


def fingerprint(raw: list[tuple]) -> str:
    """Python 侧源序列指纹（🔒6.9：集合指纹禁 MD5(GROUP_CONCAT())）。

    逐行拼 `trade_date|4 值` 后 sha256，值用原样 str（Decimal 字符串化稳定）。
    用于判「上游历史有没有被修订」，比 `MAX(update_time)` 可靠得多。
    """
    h = hashlib.sha256()
    for r in raw:
        d = r[0]
        parts = [d.isoformat() if hasattr(d, "isoformat") else str(d)]
        parts += ["" if v is None else str(v) for v in r[1:5]]
        h.update("|".join(parts).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _valid(subject_type: str, metric: str, v) -> bool:
    """指数侧非空即有效；个股侧按 clean_value。两套规则故意不统一（§2.3）。"""
    if v is None:
        return False
    return True if subject_type == "index" else clean_value(metric, float(v))


# ---------------------------------------------------------------------------
# 组装行
# ---------------------------------------------------------------------------

@lru_cache(maxsize=30000)
def _cuts_json(as_of: date) -> str:
    """窗口左界 JSON 缓存：同一交易日跨 6 个 subject 重复出现，序列化一次即可。"""
    return json.dumps(window_cuts(as_of), ensure_ascii=False, separators=(",", ":"))


def build_rows(db, subject_type: str, symbol: str, raw: list[tuple],
               source_as_of: Optional[date], calc_at: datetime,
               fp: str, only_dates: Optional[set] = None) -> list[dict]:
    """一个 subject 的全历史宽表行（一 as_of 一行）。

    `current_*` 存**当日原始值**（个股未清洗，可为负 / NULL），只作审计；
    分位公式里的 `current` 是「截至 as_of 的最后一个有效值」，由 daily_percentiles 内部取。
    """
    if not raw:
        return []
    dates = [r[0] for r in raw]
    first_date = dates[0]
    raw_n = len(raw)

    daily: dict[str, dict] = {}
    seq_by_metric: dict[str, list] = {}
    for j, m in enumerate(PCT_METRICS, start=1):
        seq = [(r[0], float(r[j])) for r in raw if _valid(subject_type, m, r[j])]
        seq_by_metric[m] = seq
        # 增量模式只让内核对目标日构造 stat dict（滑动状态仍逐日维护）
        daily[m] = daily_percentiles(seq, only_dates=only_dates) if seq else {}

    # 每个 metric 是否全序列都无有效值（no_data），区别于某日无效（loss）
    no_data = {m: not seq_by_metric[m] for m in PCT_METRICS}

    rows = []
    iter_raw = raw if only_dates is None else [r for r in raw if r[0] in only_dates]
    for r in iter_raw:
        as_of = r[0]
        # 本行的短窗口标记（按 metric 分组，逐行 O(4×3)，不是全历史 O(n²)）
        short_row: dict[str, list[str]] = {}
        for m in PCT_METRICS:
            drow = daily[m].get(as_of)
            if not drow:
                continue
            bad = [k for k in WINDOW_KEYS if drow[k]["n"] < MIN_WINDOW_SAMPLES]
            if bad:
                short_row[m] = bad
        row: dict = {
            "subject_type": subject_type, "symbol": symbol, "as_of": as_of,
            "first_date": first_date, "raw_n": raw_n,
            "source_as_of": source_as_of, "input_fp": fp, "calc_at": calc_at,
            "window_cut": _cuts_json(as_of),
            "windows_short": json.dumps(short_row, ensure_ascii=False) if short_row else None,
        }
        statuses, notes = [], []
        for j, m in enumerate(PCT_METRICS, start=1):
            pre = PCT_PREFIX[m]
            v = r[j]
            row[f"current_{pre}"] = None if v is None else float(v)
            row[f"cleaned_n_{pre}"] = len(seq_by_metric[m])

            if no_data[m]:
                st = "no_data"
                stats = None
                notes.append(f"{m} 无有效样本")
            elif as_of not in daily[m]:
                # 当天无有效值 ⇒ 该 metric 分位全 NULL，其余指标照常写
                st = "loss"
                stats = None
                notes.append(f"{m} 当日无有效值")
            else:
                st = "ok"
                stats = {k: daily[m][as_of][k] for k in WINDOW_KEYS}

            row[f"status_{pre}"] = st
            statuses.append(st)
            for k in WINDOW_KEYS:
                row[f"{pre}_pct_{k.split('_')[1]}"] = (
                    (stats or {}).get(k, {}).get("pct") if stats else None)
            row[f"{pre}_stat"] = json.dumps(stats, ensure_ascii=False) if stats else None

        row["status_note"] = "；".join(dict.fromkeys(notes))[:255] if notes else None
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 旧值比对 / upsert / 孤儿清理
# ---------------------------------------------------------------------------

def _norm(v) -> Optional[float]:
    """空值归一化（🔒6.6）：None / NaN / '' 一律视作同一个「空」。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, 2)


def audit_diff(db, planned: list[dict], keys: set) -> dict:
    """重算前后逐值比对 12 个分位列，写进 task.result.audit。"""
    empty = {"changed": 0, "max_pct": 0.0, "appeared": 0, "vanished": 0, "samples": []}
    if not planned:
        return empty
    if not keys:
        return {**empty, "appeared": len(planned)}
    where = " OR ".join("(subject_type=:t{} AND symbol=:s{})".format(i, i)
                         for i in range(len(keys)))
    params: dict = {}
    for i, (t, s) in enumerate(sorted(keys)):
        params[f"t{i}"] = t
        params[f"s{i}"] = s
    cols = ", ".join(PCT_COLUMNS)
    old_rows = db.execute(text(
        f"SELECT subject_type, symbol, as_of, {cols} FROM {TABLE} WHERE {where}"
    ), params).all()
    old = {(r[0], r[1], r[2]): tuple(_norm(x) for x in r[3:]) for r in old_rows}

    new = {(r["subject_type"], r["symbol"], r["as_of"]):
           tuple(_norm(r.get(c)) for c in PCT_COLUMNS) for r in planned}
    changed, appeared, vanished, max_pct, samples = 0, 0, 0, 0.0, []
    for k, nv in new.items():
        ov = old.get(k)
        if ov is None:
            appeared += 1
            continue
        diffs = [(PCT_COLUMNS[i], ov[i], nv[i])
                 for i in range(len(PCT_COLUMNS)) if ov[i] != nv[i]]
        if diffs:
            changed += 1
            for c, a, b in diffs:
                if a is not None and b is not None:
                    max_pct = max(max_pct, abs(b - a))
            if len(samples) < 5:
                samples.append({"key": [str(k[0]), k[1], k[2].isoformat()],
                                "diffs": diffs[:3]})
    vanished = len(set(old) - set(new))
    return {"changed": changed, "max_pct": round(max_pct, 2),
            "appeared": appeared, "vanished": vanished, "samples": samples}


def upsert_rows(planned: list[dict], chunk: int = 2000) -> int:
    """幂等 upsert（ON DUPLICATE KEY UPDATE 只覆盖白名单列），返回写行数。"""
    if not planned:
        return 0
    df = pd.DataFrame(planned)
    cols = UNIQUE_KEYS + [c for c in fields_pct_update if c in df.columns]
    # 空值归一化交给 upsert_df_to_db 内部的 df.replace({np.nan: None})；
    # 这里不再自行 where/astype，避免把 datetime / date 列变成 object 后类型漂移
    df = df[cols]
    total = 0
    for i in range(0, len(df), chunk):
        total += upsert_df_to_db(
            df.iloc[i:i + chunk], TABLE, settings.DB_ENGINE,
            unique_keys=UNIQUE_KEYS, update_columns=fields_pct_update)
    return total


def delete_orphans(db, keep_subjects: set[tuple[str, str]]) -> int:
    """清理「不在本次 subject 集合」的孤儿行（§10 哨兵 5）。

    ⚠️ 按 subject 维度删，不按 as_of 维度：M3 区间重算只写 [D, 末日]，
    若按 as_of 差集删会把 D 之前的历史行全误删。
    """
    existing = set(db.execute(text(
        f"SELECT DISTINCT subject_type, symbol FROM {TABLE}")).all())
    orphans = existing - keep_subjects
    for t, s in orphans:
        db.execute(text(f"DELETE FROM {TABLE} WHERE subject_type=:t AND symbol=:s"),
                   {"t": t, "s": s})
    db.commit()
    return len(orphans)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def plan(subjects: Optional[list[str]] = None, mode: str = "M2",
         from_date: Optional[date] = None) -> dict:
    """任务计划：subject 清单、模式、预计行数（供 total_count 与前端展示）。"""
    subs = list(subjects or L1_SUBJECTS)
    if mode not in ("M1", "M2", "M3", "auto"):
        raise ValueError("mode 必须是 M1 / M2 / M3 / auto")
    if mode == "M3" and from_date is None:
        raise ValueError("M3 必须给 from_date（修订起始日）")
    with SessionLocal() as db:
        pairs = resolve_subject_types(db, subs)
        end = source_end_date(db)
        n_days = 0
        # 只数行，不要把全序列拉进内存（plan 在 HTTP 请求线程里跑）
        for t, s in pairs:
            table = "bds_index_valuation" if t == "index" else "bds_daily_valuation"
            key = "index_code" if t == "index" else "symbol"
            n_days += int(db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {key}=:c"), {"c": s}).scalar() or 0)
    return {"subjects": subs, "subject_pairs": pairs, "mode": mode,
            "from_date": from_date, "source_as_of": end, "total_count": n_days}


def rebuild(mode: str = "M2", subjects: Optional[list[str]] = None,
            from_date: Optional[date] = None,
            on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """执行重算，返回结构化审计（写进 bds_sync_task.result）。

    `on_progress(done, total)` 由后台线程传入，用于刷任务进度。
    """
    t0 = time.perf_counter()
    subs = list(subjects or L1_SUBJECTS)
    if mode not in ("M1", "M2", "M3", "auto"):
        raise ValueError("mode 必须是 M1 / M2 / M3 / auto")
    if mode == "M3" and from_date is None:
        raise ValueError("M3 必须给 from_date（修订起始日）")

    calc_at = datetime.now()
    planned: list[dict] = []
    fps: dict[str, str] = {}
    as_of_range = [None, None]

    with SessionLocal() as db:
        pairs = resolve_subject_types(db, subs)
        source_as_of = source_end_date(db)
        # 表内每 subject 的最新行（M1 起点 / M2 快速跳过 / auto 的 fp 判据）。
        # window_cut 一并读出：口径变更后旧行的左界仍是 365 天口径，必须强制重建。
        # 🔒七·4：只取近 15 天（走 k_bvp_asof 索引），别为拿 6 行扫 26,816 行 JSON 列；
        # 某 subject 落后超过 15 天 ⇒ prev=None ⇒ 退化为全量重建，是安全降级而非误判。
        cut_recent = (source_as_of - timedelta(days=15)) if source_as_of else date(1970, 1, 1)
        latest_rows = db.execute(text(
            f"SELECT subject_type, symbol, as_of, input_fp, window_cut FROM {TABLE} "
            f"WHERE as_of >= :cut"
        ), {"cut": cut_recent}).all()
        latest: dict[tuple[str, str], tuple] = {}
        for r in latest_rows:
            k = (r[0], r[1])
            if k not in latest or r[2] > latest[k][0]:
                latest[k] = (r[2], r[3], r[4])

        skipped_subjects: list[str] = []
        cut_stale_subjects: list[str] = []
        written_keys: set[tuple[str, str]] = set()

        for i, (t, s) in enumerate(pairs):
            raw = load_source(db, t, s)
            fp = fingerprint(raw)
            fps[s] = fp
            prev = latest.get((t, s))
            # 口径闸：表内最新行的 window_cut 不是日历对日 ⇒ 无论哪个模式都强制全量重建。
            # 缺了它，auto 会在「fp 与源末日都没变」时直接 skip，旧口径永远刷不过来。
            stale = bool(prev) and not _cut_is_calendar(prev)
            if stale:
                cut_stale_subjects.append(s)

            # ---- M2 快速跳过（恒定性推论，§0.1）----
            # 源序列指纹、源末日、窗口口径三者都没变时，「全量重写」与「不写」逐值全等。
            # 实测第二次全量覆盖写 26,816 行要 57 s（ON DUPLICATE KEY UPDATE 全字段），
            # 跳过可把日常重算降到只拉源序列的 3 s。口径变更（365 天 → 日历对日）
            # 会让 window_cut 不符 ⇒ 强制重建；上游历史修订会让 fp 变 ⇒ 强制重建。
            if mode == "M2" and prev and prev[1] == fp and source_as_of \
                    and prev[0] >= source_as_of and not stale:
                skipped_subjects.append(s)
                if on_progress:
                    on_progress(i + 1, len(pairs))
                continue

            # ---- 先定「写哪些 as_of」，再让 build_rows 只算这些行 ----
            # 计算内核仍逐日维护滑动窗口状态（口径不变），只是不为非目标日
            # 构造 6 字段 stat dict / 做 round / 序列化 JSON ⇒ 增量模式快一个量级。
            eff_mode, low = mode, None
            if stale:
                # 口径变更是全局事件，局部重写没有意义 ⇒ 无条件全量
                eff_mode, low = "M2", None
            elif mode == "auto":
                if prev and prev[1] == fp and source_as_of and prev[0] >= source_as_of:
                    eff_mode = "skip"       # 输入未变且源末日未变：无需写
                elif prev and prev[1] != fp:
                    eff_mode, low = "M3", from_date or _earliest_change_date(db, t, s, prev[0])
                else:
                    eff_mode, low = "M1", (prev[0] if prev else None)
            elif mode == "M1":
                low = prev[0] if prev else None
            elif mode == "M3":
                low = from_date

            if eff_mode == "skip":
                skipped_subjects.append(s)
                if on_progress:
                    on_progress(i + 1, len(pairs))
                continue

            only_dates = None
            if low is not None:
                only_dates = ({r[0] for r in raw if r[0] > low} if eff_mode == "M1"
                              else {r[0] for r in raw if r[0] >= low})
                if not only_dates:
                    if on_progress:
                        on_progress(i + 1, len(pairs))
                    continue
            rows = build_rows(db, t, s, raw, source_as_of, calc_at, fp,
                              only_dates=only_dates)
            planned.extend(rows)
            written_keys.add((t, s))
            if rows:
                a, b = rows[0]["as_of"], rows[-1]["as_of"]
                as_of_range[0] = min(as_of_range[0], a) if as_of_range[0] else a
                as_of_range[1] = max(as_of_range[1], b) if as_of_range[1] else b
            if on_progress:
                on_progress(i + 1, len(pairs))

        # audit 只比「本次实际写了的 subject」，否则被跳过的行会被误算成 vanished
        audit = audit_diff(db, planned, written_keys)
        rows_rowcount = upsert_rows(planned)
        # MySQL 的 rowcount 在「值未变的 ON DUPLICATE KEY UPDATE」时返回 0，
        # 它不等于写行数 ⇒ 审计里两个量分开记，rows_upserted 才是实际 UPSERT 行数
        rows_upserted = len(planned)
        # 孤儿清理仍按全部 L1 subject 判定（不能把跳过的 subject 当孤儿删掉）
        all_keys = {(t, s) for t, s in pairs}
        orphans = delete_orphans(db, all_keys) if mode in ("M2", "auto") else 0

    duration_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "mode": mode, "window_mode": "calendar_anniversary",
        "metrics": list(PCT_METRICS), "windows": list(WINDOW_KEYS),
        "source_as_of": source_as_of.isoformat() if source_as_of else None,
        "subjects": subs,
        "as_of_range": [d.isoformat() if d else None for d in as_of_range],
        "rows_total": len(planned), "rows_upserted": rows_upserted,
        "rows_rowcount": rows_rowcount, "orphans_deleted": orphans,
        "skipped_subjects": skipped_subjects, "skipped_count": len(skipped_subjects),
        "cut_stale_subjects": cut_stale_subjects,
        "audit": audit,
        "input_fp": {k: v[:16] for k, v in fps.items()},
        "duration_ms": duration_ms,
    }


def _earliest_change_date(db, subject_type: str, symbol: str,
                           prev_as_of: date) -> date:
    """auto 模式：以「上一版最后一行」为保守起点，宁多算不少算（§7.1）。"""
    return prev_as_of


def _cut_is_calendar(prev: tuple) -> bool:
    """表内最新行的 10y 左界是否已是**日历对日**口径。

    M2 快速跳过的三道闸之一：口径从 365 天切到对日时，源 fp 与源末日都没变，
    只有 window_cut 能暴露「表还是旧口径」⇒ 必须强制全量重建（§7.2）。
    """
    if len(prev) < 3 or not prev[2]:
        return False
    try:
        cut = json.loads(prev[2]) if isinstance(prev[2], str) else (prev[2] or {})
    except (TypeError, ValueError):
        return False
    return cut.get("pct_10y") == shift_years(prev[0], 10).isoformat()


# ---------------------------------------------------------------------------
# 读取侧（§6.1）
# ---------------------------------------------------------------------------

READ_COLUMNS = ["subject_type", "symbol", "as_of", "source_as_of",
                "status_pe", "status_pb", "status_ps", "status_dy",
                "window_cut", "windows_short"] + PCT_COLUMNS


def read_latest_rows(recent_days: int = 15) -> dict[tuple[str, str], dict]:
    """取每个 subject 的最新一行。

    不用 `MAX(as_of)` 全表聚合（🔒七·4），改用 `WHERE as_of >= 源末日-15 天`
    走 `k_bvp_asof` 索引，Python 侧分组取最大 ⇒ 天然容忍各 subject 末日不同。
    """
    with SessionLocal() as db:
        end = source_end_date(db)
        if end is None:
            return {}
        cut = end - timedelta(days=recent_days)
        cols = ", ".join(READ_COLUMNS)
        rows = db.execute(text(
            f"SELECT {cols} FROM {TABLE} WHERE as_of >= :cut"
        ), {"cut": cut}).mappings().all()
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        d = dict(r)
        k = (d["subject_type"], d["symbol"])
        if k not in out or d["as_of"] > out[k]["as_of"]:
            out[k] = d
    return out


def read_percentiles() -> dict[tuple[str, str], Optional[float]]:
    """`um_today.percentiles()` 契约：{(symbol, metric): pct_10y}，只取 pe/pb。

    仍只给 pe_ttm / pb_mrq（新增的 PS / 股息率本期只落库备用，不进请求链路契约）；
    status != ok 不给分位；表空 / 过期 / 单 subject 缺失 ⇒ 该键不出现 ⇒ 页面显示 `—`。
    不回落实时计算（§6.3）。
    """
    out = {}
    for (_, symbol), row in read_latest_rows().items():
        for metric, pre in (("pe_ttm", "pe"), ("pb_mrq", "pb")):
            if row.get(f"status_{pre}") != "ok":
                continue
            out[(symbol, metric)] = _norm(row.get(f"{pre}_pct_10y"))
    return out
