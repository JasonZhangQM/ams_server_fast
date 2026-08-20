# -*- coding: utf-8 -*-
"""财务主要指标同步：gm 接口 stk_get_finance_prime。

复用 fund_reports._upsert_fund_sql + pub_date 保留策略。
"""
from gm.api import stk_get_finance_prime

from server_fast.app.bds.models import FinancePrime
from server_fast.app.bds.services.fund_data.fund_reports import _upsert_fund_sql

# 财务主要指标字段（gm API fields 参数，20个）
FINANCE_PRIME_FIELDS = "ttl_ast,ttl_liab,ttl_inc_oper,inc_oper,oper_prof,ttl_prof,ttl_eqy_pcom,net_prof_pcom,net_prof_pcom_cut,roe,roe_weight_avg,roe_cut,roe_weight_avg_cut,net_cf_oper,inc_oper_yoy,ttl_inc_oper_yoy,net_prof_pcom_yoy,net_asset,net_prof,net_prof_cut"


def upsert_finance_prime_sql(symbols):
    """财务主要指标数据获取并导入。"""
    return _upsert_fund_sql(
        symbols, model=FinancePrime, fields_str=FINANCE_PRIME_FIELDS,
        api_func=stk_get_finance_prime, log_name="财务主要指标",
    )
