# -*- coding: utf-8 -*-
"""bds 应用的 Pydantic v2 响应 Schema。

用于 API 响应序列化，字段与 ORM 模型一一对应：
- TradeDateOut: 交易日历响应模型（对应 TradeDate）
- SymbolInfoOut: 证券信息响应模型（对应 SymbolInfo）

均配置 from_attributes=True，支持从 ORM 实例或 dict 直接构造。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TradeDateOut(BaseModel):
    """交易日历响应 Schema（对应 bds.TradeDate 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int  # 主键
    trade_date: date  # 交易日
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class SymbolInfoOut(BaseModel):
    """证券信息响应 Schema（对应 bds.SymbolInfo 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int  # 主键
    symbol: str  # 代码
    name: str  # 名称
    industry: Optional[str] = None  # 所属行业
    online_date: Optional[date] = None  # 上市日期
    price: Optional[Decimal] = None  # 最新价
    rate: Optional[Decimal] = None  # 涨幅%
    pe_lyr: Optional[Decimal] = None  # PE(静)
    pe_ttm: Optional[Decimal] = None  # PE(TTM)
    pb: Optional[Decimal] = None  # PB
    dy_ttm: Optional[Decimal] = None  # 股息(TTM%)
    roe: Optional[Decimal] = None  # ROE(%)
    yoy_in: Optional[Decimal] = None  # 总收入(%)
    yoy_np: Optional[Decimal] = None  # 净利润(%)
    gpm: Optional[Decimal] = None  # 毛利率(%)
    dar: Optional[Decimal] = None  # 负债率(%)
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class IndexHistoryOut(BaseModel):
    """指数历史行情响应 Schema（对应 bds.IndexHistory 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int  # 主键
    symbol: str  # 代码
    trade_date: date  # 交易日期
    open: Optional[Decimal] = None  # 开盘价
    high: Optional[Decimal] = None  # 最高价
    low: Optional[Decimal] = None  # 最低价
    close: Optional[Decimal] = None  # 收盘价
    sec_name: Optional[str] = None  # 指数名称（从 Config.INDEX_CODE 查找，不存数据库）
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class IndexConstituentOut(BaseModel):
    """指数成分股响应 Schema（对应 bds.IndexConstituent 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    index_code: str  # 指数代码
    symbol: str  # 成分股代码
    weight: Optional[Decimal] = None  # 权重
    trade_date: date  # 交易日期
    sec_name: Optional[str] = None  # 指数名称（不入库，从 Config.INDEX_CODE 查找）


class FundBalanceOut(BaseModel):
    """资产负债表响应 Schema（对应 bds.FundBalance 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="股票代码")
    pub_date: Optional[date] = Field(default=None, description="发布日期")
    rpt_date: Optional[date] = Field(default=None, description="报告日期")
    rpt_type: Optional[int] = Field(default=None, description="报表类型")
    data_type: Optional[int] = Field(default=None, description="数据类型")
    # ---- 资产类字段 ----
    mny_cptl: Optional[Decimal] = Field(default=None, description="货币资金")
    acct_rcv: Optional[Decimal] = Field(default=None, description="应收账款")
    invt: Optional[Decimal] = Field(default=None, description="存货")
    ttl_cur_ast: Optional[Decimal] = Field(default=None, description="流动资产合计")
    fix_ast: Optional[Decimal] = Field(default=None, description="固定资产")
    lt_eqy_inv: Optional[Decimal] = Field(default=None, description="长期股权投资")
    intg_ast: Optional[Decimal] = Field(default=None, description="无形资产")
    gw: Optional[Decimal] = Field(default=None, description="商誉")
    ttl_ncur_ast: Optional[Decimal] = Field(default=None, description="非流动资产合计")
    ttl_ast: Optional[Decimal] = Field(default=None, description="资产总计")
    # ---- 负债类字段 ----
    sht_ln: Optional[Decimal] = Field(default=None, description="短期借款")
    acct_pay: Optional[Decimal] = Field(default=None, description="应付账款")
    ttl_cur_liab: Optional[Decimal] = Field(default=None, description="流动负债合计")
    lt_ln: Optional[Decimal] = Field(default=None, description="长期借款")
    ttl_ncur_liab: Optional[Decimal] = Field(default=None, description="非流动负债合计")
    ttl_liab: Optional[Decimal] = Field(default=None, description="负债合计")
    # ---- 权益类字段 ----
    cptl_rsv: Optional[Decimal] = Field(default=None, description="资本公积")
    ret_prof: Optional[Decimal] = Field(default=None, description="未分配利润")
    ttl_eqy_pcom: Optional[Decimal] = Field(default=None, description="归母股东权益合计")
    ttl_eqy: Optional[Decimal] = Field(default=None, description="股东权益合计")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class FundIncomeOut(BaseModel):
    """利润表响应 Schema（对应 bds.FundIncome 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="股票代码")
    pub_date: Optional[date] = Field(default=None, description="发布日期")
    rpt_date: Optional[date] = Field(default=None, description="报告日期")
    rpt_type: Optional[int] = Field(default=None, description="报表类型")
    data_type: Optional[int] = Field(default=None, description="数据类型")
    # ---- 收入类字段 ----
    ttl_inc_oper: Optional[Decimal] = Field(default=None, description="营业总收入")
    inc_oper: Optional[Decimal] = Field(default=None, description="营业收入")
    # ---- 成本费用类字段 ----
    ttl_cost_oper: Optional[Decimal] = Field(default=None, description="营业总成本")
    cost_oper: Optional[Decimal] = Field(default=None, description="营业成本")
    exp_sell: Optional[Decimal] = Field(default=None, description="销售费用")
    exp_adm: Optional[Decimal] = Field(default=None, description="管理费用")
    exp_rd: Optional[Decimal] = Field(default=None, description="研发费用")
    exp_fin: Optional[Decimal] = Field(default=None, description="财务费用")
    # ---- 其他经营收益 ----
    inc_inv: Optional[Decimal] = Field(default=None, description="投资收益")
    inc_fv_chg: Optional[Decimal] = Field(default=None, description="公允价值变动")
    # ---- 利润类字段 ----
    oper_prof: Optional[Decimal] = Field(default=None, description="营业利润")
    ttl_prof: Optional[Decimal] = Field(default=None, description="利润总额")
    inc_tax: Optional[Decimal] = Field(default=None, description="所得税费用")
    net_prof: Optional[Decimal] = Field(default=None, description="净利润")
    net_prof_pcom: Optional[Decimal] = Field(default=None, description="归母净利润")
    # ---- 每股收益 ----
    eps_base: Optional[Decimal] = Field(default=None, description="基本每股收益")
    eps_dil: Optional[Decimal] = Field(default=None, description="稀释每股收益")
    # ---- 综合收益及其他 ----
    inc_noper: Optional[Decimal] = Field(default=None, description="营业外收入")
    exp_noper: Optional[Decimal] = Field(default=None, description="营业外支出")
    ttl_comp_inc: Optional[Decimal] = Field(default=None, description="综合收益总额")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class FundCashflowOut(BaseModel):
    """现金流量表响应 Schema（对应 bds.FundCashflow 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="股票代码")
    pub_date: Optional[date] = Field(default=None, description="发布日期")
    rpt_date: Optional[date] = Field(default=None, description="报告日期")
    rpt_type: Optional[int] = Field(default=None, description="报表类型")
    data_type: Optional[int] = Field(default=None, description="数据类型")
    # ---- 经营活动现金流 ----
    cash_rcv_sale: Optional[Decimal] = Field(default=None, description="销售商品、提供劳务收到的现金")
    cf_in_oper: Optional[Decimal] = Field(default=None, description="经营活动现金流入小计")
    cash_pur_gds_svc: Optional[Decimal] = Field(default=None, description="购买商品、接受劳务支付的现金")
    cash_pay_emp: Optional[Decimal] = Field(default=None, description="支付给职工以及为职工支付的现金")
    cash_pay_tax: Optional[Decimal] = Field(default=None, description="支付的各项税费")
    cf_out_oper: Optional[Decimal] = Field(default=None, description="经营活动现金流出小计")
    net_cf_oper: Optional[Decimal] = Field(default=None, description="经营活动产生的现金流量净额")
    # ---- 投资活动现金流 ----
    cash_rcv_sale_inv: Optional[Decimal] = Field(default=None, description="收回投资收到的现金")
    cf_in_inv: Optional[Decimal] = Field(default=None, description="投资活动现金流入小计")
    pur_fix_intg_ast: Optional[Decimal] = Field(default=None, description="购建固定资产、无形资产和其他长期资产支付的现金")
    net_cf_inv: Optional[Decimal] = Field(default=None, description="投资活动产生的现金流量净额")
    # ---- 筹资活动现金流 ----
    brw_rcv: Optional[Decimal] = Field(default=None, description="取得借款收到的现金")
    cf_in_fin: Optional[Decimal] = Field(default=None, description="筹资活动现金流入小计")
    cash_rpay_brw: Optional[Decimal] = Field(default=None, description="偿还债务支付的现金")
    net_cf_fin: Optional[Decimal] = Field(default=None, description="筹资活动产生的现金流量净额")
    # ---- 汇总 ----
    net_prof: Optional[Decimal] = Field(default=None, description="净利润")
    efct_er_chg_cash: Optional[Decimal] = Field(default=None, description="汇率变动对现金及现金等价物的影响")
    net_incr_cash_eq: Optional[Decimal] = Field(default=None, description="现金及现金等价物净增加额")
    cash_cash_eq_bgn: Optional[Decimal] = Field(default=None, description="期初现金及现金等价物余额")
    cash_cash_eq_end: Optional[Decimal] = Field(default=None, description="期末现金及现金等价物余额")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")
