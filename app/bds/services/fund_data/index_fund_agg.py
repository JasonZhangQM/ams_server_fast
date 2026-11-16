# -*- coding: utf-8 -*-
"""指数成分股财务聚合：三大报表按指数成分股逐科目求和（纯库内计算，不调 gm）。

口径（2026-11-16 定稿）：
- 结果与个股混存同一批表（bds_fund_balance / bds_fund_income / bds_fund_cashflow），
  聚合行 sec_type='index'，个股行维持默认 'stock'，用 sec_type 区分
- 时点成分：对报告期 R，取该指数成分快照 S = MAX(trade_date) WHERE trade_date <= R
  （快照表 bds_index_constituent 按「名单变化」落全量名单，非每日落行）
- 早于首份快照的报告期跳过不聚合，原因经 errors 上抛（no_data 语义，写入任务 errors）
- 覆盖率落库：snap_cons_cnt=快照成分股总数，agg_cons_cnt=实际参与汇总的成分股数
- 仅聚合标准报告期（03-31/06-30/09-30/12-31）：gm 个股数据含 rpt_type=99 的快报类
  特殊披露（月末日 rpt_date），不具指数层面分析意义，不入聚合；组内 0 行的报告期
  （成员当时均无财报）同样不落行
- 幂等：每次对该指数全历史重算覆盖，唯一键 (symbol, rpt_date)

性能约定：每指数每表一次 IN 查询（uk(symbol, rpt_date) 支撑），pandas 内存聚合，
避免逐 symbol 查询；量级每指数每表约 80 行聚合结果。
"""
import logging
from bisect import bisect_right

import pandas as pd
from sqlalchemy import Numeric

from server_fast.common.db import SessionLocal
from server_fast.app.bds.models import (
    FundBalance,
    FundCashflow,
    FundIncome,
    IndexConstituent,
)

logger = logging.getLogger("uvicorn.error")

# 参与聚合的三张财务报表（FinanceDeriv 不动：比率类指标逐股求和无业务意义）
AGG_MODELS = [FundBalance, FundIncome, FundCashflow]

TABLE_LABELS = {
    FundBalance: "资产负债表",
    FundIncome: "利润表",
    FundCashflow: "现金流量表",
}

# errors 值截断长度（与 fund_reports.py 惯例一致）
_ERROR_MAX_LEN = 500

# 标准报告期的月日集合；快报等特殊披露（rpt_type=99，月末日）不具指数层面分析意义，排除
_STANDARD_RPT_MD = {"03-31", "06-30", "09-30", "12-31"}


def _load_snapshot_map(db, index_code):
    """载入该指数全部成分快照，返回 (snap_map, snap_dates) 或 None。

    snap_map: {trade_date: [symbol, ...]}（快照落全量名单，uk 保证单日内 symbol 不重）
    snap_dates: 升序快照交易日列表，供「报告期 → 时点快照」二分定位
    """
    rows = (
        db.query(IndexConstituent.trade_date, IndexConstituent.symbol)
        .filter(IndexConstituent.index_code == index_code)
        .all()
    )
    if not rows:
        return None
    snap_map = {}
    for td, sym in rows:
        snap_map.setdefault(td, []).append(sym)
    return snap_map, sorted(snap_map)


def _fetch_fund_df(db, model, symbols):
    """单次 IN 查询取该表全部成分股行，转 pandas。

    只取元数据列 + 全部数值科目列（Numeric 类型列，随模型定义自动同步），
    symbol IN (...) 复合查询由 uk_bds_fund_*(symbol, rpt_date) 唯一索引支撑。

    :return: (DataFrame, 数值科目列名列表)
    """
    numeric_cols = [c.name for c in model.__table__.columns if isinstance(c.type, Numeric)]
    select_cols = [model.symbol, model.pub_date, model.rpt_date, model.rpt_type] + [
        getattr(model, c) for c in numeric_cols
    ]
    rows = db.query(*select_cols).filter(model.symbol.in_(symbols)).all()
    col_names = ["symbol", "pub_date", "rpt_date", "rpt_type"] + numeric_cols
    df = pd.DataFrame(rows, columns=col_names) if rows else pd.DataFrame(columns=col_names)
    return df, numeric_cols


