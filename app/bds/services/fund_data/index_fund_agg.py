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

财务指标派生（2026-11-17 增）：
- 三表落库后，从库内该指数聚合行（balance+income）派生 bds_finance_deriv 指标
  （整体法，比率类 ×100 存百分比，与 gm 个股行口径一致），sec_type='index' 幂等落库；
  deriv 无成分股可加总的字段（roe_weight 加权口径、ebitda_toi 无折旧摊销科目）落 NULL
- deriv 派生自三表聚合行（确定性可重算，避免 start_date 场景下平均类/YoY
  找不到上年参考行的边界问题）；2026-11-24 起：起点之后无报告期（三表 computed
  均空、库内三表聚合行未变）时跳过 deriv 重算，避免幂等空转的假行数

性能约定：每指数每表一次 IN 查询（uk(symbol, rpt_date) 支撑），pandas 内存聚合，
避免逐 symbol 查询；量级每指数每表约 80 行聚合结果。
"""
import logging
from bisect import bisect_right
from datetime import date

import pandas as pd
from sqlalchemy import Numeric

from server_fast.common.db import SessionLocal
from server_fast.app.bds.models import (
    FinanceDeriv,
    FundBalance,
    FundCashflow,
    FundIncome,
    IndexConstituent,
)

logger = logging.getLogger("uvicorn.error")

# 参与聚合的三张财务报表（FinanceDeriv 不做成分股加总：比率类指标逐股求和无业务意义，
# 指数行由 _compute_deriv_rows 从三表聚合行整体法派生）
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
    """按 (symbol=index_code, rpt_date) 幂等覆盖写入聚合行，返回写入行数。

    覆盖率（snap_cons_cnt/agg_cons_cnt）直接从 computed 行字典逐行读取——
    三表聚合行每报告期对应各自时点快照，覆盖率本就逐行不同；FinanceDeriv
    派生行无这两个键，天然落 NULL，无需调用侧区分。
    """
    saved = 0
    for row in computed:
        values = row["values"]
        existing = (
            db.query(model)
            .filter(model.symbol == index_code, model.rpt_date == row["rpt_date"])
            .first()
        )
        # 元数据 kwarg 条件构造：coverage 两列是三表专有，FinanceDeriv 没有
        meta = {
            "symbol": index_code,
            "rpt_date": row["rpt_date"],
            "pub_date": row["pub_date"],
            "rpt_type": row["rpt_type"],
            "data_type": None,
            "sec_type": "index",
        }
        # 覆盖率逐行写入：三表聚合行带键（快照数/聚合数），deriv 行不带键落 NULL
        if "snap_cons_cnt" in row:
            meta["snap_cons_cnt"] = row["snap_cons_cnt"]
            meta["agg_cons_cnt"] = row["agg_cons_cnt"]
        if existing is None:
            db.add(model(**meta, **values))
        else:
            # 全历史重算口径：无条件覆盖（聚合结果由成员与数据确定，非增量追加）
            existing.pub_date = row["pub_date"]
            existing.rpt_type = row["rpt_type"]
            existing.data_type = None
            existing.sec_type = "index"
            if "snap_cons_cnt" in row:
                existing.snap_cons_cnt = row["snap_cons_cnt"]
                existing.agg_cons_cnt = row["agg_cons_cnt"]
            for col, val in values.items():
                setattr(existing, col, val)
        saved += 1
    db.commit()
    return saved


# ==================== 财务指标派生（整体法） ====================
#
# 由三表聚合行派生 bds_finance_deriv 指数行。设计取舍：
# - 比率类 ×100 存百分比、周转天数存天，与 gm 个股行单位一致，前端展示零分支
# - gm 专属口径落 NULL：roe_weight（按资本存续时间加权，指数层面无法复现）、
#   ebitda_toi（三表无折旧摊销科目）
# - 近似口径：roic/ebit_toi 的 EBIT≈利润总额+财务费用（利息费用以财务费用近似）、
#   投入资本≈短期借款+长期借款+归母权益（无应付债券科目）；acct_rcv_turnover_days
#   仅含应收账款（无应收票据科目）
# - 依赖上年参考行的指标（roe_avg/周转类/YoY）：期初取上年末(12-31)聚合行、
#   上年同期取上年同月日聚合行，缺失落 NULL（各指数起始后的第一年受影响）


def _div(numer, denom):
    """None 安全除法：分母为 0/None 返回 None。"""
    if numer is None or not denom:
        return None
    return numer / denom


def _pct(numer, denom):
    """None 安全除法后 ×100（比率类存百分比，与 gm 个股口径一致）。"""
    val = _div(numer, denom)
    return val * 100 if val is not None else None


def _add(*vals):
    """None 安全求和：任一项为 None 返回 None。"""
    if any(v is None for v in vals):
        return None
    return sum(vals)


def _sub(a, b):
    """None 安全减法。"""
    if a is None or b is None:
        return None
    return a - b


def _avg(cur, prev):
    """None 安全平均（期初+期末)/2。"""
    if cur is None or prev is None:
        return None
    return (cur + prev) / 2


def _yoy(cur, prev):
    """None 安全同比增长率（×100 存百分比）。"""
    if cur is None or not prev:
        return None
    return (cur / prev - 1) * 100


def _load_index_agg_rows(db, model, index_code):
    """读回该指数已落库的三表聚合行（sec_type='index'），供 deriv 派生。

    :return: {rpt_date: {"pub_date", "rpt_type", "values": {科目: 值}}}；无行返回 {}
    """
    numeric_cols = [c.name for c in model.__table__.columns if isinstance(c.type, Numeric)]
    rows = db.query(
        model.rpt_date, model.pub_date, model.rpt_type,
        *[getattr(model, c) for c in numeric_cols],
    ).filter(model.symbol == index_code, model.sec_type == "index").all()
    by_rpt = {}
    for rpt, pub, rtype, *vals in rows:
        if rpt is None:
            continue
        by_rpt[rpt] = {"pub_date": pub, "rpt_type": rtype, "values": dict(zip(numeric_cols, vals))}
    return by_rpt


def _compute_deriv_rows(bal_by_rpt, inc_by_rpt):
    """由聚合行派生财务指标（纯计算，不写库），返回 computed 行列表。

    以 income ∪ balance 的报告期为驱动；仅两表均缺某科目时对应指标落 NULL。
    行结构同三表聚合：{"rpt_date", "pub_date", "rpt_type", "values"}。
    """
    computed = []
    for rpt in sorted(set(bal_by_rpt) | set(inc_by_rpt)):
        bal = bal_by_rpt.get(rpt, {}).get("values", {})
        inc = inc_by_rpt.get(rpt, {}).get("values", {})
        bal_prev = bal_by_rpt.get(date(rpt.year - 1, 12, 31), {}).get("values", {})
        inc_prev = inc_by_rpt.get(date(rpt.year - 1, rpt.month, rpt.day), {}).get("values", {})

        # EBIT 近似：利润总额 + 财务费用；投入资本近似：有息负债(短借+长借) + 归母权益
        ebit = _add(inc.get("ttl_prof"), inc.get("exp_fin"))
        icap = _add(bal.get("sht_ln"), bal.get("lt_ln"), bal.get("ttl_eqy_pcom"))
        # 平均类指标的分母参考值（期初=上年末聚合行）
        eqy_avg = _avg(bal.get("ttl_eqy_pcom"), bal_prev.get("ttl_eqy_pcom"))
        ast_avg = _avg(bal.get("ttl_ast"), bal_prev.get("ttl_ast"))
        rcv_avg = _avg(bal.get("acct_rcv"), bal_prev.get("acct_rcv"))
        invt_avg = _avg(bal.get("invt"), bal_prev.get("invt"))

        values = {
            "roe": _pct(inc.get("net_prof_pcom"), bal.get("ttl_eqy_pcom")),
            # gm 加权 ROE 按资本存续时间加权，指数层面无法复现，恒 NULL
            "roe_weight": None,
            "roe_avg": _pct(inc.get("net_prof_pcom"), eqy_avg),
            "roa": _pct(inc.get("net_prof"), bal.get("ttl_ast")),
            "roic": _pct(ebit, icap),
            "sale_gpm": _pct(_sub(inc.get("inc_oper"), inc.get("cost_oper")), inc.get("inc_oper")),
            "sale_npm": _pct(inc.get("net_prof"), inc.get("inc_oper")),
            # 三表无折旧摊销科目，EBITDA 不可算，恒 NULL
            "ebitda_toi": None,
            "ebit_toi": _pct(ebit, inc.get("ttl_inc_oper")),
            "ast_liab_rate": _pct(bal.get("ttl_liab"), bal.get("ttl_ast")),
            "curr_rate": _pct(bal.get("ttl_cur_ast"), bal.get("ttl_cur_liab")),
            "quick_rate": _pct(_sub(bal.get("ttl_cur_ast"), bal.get("invt")), bal.get("ttl_cur_liab")),
            "liab_eqy_rate": _pct(bal.get("ttl_liab"), bal.get("ttl_eqy")),
            "ttl_ast_turnover_rate": _pct(inc.get("ttl_inc_oper"), ast_avg),
            # 360 × 平均应收账款 / 营业收入（天；无应收票据科目，口径近似）
            "acct_rcv_turnover_days": _div(rcv_avg * 360 if rcv_avg is not None else None, inc.get("inc_oper")),
            "inv_turnover_days": _div(invt_avg * 360 if invt_avg is not None else None, inc.get("cost_oper")),
            "net_prof_pcom_yoy": _yoy(inc.get("net_prof_pcom"), inc_prev.get("net_prof_pcom")),
            "ttl_inc_oper_yoy": _yoy(inc.get("ttl_inc_oper"), inc_prev.get("ttl_inc_oper")),
            "net_prof_yoy": _yoy(inc.get("net_prof"), inc_prev.get("net_prof")),
            "ttl_asset_yoy": _yoy(bal.get("ttl_ast"), bal_prev.get("ttl_ast")),
        }

        pubs = [m["pub_date"] for m in (bal_by_rpt.get(rpt), inc_by_rpt.get(rpt)) if m and m["pub_date"]]
        rtypes = [m["rpt_type"] for m in (bal_by_rpt.get(rpt), inc_by_rpt.get(rpt)) if m and m["rpt_type"]]
        computed.append({
            "rpt_date": rpt,
            "pub_date": max(pubs) if pubs else None,
            "rpt_type": rtypes[0] if rtypes else None,
            "values": values,
        })
    return computed


def aggregate_index_fund(index_codes, start_date=None):
    """对每个指数聚合三大财务报表并派生财务指标（纯库内计算，不调 gm）。

    单指数失败不中断后续指数。

    :param index_codes: 指数代码列表
    :param start_date: 重算起点（date，可选）。不传=对该指数全历史重算覆盖；
        传入=仅三表聚合该日及之后的报告期，更早报告期的已有聚合行保持不动；
        deriv 派生始终全历史重算
    :return: (steps, errors)
        steps: {index_code: 三表+deriv 合计落库行数}，-1 表示该指数整体失败
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
    """单指数聚合三张表并派生财务指标，返回合计落库行数。

    无快照/跳过原因写入 errors[index_code]。
    start_date（可选）：仅聚合该日及之后的报告期，更早报告期不重算不覆盖；
    起点之后无报告期（三表 computed 均空）时跳过 deriv 派生——库内三表聚合行
    未变，重算 deriv 只是幂等空转（2026-11-24 用户定稿，配合未来起点 400 拦截）。
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
        any_computed = False  # 本次是否有报告期聚合行产出（决定 deriv 是否重算）
        for model in AGG_MODELS:
            df, numeric_cols = _fetch_fund_df(db, model, all_syms)
            if start_date is not None:
                # 重算起点：仅聚合该日及之后的报告期，更早报告期不重算不覆盖
                df = df[df["rpt_date"] >= start_date]
            computed, reasons = _compute_index_rows(df, numeric_cols, snap_map, snap_dates)
            if computed:
                any_computed = True
            if df.empty:
                # 区分归因：起点过滤后为空 ≠ 成分股真缺数据（2026-11-24，配合未来起点 400 拦截）
                reasons = ([f"起点 {start_date} 之后无报告期"] if start_date is not None
                           else ["成分股无该表数据"])
            total_saved += _upsert_index_rows(db, model, index_code, computed)
            if reasons:
                reasons_by_model[model] = reasons
        # ---- 财务指标派生（整体法）：三表落库后从库内聚合行重算，幂等覆盖 ----
        # 起点之后无报告期 ⇒ 三表聚合行未变，deriv 重算只是空转，跳过（2026-11-24）
        if any_computed:
            deriv_rows = _compute_deriv_rows(
                _load_index_agg_rows(db, FundBalance, index_code),
                _load_index_agg_rows(db, FundIncome, index_code),
            )
            total_saved += _upsert_index_rows(db, FinanceDeriv, index_code, deriv_rows)
    if reasons_by_model:
        errors[index_code] = "；".join(
            f"{TABLE_LABELS[m]}：{'；'.join(rs)}" for m, rs in reasons_by_model.items()
        )[:_ERROR_MAX_LEN]
    return total_saved
