# -*- coding: utf-8 -*-
"""标的监测抽屉「财务分析」Tab 的聚合数据（2026-11-17）。

核心原则（方案 v6，用户已确认）：
- 前端零口径计算：年报序列、净现比、扣非占比、TTM 差分全部后端算好，前端只渲染
- 个股与指数零分流：统一 symbol 查询，deriv 行个股/指数混表靠 sec_type 区分，
  本模块读数不过滤 sec_type（一个 symbol 只会有一种主体类型的行）
- 外盘标的（SPX / GC00Y）无财务数据 ⇒ empty=true HTTP 200 空态，不猜数
- 双口径（方案 v8，2026-11-17）：freq=annual 近 N 年年报（默认 5，N ∈ {3,5,10}）/
  freq=latest 近 12 个标准报告期（含单季差分与单季同比）；响应统一字段 series
- 最新报告期快照卡（含 TTM 差分）

单季口径（方案 v8）：
- 单季 = 本期累计 − 上一日历季度累计（Q1 = 累计本身），资产负债表时点值不做单季
- 单季同比 = 本季单季 vs 去年同期单季（%），去年单季缺失或非正 → None

数据源（bds 只读，🔒4.12 irs → bds 允许）：
- bds_finance_deriv：17 指标直取（个股 gm 口径 / 指数整体法派生，比率均 ×100 存百分比）
- bds_fund_balance / income / cashflow：绝对值（净利润/营收/经营现金流）与派生所需科目
- bds_symbol_info：name 与 subject_type 判定（industry='A股指数'）

查询层派生口径：
- net_cash_ratio = 经营现金流净额 ÷ 归母净利润（年报累计值直除，倍数）
- cut_ratio = 扣非归母 ÷ 归母 ×100（%）
- TTM = 最新报告期累计值 − 去年同期累计值（net_prof/net_prof_cut/revenue/ocf 四个）
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from server_fast.common.db import SessionLocal

# 年报口径：报告期月日集合（快报 rpt_type=99 的月末日天然被过滤）
ANNUAL_MD = "12-31"

# 标准报告期月日集合（freq=latest 展示窗口；快报 rpt_type=99 的非季末月末日天然被过滤）
STANDARD_MD = {"03-31", "06-30", "09-30", "12-31"}

# deriv 直取列（比率 ×100 存百分比 / 周转天数存天，单位口径统一）
DERIV_RATIO_COLS = [
    "roe", "roe_avg", "roic", "roa", "sale_gpm", "sale_npm",
    "ast_liab_rate", "curr_rate", "quick_rate", "liab_eqy_rate",
    "ttl_ast_turnover_rate", "acct_rcv_turnover_days", "inv_turnover_days",
    "net_prof_pcom_yoy", "ttl_inc_oper_yoy", "ttl_asset_yoy",
]
# 三表派生所需科目列（只取这些列，避免拉 34 万行大表的全部 Numeric 列）
BAL_COLS = ["ttl_ast", "ttl_cur_ast", "ttl_cur_liab", "ttl_liab",
            "ttl_eqy_pcom", "ttl_eqy", "sht_ln", "lt_ln", "invt", "acct_rcv"]
INC_COLS = ["ttl_inc_oper", "inc_oper", "net_prof", "net_prof_pcom", "net_prof_pcom_cut"]
CF_COLS = ["net_cf_oper"]

# 三表表名 → 派生所需科目列
_THREE_TABLES = {
    "bds_fund_balance": BAL_COLS,
    "bds_fund_income": INC_COLS,
    "bds_fund_cashflow": CF_COLS,
}


def _f(v) -> Optional[float]:
    """Decimal → float（None 透传），比率类统一 4 位小数。"""
    return None if v is None else round(float(v), 4)


def _r2(v) -> Optional[float]:
    return None if v is None else round(float(v), 2)


def _div(numer, denom):
    """None 安全除法：分母为 0/None 返回 None。"""
    if numer is None or not denom:
        return None
    return numer / denom


def _sub(a, b):
    """None 安全减法。"""
    if a is None or b is None:
        return None
    return a - b


def _subject_type(db: Session, symbol: str, info_row) -> Optional[str]:
    """主体类型判定：industry='A股指数' 最优先，其次看 deriv 行 sec_type，
    最后三表有行按个股处理（历史行 sec_type 可能为 NULL 老口径）。"""
    if info_row is not None and info_row.industry == "A股指数":
        return "index"
    st = db.execute(text(
        "SELECT sec_type FROM bds_finance_deriv WHERE symbol = :s "
        "AND sec_type IS NOT NULL LIMIT 1"), {"s": symbol}).scalar()
    if st in ("index", "stock"):
        return st
    if db.execute(text(
            "SELECT 1 FROM bds_fund_income WHERE symbol = :s LIMIT 1"),
            {"s": symbol}).first():
        return "stock"
    return None


def _load_deriv_rows(db: Session, symbol: str) -> list:
    """deriv 全部行（不过滤 sec_type：一个 symbol 只有一种主体），rpt_date 升序。"""
    cols = ", ".join(["rpt_date", "pub_date"] + DERIV_RATIO_COLS)
    rows = db.execute(text(
        f"SELECT {cols} FROM bds_finance_deriv WHERE symbol = :s ORDER BY rpt_date ASC"),
        {"s": symbol}).all()
    names = ["rpt_date", "pub_date"] + DERIV_RATIO_COLS
    return [dict(zip(names, r)) for r in rows]


def _load_three_table(db: Session, symbol: str) -> dict:
    """三表按 (rpt_date → 科目 dict) 载入，只取派生所需列。

    :return: {表名: {rpt_date: {科目: float|None}}}
    """
    out = {}
    for table, cols in _THREE_TABLES.items():
        sql = f"SELECT rpt_date, {', '.join(cols)} FROM {table} WHERE symbol = :s"
        rows = db.execute(text(sql), {"s": symbol}).all()
        out[table] = {r[0]: dict(zip(cols, r[1:])) for r in rows if r[0] is not None}
    return out


def _annual_deriv(deriv_rows: list, years: int) -> list:
    """取最近 N 个年报的 deriv 指标（升序输出）。"""
    annual = [r for r in deriv_rows
              if r["rpt_date"].strftime("%m-%d") == ANNUAL_MD]
    return annual[-years:] if annual else []


def _rpt_label(rpt: date) -> tuple:
    """报告期标签：(rpt_label, q_tag)，如 ('2025FY', 'FY') / ('2026Q1', 'Q1')。

    非标准报告期（理论上不会出现在序列里）回落 ISO 日期 + 空串。
    """
    q = {"03-31": "Q1", "06-30": "Q2", "09-30": "Q3", "12-31": "FY"}.get(
        rpt.strftime("%m-%d"))
    if q is None:
        return rpt.isoformat(), ""
    return f"{rpt.year}{q}", q


def _prev_quarter(rpt: date) -> Optional[date]:
    """上一个日历季度报告期；非标准报告期返回 None。"""
    md = rpt.strftime("%m-%d")
    if md == "03-31":
        return date(rpt.year - 1, 12, 31)
    if md == "06-30":
        return date(rpt.year, 3, 31)
    if md == "09-30":
        return date(rpt.year, 6, 30)
    if md == "12-31":
        return date(rpt.year, 9, 30)
    return None


# 单季差分科目：输出字段 → (三表表名, 累计值列)
_SQ_COLS = [
    ("sq_net_prof", "bds_fund_income", "net_prof_pcom"),
    ("sq_net_prof_cut", "bds_fund_income", "net_prof_pcom_cut"),
    ("sq_revenue", "bds_fund_income", "ttl_inc_oper"),
    ("sq_ocf", "bds_fund_cashflow", "net_cf_oper"),
]


def _quarterly_enrich(std_rows: list, inc_map: dict, cf_map: dict) -> dict:
    """对标准报告期计算单季差分与单季同比（方案 v8）。

    单季 = 本期累计 − 上一日历季度累计（Q1 = 累计本身）；上期行缺失 → None。
    上期按日历推算而非「序列前一行」：中间缺报告期时差分跨度错误，宁缺毋错。
    单季同比 = 本季 vs 去年同期单季（%），去年单季缺失或非正 → None（负基数无意义）。
    需比展示窗口多取一年历史行，首期同比才可算——本函数对全量 std_rows 计算，
    窗口截取由调用方完成。
    :return: {rpt_date: {"sq_*": 元, "sq_*_yoy": %}}
    """
    table_maps = {"bds_fund_income": inc_map, "bds_fund_cashflow": cf_map}

    def cum(table, rpt, col):
        row = table_maps[table].get(rpt)
        return row.get(col) if row else None

    def sq_val(rpt, table, col):
        cur = cum(table, rpt, col)
        if cur is None:
            return None
        if rpt.month == 3:
            return cur  # Q1：累计值当年起算，单季即累计本身（不可减上年年报）
        prev_rpt = _prev_quarter(rpt)
        prev = cum(table, prev_rpt, col) if prev_rpt else None
        return None if prev is None else cur - prev

    out = {}
    for r in std_rows:
        rpt = r["rpt_date"]
        item = {}
        for key, table, col in _SQ_COLS:
            sq = sq_val(rpt, table, col)
            item[key] = _r2(sq)
            prev_sq = sq_val(_prev_year_same(rpt), table, col)
            if sq is not None and prev_sq is not None and prev_sq > 0:
                item[key + "_yoy"] = _f((sq - prev_sq) / prev_sq * 100)
            else:
                item[key + "_yoy"] = None
        out[rpt] = item
    return out


def build(symbol: str, years: int = 5, freq: str = "annual",
          db: Optional[Session] = None) -> dict:
    """一次返回财务分析 Tab 全部数据。`symbol` 是业务主键（id 会漂移，禁用 id 关联）。

    freq=annual：近 N 年年报（默认 5 年，2026-11-17 用户拍板，与路由 Query(5)、
    前端 years 初值三处同步）；freq=latest：近 12 个标准报告期（含单季差分，
    years 被忽略，期数固定 12）。
    """
    own = db is None
    session = SessionLocal() if own else db
    try:
        from server_fast.app.bds.models import SymbolInfo

        info_row = session.query(SymbolInfo.name, SymbolInfo.industry).filter(
            SymbolInfo.symbol == symbol).first()
        subject_type = _subject_type(session, symbol, info_row)

        deriv_rows = _load_deriv_rows(session, symbol)
        three = _load_three_table(session, symbol)
        bal_map = three["bds_fund_balance"]
        inc_map = three["bds_fund_income"]
        cf_map = three["bds_fund_cashflow"]

        annual_deriv = _annual_deriv(deriv_rows, years)
        annual_rpts = [r["rpt_date"] for r in annual_deriv]

        # ---- 序列：annual=近 N 年年报 / latest=近 12 个标准报告期（含单季差分） ----
        def make_item(r):
            rpt = r["rpt_date"]
            inc = inc_map.get(rpt, {})
            cf = cf_map.get(rpt, {})
            label, q_tag = _rpt_label(rpt)
            item = {
                "rpt_date": rpt.isoformat(),
                "pub_date": r["pub_date"].isoformat() if r["pub_date"] else None,
                "rpt_label": label,
                "q_tag": q_tag,
            }
            item.update({c: _f(r[c]) for c in DERIV_RATIO_COLS})
            npcom = inc.get("net_prof_pcom")
            item["net_cash_ratio"] = _r2(_div(cf.get("net_cf_oper"), npcom))
            item["cut_ratio"] = _f(_pct100(inc.get("net_prof_pcom_cut"), npcom))
            item["net_prof"] = _r2(npcom)
            item["net_prof_cut"] = _r2(inc.get("net_prof_pcom_cut"))
            item["revenue"] = _r2(inc.get("ttl_inc_oper"))
            item["ocf"] = _r2(cf.get("net_cf_oper"))
            return item

        if freq == "latest":
            std_rows = [r for r in deriv_rows
                        if r["rpt_date"].strftime("%m-%d") in STANDARD_MD]
            sq_map = _quarterly_enrich(std_rows, inc_map, cf_map)
            window = std_rows[-12:]
            series = []
            for r in window:
                item = make_item(r)
                item.update(sq_map.get(r["rpt_date"], {}))
                series.append(item)
            series_rpts = [r["rpt_date"] for r in window]
        else:
            series = [make_item(r) for r in annual_deriv]
            series_rpts = annual_rpts

        # ---- 最新报告期快照卡（含 TTM 差分） ----
        # 快照报告期优先取 deriv 最新且有利润表行的报告期（快报 rpt_type=99 无三表行，
        # 会丢失绝对值与 TTM）；无三表行时回落 deriv 最后一行（只展示比率类指标）
        latest = None
        if deriv_rows:
            with_income = [r["rpt_date"] for r in deriv_rows if r["rpt_date"] in inc_map]
            snap_row = next((r for r in reversed(deriv_rows)
                             if r["rpt_date"] in with_income), deriv_rows[-1])
            rpt = snap_row["rpt_date"]
            prev_rpt = _prev_year_same(rpt)
            inc = inc_map.get(rpt, {})
            inc_prev = inc_map.get(prev_rpt, {})
            cf = cf_map.get(rpt, {})
            cf_prev = cf_map.get(prev_rpt, {})
            latest = {
                "rpt_date": rpt.isoformat(),
                "pub_date": snap_row["pub_date"].isoformat() if snap_row["pub_date"] else None,
            }
            latest.update({c: _f(snap_row[c]) for c in DERIV_RATIO_COLS})
            npcom = inc.get("net_prof_pcom")
            latest["net_cash_ratio"] = _r2(_div(cf.get("net_cf_oper"), npcom))
            latest["cut_ratio"] = _f(_pct100(inc.get("net_prof_pcom_cut"), npcom))
            latest["net_prof"] = _r2(npcom)
            latest["net_prof_cut"] = _r2(inc.get("net_prof_pcom_cut"))
            latest["revenue"] = _r2(inc.get("ttl_inc_oper"))
            latest["ocf"] = _r2(cf.get("net_cf_oper"))
            # TTM（滚动 12 个月）= 最新累计 − 去年同期累计 + 上年年报。
            # 统一公式对年报期自身也成立：此时去年同期累计 = 上年年报，两项相消退化为年报值
            inc_annual_prev = inc_map.get(date(rpt.year - 1, 12, 31), {})
            cf_annual_prev = cf_map.get(date(rpt.year - 1, 12, 31), {})
            latest["ttm_net_prof"] = _r2(_ttm(
                npcom, inc_prev.get("net_prof_pcom"), inc_annual_prev.get("net_prof_pcom")))
            latest["ttm_net_prof_cut"] = _r2(_ttm(
                inc.get("net_prof_pcom_cut"), inc_prev.get("net_prof_pcom_cut"),
                inc_annual_prev.get("net_prof_pcom_cut")))
            latest["ttm_revenue"] = _r2(_ttm(
                inc.get("ttl_inc_oper"), inc_prev.get("ttl_inc_oper"),
                inc_annual_prev.get("ttl_inc_oper")))
            latest["ttm_ocf"] = _r2(_ttm(
                cf.get("net_cf_oper"), cf_prev.get("net_cf_oper"),
                cf_annual_prev.get("net_cf_oper")))

        # ---- 元数据与覆盖率 ----
        empty = not deriv_rows and not any(bal_map) and not any(inc_map) and not any(cf_map)
        coverage: dict = {
            "deriv_rows": len(deriv_rows),
            "three_table_rows": len(bal_map) + len(inc_map) + len(cf_map),
            "latest_rpt_date": deriv_rows[-1]["rpt_date"].isoformat() if deriv_rows else None,
            "data_start": series_rpts[0].isoformat() if series_rpts else None,
            "empty": empty,
            "empty_reason": "无财务数据落表（外盘 / 商品类标的）" if empty else None,
        }
        if subject_type == "index":
            snap, agg, pct = _index_coverage_real(session, symbol)
            coverage["snap_cons_cnt"] = snap
            coverage["agg_cons_cnt"] = agg
            coverage["coverage_pct"] = pct

        message = None
        if empty:
            message = "该标的暂无财务数据落表（外盘 / 商品类标的），本 Tab 无财务分析可展示。"
        elif subject_type == "index" and coverage.get("coverage_pct") is not None \
                and coverage["coverage_pct"] < 85:
            message = (f"指数成分覆盖不足（聚合 {coverage.get('agg_cons_cnt')}/快照 "
                       f"{coverage.get('snap_cons_cnt')}），财务指标系统性偏低，判读需谨慎。")
        elif not series:
            message = "该标的暂无所选取径的财务序列，仅展示最新报告期快照。"

        return {
            "symbol": symbol,
            "name": info_row.name if info_row else None,
            "subject_type": subject_type,
            "years": years,
            "freq": freq,
            "coverage": coverage,
            "series": series,
            "latest": latest,
            "message": message,
        }
    finally:
        if own:
            session.close()


def _ttm(cur_cum, prev_cum, annual_prev):
    """TTM = 最新累计 − 去年同期累计 + 上年年报；任一参考行缺失返回 None。"""
    if cur_cum is None or prev_cum is None or annual_prev is None:
        return None
    return cur_cum - prev_cum + annual_prev


def _pct100(numer, denom):
    """None 安全除法 ×100（扣非占比存百分比）。"""
    val = _div(numer, denom)
    return val * 100 if val is not None else None


def _prev_year_same(rpt: date) -> date:
    """去年同期（2/29 → 2/28 clamp）。"""
    try:
        return date(rpt.year - 1, rpt.month, rpt.day)
    except ValueError:
        return date(rpt.year - 1, rpt.month, 28)


def _index_coverage_real(db: Session, symbol: str) -> tuple:
    """指数覆盖率：三表聚合行 MAX(snap_cons_cnt/agg_cons_cnt) → (snap, agg, pct)。"""
    snap = agg = None
    for table in _THREE_TABLES:
        s, a = db.execute(text(
            f"SELECT MAX(snap_cons_cnt), MAX(agg_cons_cnt) FROM {table} "
            "WHERE symbol = :s AND sec_type = 'index'"), {"s": symbol}).first()
        if s is not None and (snap is None or s > snap):
            snap = int(s)
        if a is not None and (agg is None or a > agg):
            agg = int(a)
    pct = _div(agg, snap)
    return snap, agg, None if pct is None else round(pct * 100, 1)


# =========================================================================
# 同步：个股走 gm 4 函数链，指数走纯库内聚合（零分流判定在本函数内完成）
# =========================================================================

_STOCK_STEPS = [
    ("资产负债表", "upsert_fund_balance_sql"),
    ("利润表", "upsert_fund_income_sql"),
    ("现金流量表", "upsert_fund_cashflow_sql"),
    ("财务指标", "upsert_finance_deriv_sql"),
]


def sync(symbol: str) -> dict:
    """按标的触发财务数据同步，返回 {status, message, subject_type, steps, errors}。

    - 个股：依次调 bds 的三表 + deriv 同步函数（gm SDK，函数内已带 30s 超时保护）
    - 指数：aggregate_index_fund（纯库内聚合 + deriv 派生，不调 gm）
    返回结构与 executeSync 前端约定对齐（steps: [{step, result}]）。
    """
    own_db = SessionLocal()
    try:
        from server_fast.app.bds.models import SymbolInfo

        info_row = own_db.query(SymbolInfo.name, SymbolInfo.industry).filter(
            SymbolInfo.symbol == symbol).first()
        subject_type = _subject_type(own_db, symbol, info_row)
    finally:
        own_db.close()

    steps, errors = [], []
    if subject_type == "index":
        # 指数：纯库内聚合（三表 + deriv 派生幂等落库）
        from server_fast.app.bds.services.fund_data.index_fund_agg import aggregate_index_fund

        try:
            s, e = aggregate_index_fund([symbol])
            n = s.get(symbol, -1)
            steps.append({"step": "指数财务聚合", "result": None if n < 0 else n})
            if symbol in e:
                errors.append(f"指数财务聚合：{e[symbol]}")
        except Exception as exc:  # 单指数聚合整体失败兜底
            errors.append(f"指数财务聚合：{type(exc).__name__}: {exc}")
    else:
        # 个股：gm 4 函数链（函数内部已 call_with_timeout(api, 30)，超时自动跳过该步）
        from server_fast.app.bds.services import fund_data

        for label, fname in _STOCK_STEPS:
            try:
                s, e = getattr(fund_data, fname)([symbol])
                n = s.get(symbol, -1)
                steps.append({"step": label, "result": None if n < 0 else n})
                if symbol in e:
                    errors.append(f"{label}：{e[symbol]}")
            except Exception as exc:
                errors.append(f"{label}：{type(exc).__name__}: {exc}")

    ok = all(r["result"] is not None for r in steps) and not errors
    status = "success" if ok else "error"
    mode = "指数聚合" if subject_type == "index" else "个股 gm 链"
    msg = (f"同步完成：{symbol}（{mode}）" if ok
           else f"同步完成但有失败项：{symbol}（{mode}）；" + "；".join(errors))
    return {
        "status": status,
        "message": msg,
        "subject_type": subject_type,
        "steps": steps,
        "errors": errors,
    }