def _compute_index_rows(df, numeric_cols, snap_map, snap_dates):
    """逐报告期聚合（纯计算，不写库），返回 (聚合行列表, 跳过原因列表)。

    每个报告期 R：选快照 trade_date = max(trade_date where <= R)，无则跳过并记原因；
    有则 members=该快照名单，数值科目逐列求和（跳过 NaN，全 NaN → None 落 NULL），
    pub_date=组内最大、rpt_type=组内值（同 R 只有一行/股）。
    仅处理标准报告期；组内 0 行（成员当时均无财报）跳过不落行。
    """
    computed, reasons = [], []
    if df.empty:
        return computed, reasons
    # 排除快报类非标准报告期（月末日），避免指数行出现 2022-11-30 之类日期
    md = pd.to_datetime(df["rpt_date"]).dt.strftime("%m-%d")
    df = df[md.isin(_STANDARD_RPT_MD)]
    if df.empty:
        return computed, reasons
    for rpt in sorted(df["rpt_date"].dropna().unique()):
        pos = bisect_right(snap_dates, rpt) - 1
        if pos < 0:
            reasons.append(f"{rpt} 早于首份快照(首份={snap_dates[0]})，跳过")
            continue
        members = snap_map[snap_dates[pos]]
        sub = df[(df["rpt_date"] == rpt) & (df["symbol"].isin(members))]
        if sub.empty:
            continue
        values = {}
        for col in numeric_cols:
            vals = sub[col].dropna()
            # 用内置 sum 而非 pandas .sum()：对象列全 NaN 时 pandas 会返回 0，语义应为 NULL
            values[col] = sum(vals) if len(vals) else None
        pub = sub["pub_date"].dropna()
        rpt_type = sub["rpt_type"].dropna()
        computed.append({
            "rpt_date": rpt,
            "snap_cons_cnt": len(members),
            "agg_cons_cnt": int(sub["symbol"].nunique()),
            "pub_date": pub.max() if len(pub) else None,
            "rpt_type": rpt_type.iloc[0] if len(rpt_type) else None,
            "values": values,
        })
    return computed, reasons


def _upsert_index_rows(db, model, index_code, computed):
    """按 (symbol=index_code, rpt_date) 幂等覆盖写入聚合行，返回写入行数。"""
    saved = 0
    for row in computed:
        values = row["values"]
        existing = (
            db.query(model)
            .filter(model.symbol == index_code, model.rpt_date == row["rpt_date"])
            .first()
        )
        if existing is None:
            db.add(model(
                symbol=index_code,
                rpt_date=row["rpt_date"],
                pub_date=row["pub_date"],
                rpt_type=row["rpt_type"],
                data_type=None,
                sec_type="index",
                snap_cons_cnt=row["snap_cons_cnt"],
                agg_cons_cnt=row["agg_cons_cnt"],
                **values,
            ))
        else:
            # 全历史重算口径：无条件覆盖（聚合结果由成员与数据确定，非增量追加）
            existing.pub_date = row["pub_date"]
            existing.rpt_type = row["rpt_type"]
            existing.data_type = None
            existing.sec_type = "index"
            existing.snap_cons_cnt = row["snap_cons_cnt"]
            existing.agg_cons_cnt = row["agg_cons_cnt"]
            for col, val in values.items():
                setattr(existing, col, val)
        saved += 1
    db.commit()
    return saved


def aggregate_index_fund(index_codes, start_date=None):
    """对每个指数聚合三大财务报表（纯库内计算，不调 gm）。

    单指数失败不中断后续指数。

    :param index_codes: 指数代码列表
    :param start_date: 重算起点（date，可选）。不传=对该指数全历史重算覆盖；
        传入=仅聚合该日及之后的报告期，更早报告期的已有聚合行保持不动
    :return: (steps, errors)
        steps: {index_code: 三表合计落库行数}，-1 表示该指数整体失败
        errors: {index_code: 失败/跳过原因}，值截断 500 字符
    """
    steps, errors = {}, {}
    for index_code in index_codes:
        try:
            steps[index_code] = _aggregate_one_index(index_code, errors, start_date)
        except Exception as e:
            raw = f"{type(e).__name__}: {e}"
            logger.error(f"指数财务聚合失败 {index_code}: {raw}")
            steps[index_code] = -1
            errors[index_code] = raw[:_ERROR_MAX_LEN]
    return steps, errors


def _aggregate_one_index(index_code, errors, start_date=None):
    """单指数聚合三张表，返回合计落库行数；无快照/跳过原因写入 errors[index_code]。

    start_date（可选）：仅聚合该日及之后的报告期，更早报告期不重算不覆盖。
    """
    with SessionLocal() as db:
        snap = _load_snapshot_map(db, index_code)
        if snap is None:
            errors[index_code] = "无成分快照"
            return 0
        snap_map, snap_dates = snap
        all_syms = {s for members in snap_map.values() for s in members}
        total_saved = 0
        reasons_by_model = {}
        for model in AGG_MODELS:
            df, numeric_cols = _fetch_fund_df(db, model, all_syms)
            if start_date is not None:
                # 重算起点：仅聚合该日及之后的报告期，更早报告期不重算不覆盖
                df = df[df["rpt_date"] >= start_date]
            computed, reasons = _compute_index_rows(df, numeric_cols, snap_map, snap_dates)
            if df.empty:
                reasons = ["成分股无该表数据"]
            total_saved += _upsert_index_rows(db, model, index_code, computed)
            if reasons:
                reasons_by_model[model] = reasons
    if reasons_by_model:
        errors[index_code] = "；".join(
            f"{TABLE_LABELS[m]}：{'；'.join(rs)}" for m, rs in reasons_by_model.items()
        )[:_ERROR_MAX_LEN]
    return total_saved
