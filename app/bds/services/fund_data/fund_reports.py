# -*- coding: utf-8 -*-
"""财务数据同步：三大财报（资产负债表/利润表/现金流量表）+ 财务指标。

共用 _upsert_fund_sql 通用 upsert 框架：
- 报告期预判：库内 MAX(rpt_date) 已追平/超过 bds_symbol_info.latest_rpt_date 时
  直接跳过，不调 gm（财报按季更新，避免每天重复调接口）
- gm API fields ≤20 限制通过 _fetch_fund_batched 分批获取 + 元数据列 merge 合并
- pub_date 保留策略：新数据 pub_date < 已有 pub_date 时跳过
- 单 symbol 失败不中断，返回 steps 字典记录 -2/-1/0/>0 四态
"""
import logging
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func
from gm.api import (
    stk_get_fundamentals_balance,
    stk_get_fundamentals_income,
    stk_get_fundamentals_cashflow,
    stk_get_finance_deriv,
)

from server_fast.common.utils import call_with_timeout
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import (
    FundBalance,
    FundIncome,
    FundCashflow,
    FinanceDeriv,
    SymbolInfo,
)

logger = logging.getLogger("uvicorn.error")


# ---- 共用工具函数（daily_valuation 也复用） ----

def _clean_scalar(v):
    """将 pandas NaN 统一转为 None，适配数据库空值存储。"""
    if v is None or pd.isna(v):
        return None
    return v


def _to_date(v):
    """将字符串/datetime 统一转为 date 类型，空值返回 None。"""
    if v is None or pd.isna(v):
        return None
    if isinstance(v, date):
        return v
    return pd.to_datetime(v).date()


# ---- 字段列表（gm API fields 参数，每类 20 个） ----

# 资产负债表
FUND_BALANCE_FIELDS = "mny_cptl,acct_rcv,invt,ttl_cur_ast,fix_ast,lt_eqy_inv,intg_ast,gw,ttl_ncur_ast,ttl_ast,sht_ln,acct_pay,ttl_cur_liab,lt_ln,ttl_ncur_liab,ttl_liab,cptl_rsv,ret_prof,ttl_eqy_pcom,ttl_eqy"

# 利润表
FUND_INCOME_FIELDS = "ttl_inc_oper,inc_oper,ttl_cost_oper,cost_oper,exp_sell,exp_adm,exp_rd,exp_fin,inc_inv,inc_fv_chg,oper_prof,ttl_prof,inc_tax,net_prof,net_prof_pcom,eps_base,eps_dil,inc_noper,exp_noper,ttl_comp_inc"

# 现金流量表
FUND_CASHFLOW_FIELDS = "cash_rcv_sale,cf_in_oper,cash_pur_gds_svc,cash_pay_emp,cash_pay_tax,cf_out_oper,net_cf_oper,cash_rcv_sale_inv,cf_in_inv,pur_fix_intg_ast,net_cf_inv,brw_rcv,cf_in_fin,cash_rpay_brw,net_cf_fin,net_prof,efct_er_chg_cash,net_incr_cash_eq,cash_cash_eq_bgn,cash_cash_eq_end"

# 财务指标
FINANCE_DERIV_FIELDS = "roe,roe_weight,roe_avg,roa,roic,sale_gpm,sale_npm,ebitda_toi,ebit_toi,ast_liab_rate,curr_rate,quick_rate,liab_eqy_rate,ttl_ast_turnover_rate,acct_rcv_turnover_days,inv_turnover_days,net_prof_pcom_yoy,ttl_inc_oper_yoy,net_prof_yoy,ttl_asset_yoy"


def _fetch_fund_batched(symbol, start_date, end_date, api_func, fields_str):
    """通用分批获取财报数据（gm API fields ≤20 限制）。

    将字段按 20 个一批分别请求，再以元数据列为 key 合并，
    返回包含全部字段的完整 DataFrame。

    :param api_func: gm API 函数（stk_get_fundamentals_balance/income/cashflow/finance_deriv）
    :param fields_str: 逗号分隔的字段名字符串（如 FUND_BALANCE_FIELDS）
    :return: 合并后的 DataFrame，或 None
    """
    all_fields = fields_str.split(",")
    batch_size = 20
    dfs = []
    for i in range(0, len(all_fields), batch_size):
        batch_fields = ",".join(all_fields[i:i + batch_size])
        df = call_with_timeout(api_func, timeout=30)(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            fields=batch_fields,
            df=True,
        )
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]
    # 多批数据按元数据列合并（同一 symbol+日期范围返回的行一致）
    merge_cols = ["symbol", "pub_date", "rpt_date", "rpt_type", "data_type"]
    result = dfs[0]
    for df in dfs[1:]:
        result = result.merge(df, on=merge_cols, how="outer")
    return result


def _upsert_fund_sql(symbols, model, fields_str, api_func, log_name, force=False):
    """通用财报数据 upsert 入库（三大报表 + 财务指标共用）。

    报告期预判（force=False 时生效）：
    - 取库内该 symbol 的 MAX(rpt_date)，与 bds_symbol_info.latest_rpt_date
      （东财「最新报告期(含快报)」快照）比对
    - MAX(rpt_date) >= latest_rpt_date ⇒ 该表已是最新报告期，直接跳过，不调 gm
    - 任一方为 NULL（无历史数据 / 快照缺值）⇒ 无法判定，按原增量逻辑请求
    - force=True ⇒ 绕过该判断，强制按增量执行（用于财报更正回补、快报转正式、
      以及怀疑漏数时强刷）

    增量更新策略（未跳过时）：
    - 数据库已有该 symbol 数据：从最新 rpt_date + 1 天开始获取
    - 数据库无该 symbol 数据：从 EARLIEST_START_DATE（1901-01-01）全量获取，
      gm 会自动截断到该股最早报告期（含上市前披露的财务）

    去重规则（按 symbol + rpt_date）：
    - 已有记录且新数据 pub_date >= 已有 pub_date：更新所有字段
    - 已有记录但新数据 pub_date < 已有 pub_date：跳过该行
    - 无已有记录：插入新记录

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的结果。

    :param model: ORM 模型类（FundBalance/FundIncome/FundCashflow/FinanceDeriv）
    :param fields_str: 字段名逗号分隔字符串
    :param api_func: gm API 函数
    :param log_name: 日志中的表名（如"资产负债表"）
    :param force: True 时跳过「已是最新报告期」判断
    :return: steps 字典 {symbol: 保存条数 / -1 失败 / -2 跳过（已是最新报告期）}
    """
    _mdl = model
    _field_list = fields_str.split(",")
    steps = {}
    logger.info(f"{log_name}数据获取并导入")
    end_date = datetime.now().strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            with SessionLocal() as db:
                max_rpt_date = (
                    db.query(func.max(_mdl.rpt_date))
                    .filter(_mdl.symbol == symbol)
                    .scalar()
                )
                # 报告期快照（东财 Excel 列「最新报告期(含快报)」），判断是否值得调 gm
                latest_rpt_date = (
                    db.query(SymbolInfo.latest_rpt_date)
                    .filter(SymbolInfo.symbol == symbol)
                    .scalar()
                )
            # 库内报告期已追平/超过最新报告期 → 该表数据已是最新，跳过 gm 请求
            if (not force and max_rpt_date is not None and latest_rpt_date is not None
                    and max_rpt_date >= latest_rpt_date):
                logger.info(
                    f"->{symbol} 已是最新报告期 {latest_rpt_date}（库内 {max_rpt_date}），跳过"
                )
                steps[symbol] = -2
                continue
            if max_rpt_date is not None:
                start_date = (max_rpt_date + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = dbsCfg.EARLIEST_START_DATE
            df = _fetch_fund_batched(symbol, start_date, end_date, api_func, fields_str)
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            saved_count = 0
            with SessionLocal() as db:
                for _, row in df.iterrows():
                    rpt_date = _to_date(row.get("rpt_date"))
                    if rpt_date is None:
                        continue
                    pub_date = _to_date(row.get("pub_date"))
                    existing = (
                        db.query(_mdl)
                        .filter(_mdl.symbol == symbol, _mdl.rpt_date == rpt_date)
                        .first()
                    )
                    if existing is not None:
                        # pub_date 保留策略：新数据更旧则跳过
                        if (pub_date is not None and existing.pub_date is not None
                                and pub_date < existing.pub_date):
                            continue
                        existing.pub_date = pub_date
                        existing.rpt_type = _clean_scalar(row.get("rpt_type"))
                        existing.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(existing, f, _clean_scalar(row.get(f)))
                    else:
                        obj = _mdl(symbol=symbol, rpt_date=rpt_date)
                        obj.pub_date = pub_date
                        obj.rpt_type = _clean_scalar(row.get("rpt_type"))
                        obj.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(obj, f, _clean_scalar(row.get(f)))
                        db.add(obj)
                    saved_count += 1
                    if saved_count % 100 == 0:
                        db.commit()
                db.commit()
            logger.info(f"->{symbol} 成功：{saved_count}")
            steps[symbol] = saved_count
        except Exception as e:
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps


def upsert_fund_balance_sql(symbols, force=False):
    """资产负债表数据获取并导入。"""
    return _upsert_fund_sql(
        symbols, model=FundBalance, fields_str=FUND_BALANCE_FIELDS,
        api_func=stk_get_fundamentals_balance, log_name="资产负债表", force=force,
    )


def upsert_fund_income_sql(symbols, force=False):
    """利润表数据获取并导入。"""
    return _upsert_fund_sql(
        symbols, model=FundIncome, fields_str=FUND_INCOME_FIELDS,
        api_func=stk_get_fundamentals_income, log_name="利润表", force=force,
    )


def upsert_fund_cashflow_sql(symbols, force=False):
    """现金流量表数据获取并导入。"""
    return _upsert_fund_sql(
        symbols, model=FundCashflow, fields_str=FUND_CASHFLOW_FIELDS,
        api_func=stk_get_fundamentals_cashflow, log_name="现金流量表", force=force,
    )


def upsert_finance_deriv_sql(symbols, force=False):
    """财务指标数据获取并导入。"""
    return _upsert_fund_sql(
        symbols, model=FinanceDeriv, fields_str=FINANCE_DERIV_FIELDS,
        api_func=stk_get_finance_deriv, log_name="财务指标", force=force,
    )
