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
    market: Optional[str] = None  # 市场：上交所/深交所/北交所；非 A 股为 NULL
    board: Optional[str] = None  # 板块：主板/创业板/科创板/北交所；非 A 股为 NULL
    online_date: Optional[date] = None  # 上市日期
    pe_lyr: Optional[Decimal] = None  # PE(静)
    pe_ttm: Optional[Decimal] = None  # PE(TTM)
    pb: Optional[Decimal] = None  # PB
    dy_ttm: Optional[Decimal] = None  # 股息(TTM%)
    roe: Optional[Decimal] = None  # ROE(%)
    yoy_in: Optional[Decimal] = None  # 总收入(%)
    yoy_np: Optional[Decimal] = None  # 净利润(%)
    gpm: Optional[Decimal] = None  # 毛利率(%)
    dar: Optional[Decimal] = None  # 负债率(%)
    latest_rpt_date: Optional[date] = None  # 最新报告期(含快报)
    sec_abbr: Optional[str] = None  # 拼音缩写（来源 gm）
    delisted_date: Optional[date] = None  # 退市日期（在市为 NULL；可能为未来日期）
    sync_at: Optional[date] = None  # 同步日期：批量同步完成当日写今日，重跑据此过滤
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
    amount: Optional[Decimal] = None  # 成交额
    volume: Optional[Decimal] = None  # 成交量
    position: Optional[Decimal] = None  # 持仓量
    sec_name: Optional[str] = None  # 指数名称（从 Config.INDEX_CODE 查找，不存数据库）
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class IndexConstituentOut(BaseModel):
    """指数成分股响应 Schema（对应 bds.IndexConstituent 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    index_code: str  # 指数代码
    symbol: str  # 成分股代码
    weight: Optional[Decimal] = None  # 已弃用（2026-10-21）：仅历史快照残留数值，勿用于计算
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


class FinanceDerivOut(BaseModel):
    """财务指标响应 Schema（对应 bds.FinanceDeriv 模型）。

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
    # ---- 收益率字段 ----
    roe: Optional[Decimal] = Field(default=None, description="净资产收益率ROE(摊薄)")
    roe_weight: Optional[Decimal] = Field(default=None, description="净资产收益率ROE(加权)")
    roe_avg: Optional[Decimal] = Field(default=None, description="净资产收益率ROE(平均)")
    roa: Optional[Decimal] = Field(default=None, description="总资产报酬率ROA")
    roic: Optional[Decimal] = Field(default=None, description="投入资本回报率ROIC")
    # ---- 盈利能力字段 ----
    sale_gpm: Optional[Decimal] = Field(default=None, description="销售毛利率")
    sale_npm: Optional[Decimal] = Field(default=None, description="销售净利率")
    ebitda_toi: Optional[Decimal] = Field(default=None, description="EBITDA/营业总收入")
    ebit_toi: Optional[Decimal] = Field(default=None, description="息税前利润/营业总收入")
    # ---- 偿债能力字段 ----
    ast_liab_rate: Optional[Decimal] = Field(default=None, description="资产负债率")
    curr_rate: Optional[Decimal] = Field(default=None, description="流动比率")
    quick_rate: Optional[Decimal] = Field(default=None, description="速动比率")
    liab_eqy_rate: Optional[Decimal] = Field(default=None, description="产权比率")
    # ---- 营运能力字段 ----
    ttl_ast_turnover_rate: Optional[Decimal] = Field(default=None, description="总资产周转率")
    acct_rcv_turnover_days: Optional[Decimal] = Field(default=None, description="应收账款周转天数(含应收票据)")
    inv_turnover_days: Optional[Decimal] = Field(default=None, description="存货周转天数")
    # ---- 增长能力字段 ----
    net_prof_pcom_yoy: Optional[Decimal] = Field(default=None, description="归属母公司股东的净利润同比增长率")
    ttl_inc_oper_yoy: Optional[Decimal] = Field(default=None, description="营业总收入同比增长率")
    net_prof_yoy: Optional[Decimal] = Field(default=None, description="净利润同比增长率")
    ttl_asset_yoy: Optional[Decimal] = Field(default=None, description="总资产同比增长率")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class DailyValuationOut(BaseModel):
    """估值指标响应 Schema（对应 bds.DailyValuation 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="股票代码")
    trade_date: date = Field(description="交易日期")
    # ---- 市盈率 PE 字段 ----
    pe_ttm: Optional[Decimal] = Field(default=None, description="市盈率(TTM)")
    pe_lyr: Optional[Decimal] = Field(default=None, description="市盈率(最新年报LYR)")
    pe_mrq: Optional[Decimal] = Field(default=None, description="市盈率(最新报告期MRQ)")
    pe_ttm_cut: Optional[Decimal] = Field(default=None, description="市盈率(TTM)扣除非经常性损益")
    pe_lyr_cut: Optional[Decimal] = Field(default=None, description="市盈率(最新年报LYR)扣除非经常性损益")
    pe_mrq_cut: Optional[Decimal] = Field(default=None, description="市盈率(最新报告期MRQ)扣除非经常性损益")
    # ---- 市净率 PB 字段 ----
    pb_lyr: Optional[Decimal] = Field(default=None, description="市净率(最新年报LYR)")
    pb_mrq: Optional[Decimal] = Field(default=None, description="市净率(最新报告期MRQ)")
    # ---- 市现率 PCF 字段 ----
    pcf_ttm_oper: Optional[Decimal] = Field(default=None, description="市现率(经营现金流,TTM)")
    pcf_ttm_ncf: Optional[Decimal] = Field(default=None, description="市现率(现金净流量,TTM)")
    pcf_lyr_oper: Optional[Decimal] = Field(default=None, description="市现率(经营现金流,最新年报LYR)")
    pcf_lyr_ncf: Optional[Decimal] = Field(default=None, description="市现率(现金净流量,最新年报LYR)")
    # ---- 市销率 PS 字段 ----
    ps_ttm: Optional[Decimal] = Field(default=None, description="市销率(TTM)")
    ps_lyr: Optional[Decimal] = Field(default=None, description="市销率(最新年报LYR)")
    ps_mrq: Optional[Decimal] = Field(default=None, description="市销率(最新报告期MRQ)")
    # ---- PEG 字段 ----
    peg_lyr: Optional[Decimal] = Field(default=None, description="历史PEG值(当年年报增长率)")
    peg_1q: Optional[Decimal] = Field(default=None, description="历史PEG值(当年1季*4较上年年报增长率)")
    peg_3q: Optional[Decimal] = Field(default=None, description="历史PEG值(当年3季*4/3较上年年报增长率)")
    # ---- 股息率 DY 字段 ----
    dy_ttm: Optional[Decimal] = Field(default=None, description="股息率(滚动12月TTM)")
    dy_lfy: Optional[Decimal] = Field(default=None, description="股息率(上一财年LFY)")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class DailyMktvalueOut(BaseModel):
    """每日市值响应 Schema（对应 bds.DailyMktvalue 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="股票代码")
    trade_date: date = Field(description="交易日期")
    # ---- 市值/价值类字段（单位：元） ----
    tot_mv: Optional[Decimal] = Field(default=None, description="总市值")
    tot_mv_csrc: Optional[Decimal] = Field(default=None, description="总市值(证监会算法)")
    a_mv: Optional[Decimal] = Field(default=None, description="A股市值")
    a_mv_ex_ltd: Optional[Decimal] = Field(default=None, description="A股市值(剔除限售股)")
    b_mv: Optional[Decimal] = Field(default=None, description="B股市值")
    b_mv_ex_ltd: Optional[Decimal] = Field(default=None, description="B股市值(剔除限售股)")
    ev: Optional[Decimal] = Field(default=None, description="企业价值")
    ev_ex_curr: Optional[Decimal] = Field(default=None, description="企业价值(剔除货币资金)")
    equity_value: Optional[Decimal] = Field(default=None, description="股权价值")
    # ---- 比率类字段（单位：倍） ----
    ev_ebitda: Optional[Decimal] = Field(default=None, description="企业价值/EBITDA")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class EconomicIndicatorOut(BaseModel):
    """宏观经济指标响应 Schema（对应 bds.EconomicIndicator 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    indicator_code: str = Field(description="指标代码")
    indicator_name: str = Field(description="指标名称")
    indicator_short_name: Optional[str] = Field(default=None, description="指标简称")
    category: str = Field(description="类别")
    country: str = Field(description="国别")
    report_date: date = Field(description="报告日期")
    pub_date: Optional[date] = Field(default=None, description="发布日期")
    value: Optional[Decimal] = Field(default=None, description="数值")
    value_prev: Optional[Decimal] = Field(default=None, description="前值")
    value_expected: Optional[Decimal] = Field(default=None, description="预期值")
    importance: Optional[int] = Field(default=None, description="重要性")
    revised: Optional[Decimal] = Field(default=None, description="修正值")
    title: Optional[str] = Field(default=None, description="标题")
    foresight: Optional[str] = Field(default=None, description="前瞻")
    unit: Optional[str] = Field(default=None, description="单位")
    frequency: Optional[str] = Field(default=None, description="频率")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class GoldReserveOut(BaseModel):
    """黄金储备响应 Schema（对应 bds.GoldReserve 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    country_code: str = Field(description="国家代码")
    country_name: str = Field(description="国家名称")
    rpt_date: date = Field(description="报告日期")
    gold_holdings_usd: Optional[Decimal] = Field(default=None, description="黄金储备(USD)")
    unit: Optional[str] = Field(default=None, description="单位")
    frequency: Optional[str] = Field(default=None, description="频率")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class DailyIndicatorOut(BaseModel):
    """美债收益率指标响应 Schema（对应 bds.DailyIndicator 模型）。

    每个字段的 description 与 ORM 模型的 comment 保持一致，
    以便 OpenAPI 文档与前端表头统一。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    indicator_code: str = Field(description="指标代码")
    indicator_name: str = Field(description="指标名称")
    indicator_short_name: Optional[str] = Field(default=None, description="指标简称")
    category: str = Field(description="类别")
    country: str = Field(description="国别")
    report_date: date = Field(description="报告日期")
    value: Optional[Decimal] = Field(default=None, description="数值")
    unit: Optional[str] = Field(default=None, description="单位")
    frequency: Optional[str] = Field(default=None, description="频率")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class BatchSyncRequest(BaseModel):
    """批量同步任务请求体（POST /bds/run/batch-sync）。"""

    # 拒绝未声明字段：旧参数 exclude_synced_today 已删除，前端/脚本若仍传它，
    # 默认的 extra='ignore' 会静默丢弃 ⇒ 任务按「不跳过」全量跑（最坏 2.7 万笔空转），
    # 必须报错而不是无感降级。
    model_config = ConfigDict(extra='forbid')

    scope: str = Field(description="同步范围: all/index/industry/explicit")
    index_codes: Optional[list[str]] = Field(
        default=None,
        description="scope=index 时的指数代码清单（多选，取各指数最新快照成分股的**合集**再同步），"
                    "如 ['SHSE.000300', 'SZSE.399006']",
    )
    index_code: Optional[str] = Field(
        default=None,
        description="[兼容保留] scope=index 的单指数写法，等价于 index_codes=[index_code]；新调用请用 index_codes",
    )
    industry: Optional[str] = Field(default=None, description="scope=industry 时填行业名，如 银行")
    symbols: Optional[list[str]] = Field(default=None, description="scope=explicit 时填手写清单")
    tables: Optional[list[str]] = Field(default=None, description="要同步的表列表，默认 6 张全跑")
    mark_synced: bool = Field(default=True, description="全部成功后是否写 sync_at 标记")
    exclude_synced_since: Optional[date] = Field(
        default=None,
        description="剔除 sync_at >= 该日期的标的（YYYY-MM-DD）；None=不跳过。"
                    "选今天=只跑今日未同步，选昨天=接着昨天进度续跑",
    )
    trading_status: str = Field(
        default='trading',
        description="退市口径: trading=仅在市（默认，含已公告未退市）/delisted=仅已退市/all=全部；"
                    "仅作用于 all/industry/index，explicit 手写清单不做退市过滤",
    )
    max_symbols: int = Field(default=0, ge=0, description="最多处理几只 symbol: 0=不限")


class MacroSyncRequest(BaseModel):
    """一键同步请求体（POST /bds/sync/macro）—— 基础数据 + 宏观数据。

    七段**固定按 trade_date → symbol_info → index_history → option_market → wscn → daily → gold
    顺序执行**（基础在下游依赖之前，整体快 → 慢），只跑 stages 里勾选的段。
    执行在后台线程，返回 task 供前端轮询进度（与 /bds/run/batch-sync 同一套任务表）。
    """

    model_config = ConfigDict(extra='forbid')

    stages: list[str] = Field(
        default_factory=lambda: ['symbol_info', 'index_history', 'option_market', 'wscn', 'daily'],
        description="要同步的段（顺序固定 trade_date→symbol_info→index_history→option_market→"
                    "wscn→daily→gold）：trade_date=交易日历 / symbol_info=证券信息 / "
                    "index_history=指数行情 / option_market=期权行情（按「品种 × 到期月」逐组） / "
                    "wscn=经济指标（华尔街见闻）/ daily=美债收益率（FRED）/ gold=黄金储备（IMF）。"
                    "不传 ⇔ 前端默认勾选（不含 trade_date 与 gold：前者一年只需跑一次，后者 IMF 接口 502）",
    )


class IndexConstituentSyncRequest(BaseModel):
    """指数成分股同步/回填请求体（POST /bds/sync/index-constituent）。

    指数清单由前端页面手动勾选后显式传入；后端仍会硬校验（空/未知/yfinance → 400）。
    """

    index_codes: list[str] = Field(
        min_length=1,
        description="本次要处理的指数代码清单（页面勾选），如 ['SHSE.000300', 'SZSE.399006']")
    start_date: Optional[date] = Field(
        default=None,
        description="回填起点 YYYY-MM-DD；不传=各指数从核查水位 checked_to+1 增量"
                    "（水位未初始化退回库内最新 trade_date+1，库空则从 listed_date 起）")


class IndexValuationSyncRequest(BaseModel):
    """指数估值重算请求体（POST /bds/sync/index-valuation，后台任务）。

    区间语义（四种组合，见 `docs/指数估值任务化改造方案_2026-10-21.md` §4.2.1）：

    | start_date | end_date | 实际计算区间                                    |
    |------------|----------|-------------------------------------------------|
    | 都不传     |          | 各指数从**自己的**最新已算日往前重叠 `days` 个交易日 → 最新 |
    | 只传 start |          | `start_date` → 最新                              |
    | 都传       |          | `start_date` → `end_date`（**含两端**）           |
    | 只传 end   |          | 各指数**最早快照** → `end_date`（≈跑几乎全历史，慎用）|

    `days` 只在第一行（纯增量）生效：起点 = 各指数自己的 `MAX(trade_date)` 往前
    `days-1` 个交易日 ⇒ 共重算 `days` 个交易日（前 `days-1` 天是**重叠纠错**，
    覆盖财报修订/成分补录导致的历史指标改写）。指定任一端点时 `days` 被忽略，
    任务的 `scope_params.days` 记为 `null`（不留假值误导事后审计）。
    """

    index_codes: Optional[list[str]] = Field(
        default=None, description="指数代码清单；不传=默认 5 个宽基（不含上证指数）")
    days: int = Field(
        default=5, ge=1, le=60,
        description="纯增量模式下重算的**交易日个数**（用 bds_trade_date 精确倒推，不是自然日）；"
                    "仅当 start_date 与 end_date **都不传**时生效，指定任一端点即失效")
    start_date: Optional[date] = Field(
        default=None, description="重算起点（闭区间）；传了则所有指数共用该起点，days 失效")
    end_date: Optional[date] = Field(
        default=None, description="重算终点（闭区间，含当天）；不传=算到最新")
    with_median: bool = Field(
        default=True, description="是否同时算横截面中位数 pe_median（额外一条窗口函数 SQL，约慢一倍）")


class IndexValuationRow(BaseModel):
    """指数估值原始数据行（对应 bds.IndexValuation 模型，供 /index-valuations/paged 用）。

    与 `/index-valuations`（pivot：`trade_dates[]` + `series{}`，给图表）的区别：
    本结构是**行式**的，一条记录 = 一个指数的一天，可分页、可按指数/日期筛选、可核对中间量。

    ⚠️ `calc_at` 必须暴露：本表是「可重算的派生表」，补成分快照/财报追溯调整会改写历史行，
    不标计算时间就没法判断看到的数是哪版算出来的。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int  # 主键
    index_code: str  # 指数代码
    sec_name: Optional[str] = None  # 指数名称（不入库，从 Config.INDEX_CODE 查找）
    trade_date: date  # 交易日期
    close: Optional[Decimal] = None  # 指数收盘价（来自 bds_index_history）
    pe_ttm: Optional[Decimal] = None  # 整体法 PE_TTM（亏损计入，主口径）
    pe_median: Optional[Decimal] = None  # 成分 PE 横截面中位数（仅 PE>0 样本）
    pb_mrq: Optional[Decimal] = None  # 整体法 PB_MRQ
    dy_ttm: Optional[Decimal] = None  # 市值加权股息率 TTM(%)，分母含全部成分市值
    ps_ttm: Optional[Decimal] = None  # 整体法市销率 TTM = ΣMV/Σ(MV/ps)
    pcf_ttm_oper: Optional[Decimal] = None  # 整体法市现率 TTM（仅 pcf>0.5 成分，负现金流剔除）
    mv_total: Optional[Decimal] = None  # 成分总市值合计（元）
    profit_total: Optional[Decimal] = None  # 成分净利润合计（元，含负值）
    n_total: Optional[int] = None  # 成分股数（有估值数据）
    expected_total: Optional[int] = None  # 该快照应有成分数（覆盖率分母）
    n_valid: Optional[int] = None  # 有效样本数（PE 参与计算）
    n_loss: Optional[int] = None  # 亏损/无 PE 数
    n_cf_neg: Optional[int] = None  # 负/无经营现金流成分数（PCF 剔除样本审计）
    snapshot_date: Optional[date] = None  # 所用成分快照日期（可追溯）
    calc_at: Optional[datetime] = None  # 本行计算时间（重算会刷新）
    create_time: datetime  # 创建时间
    update_time: datetime  # 更新时间


class BdsSyncTaskOut(BaseModel):
    """批量同步任务响应 Schema（对应 bds.BdsSyncTask 模型）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    scope: str = Field(description="同步范围")
    scope_params: Optional[dict] = Field(default=None, description="范围参数")
    tables: list = Field(description="要同步的表列表")
    mark_synced: int = Field(description="是否写 sync_at: 1=是 0=否")
    exclude_synced_since: Optional[date] = Field(default=None, description="剔除 sync_at >= 该日期 的标的；null=不跳过")
    trading_status: str = Field(description="退市口径: trading=仅在市/delisted=仅退市/all=全部")
    max_symbols: int = Field(description="最多处理几只 symbol: 0=不限")
    status: str = Field(description="状态: pending/running/done/failed")
    total_count: int = Field(description="任务总数")
    done_count: int = Field(description="已完成数")
    success_count: int = Field(description="success 数")
    no_data_count: int = Field(description="no_data 数")
    error_count: int = Field(description="error 数")
    skipped_count: int = Field(default=0, description="skipped 数（名单未变/已存跳过，index_backfill 专用）")
    dirty_count: int = Field(default=0, description="被拒存日数（疑 gm 伪快照，index_backfill 专用）")
    aborted_count: int = Field(default=0, description="提前中止的指数个数（index_backfill 专用）")
    progress: Decimal = Field(description="进度% (0-100)")
    errors: Optional[list] = Field(default=None, description="失败明细")
    result: Optional[dict] = Field(
        default=None,
        description="任务结果摘要（结构化，非失败信息）：index_valuation 存审计/覆盖率告警/落后指数")
    start_time: Optional[datetime] = Field(default=None, description="实际开始时间")
    end_time: Optional[datetime] = Field(default=None, description="实际结束时间")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


# ============================================================================
# 期权合约字典（bds_option_info）
# ============================================================================


class OptionInfoOut(BaseModel):
    """期权合约字典响应 Schema（对应 bds.OptionInfo）。

    `symbol` 只在 `(product_id, underlying_contract_id)` 范围内唯一 —— ETF 标准码
    沿用交易所「合约简称」口径、不含标的 6 位，9 个 ETF 标的的行权价区间大量重叠，
    全量下跨标的必然同码（实测 422 组）。所以前端展示合约时必须同时显示标的名，
    且「按代码搜」要允许返回多行。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    contract_id: str = Field(description="openctp 原生合约ID（ETF 为 8 位码 = 券商码去前缀）")
    symbol: str = Field(description="系统标准码（派生）；仅在品种+标的内唯一")
    contract_name: str = Field(description="数据源原始名称")
    exchange: str = Field(description="交易所")
    product_id: str = Field(description="品种ID：IO/au_o/ETF_O/...")
    underlying_contract_id: str = Field(description="标的合约ID：IO2611 / 510500 / au2612")
    cp: str = Field(description="C=认购 P=认沽")
    strike: Decimal = Field(description="行权价（交易所原始值，A类已调整）")
    nominal_strike: Decimal = Field(description="名义行权价（生成标准码用）")
    multiplier: int = Field(description="合约乘数（A类 10180）")
    tick_size: Optional[Decimal] = Field(default=None, description="最小变动价位")
    list_date: Optional[date] = Field(default=None, description="上市日期")
    last_trade_date: date = Field(description="最后交易日（权威到期日）")
    delivery_date: Optional[date] = Field(default=None, description="交割日")
    expire_year_month: str = Field(
        description="期权到期年月 YYYYMM（由 last_trade_date 派生，非数据源交割月）")
    is_listed: int = Field(description="1=最近一次拉取仍挂牌")
    last_seen_date: date = Field(description="最后一次出现在数据源的日期")
    sync_time: datetime = Field(description="本次写入时刻")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class OptionInfoProductOut(BaseModel):
    """字典品种下拉项（只返回 `Config.OPTION_VARIETIES` 已登记的品种）。"""

    option_name: str = Field(description="中文品种名（行情接口入参）")
    und_key: str = Field(description="情景分组键 csi300/csi500etf/au")
    product_id: str = Field(description="openctp 品种ID")
    ctp_underlying: Optional[str] = Field(
        default=None, description="标的 6 位码；ETF 必须，股指/商品为 None")


# ============================================================================
# 期权行情（bds_option_config / bds_option_market）
# ============================================================================


class OptionConfigOut(BaseModel):
    """同步清单响应 Schema（对应 bds.OptionConfig + `bds_option_info` 的派生规格）。

    字段分工（三把钥匙，各对接一个外部系统，详见 `bds.models.OptionConfig` 类文档）：

    | 字段 | 对齐谁 | 用途 |
    |---|---|---|
    | `contract_id` | `bds_option_info.contract_id` | 查合约详情 |
    | `broker_symbol` | 账单 `bills_group.symbol` | 认领实盘持仓（**缺则风险情景报错**） |
    | `option_name` + `end_month` | 行情接口 | 拉行情 |

    ⚠️ **2026-10-27 起，`symbol` 与品种级字段（`underlying_symbol`/`und_key`）在本表
    已无物理列**，全部由接口现算后返回，**只读、PUT 改不了**：

    | 字段 | 来源 |
    |---|---|
    | `contract_id` | **本表实列**，唯一身份（与 `bds_option_info` 1:1） |
    | `symbol` | `bds_option_info`（按 `contract_id` join），仅用于展示/对账 |
    | `und_key` | `Config.OPTION_VARIETIES[option_name]['und_key']`，品种级固定 |
    | `underlying_symbol` | 品种表 `_opt_meta_by_name(option_name)` |

    🔴 **2026-10-27 三次变更：规格字段全部删除**（原 `cp` / `strike` / `multiplier` /
    `margin_type` / `expiry_date`）。原先这些字段只为前端「规格（来自合约字典）」那一列
    服务，而该列有三个问题：①内容只是把合约字典里的值抄过来，要看规格去「合约字典」页即可；
    ②`margin_type` 根本不是字典字段，是品种级 `margin_type_of(option_name)` 派生，
    标题「来自合约字典」名不副实；③列表每刷一次都要多一次字典 join。
    本清单只回答「同步哪些合约、怎么拉」——规格归 `GET /option-info`。

    值为 None 表示**字典里查不到该合约**（字典未刷新或该合约已退市清空），
    不是数据错误。要修它只有一条路：刷新合约字典（批量同步的 `option_info` 段）。

    🔴 **2026-10-27 二次瘦身：响应体再删 `source` / `note`**（表里已无物理列）。
    `source` 恒 `holding`、`note` 恒为「从持仓导入（券商码）」的复述，两个字段
    既没有产生方也没有消费方。清单里真正可编辑的只剩 `broker_symbol` + `is_enabled`。

    🔄 **2026-10-27 五次变更：`expiry_date` 单独加回响应体**（不落库、纯 join 派生）。
    规格五字段删的时候它跟着走了，但「这合约哪天到期、该不该续月」是清单页自己的
    决策信息（到期月 YYYYMM 看不出具体哪天），故按只读派生恢复 —— 10-27 删的是
    「逐行抄进本表」的物理列，不是这个事实本身。权威仍是 `bds_option_info.last_trade_date`。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    contract_id: str = Field(description="openctp 合约ID（唯一身份，与字典表 1:1）")
    symbol: Optional[str] = Field(
        default=None, description="交易所标准期权代码【只读派生：bds_option_info】")
    broker_symbol: Optional[str] = Field(
        default=None,
        description="券商账单代码（与 bills_group.symbol 对齐，认领实盘持仓用）")
    underlying_symbol: Optional[str] = Field(
        default=None, description="标的代码【只读派生】")
    und_key: Optional[str] = Field(
        default=None, description="情景分组键 csi300/csi500etf/au【只读派生】")
    option_name: str = Field(description="akshare 品种名")
    end_month: Optional[str] = Field(default=None, description="到期年月 YYYYMM；商品期权为 ALL")
    is_enabled: int = Field(description="1=参与一键同步")
    expiry_date: Optional[date] = Field(
        default=None,
        description="到期日【只读派生：bds_option_info.last_trade_date，权威值】；"
                    "None = 字典里查不到该合约（先刷新合约字典）")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class OptionConfigCreate(BaseModel):
    """新增同步配置入参。

    🔴 **2026-10-27 起不再接受手工拼码**：入参里的合约必须能在 `bds_option_info` 里
    解析到，解析不到直接 400「字典中无此合约，请先刷新合约字典」。这样「ETF symbol
    多 6 位」（10-26 那次事故）这类拼写错误在结构上不可能再发生。

    两种入参写法（二选一，`contract_id` 优先）：

    - `{"contract_id": "10012562"}` —— 从合约字典页勾选，最稳
    - `{"symbol": "P2611M06250", "option_name": "南方中证500ETF期权"}`
      —— 只是**查询用的入参**，落库时会被兑换成 `contract_id`；因为 ETF 标准码
      跨标的会撞，**必须**带 `option_name` 限定品种

    `end_month` 留空时按字典的 `expire_year_month` 自动推（商品期权恒 `ALL`）。

    ⚠️ 不继承 OptionConfigOut：id / create_time / update_time 是库生成的派生字段，
    混进 Save schema 会导致「无默认值的必填 id」这类恒 422 的缺陷（已有前车之鉴）。
    """

    contract_id: Optional[str] = Field(
        default=None, description="合约字典主键（openctp 的「合约ID」，ETF 即券商码数字部分）")
    symbol: Optional[str] = Field(
        default=None,
        description="交易所标准码，仅作查询入口（落库前兑换成 contract_id）；ETF 须同时给 option_name")
    option_name: str = Field(description="akshare 品种名，须在 Config.OPTION_VARIETIES 中")
    end_month: Optional[str] = Field(
        default=None, description="到期年月 YYYYMM；留空按字典推导；商品期权恒 ALL")
    broker_symbol: Optional[str] = Field(
        default=None,
        description="券商账单代码（与 bills_group.symbol 对齐）；有实盘持仓的合约必须填，"
                    "否则风险情景推演会以「不在同步清单里」报错")
    is_enabled: int = Field(default=1, description="1=参与一键同步")


class OptionConfigUpdate(BaseModel):
    """修改配置。**只允许改人工列**（`broker_symbol` / `is_enabled`）。

    身份字段 `contract_id` 与「拉行情入参」`option_name` / `end_month` 都不可改：
    前者改了就是另一个合约（应 DELETE 后 POST 重建），后两者改了会指向另一个品种/到期月
    （同样应重建）。规格字段（`cp`/`strike`/`multiplier`/`margin_type`/`expiry`）
    物理上已不存在于本表，权威在 `bds_option_info`。
    **一律 None 视为「不改」**，避免 PUT 全字段覆盖把未传的列清空。

    `broker_symbol` 是例外语义：传空串 `""` 表示**清空**（落库 None）。
    ⚠️ 清空它会**让该合约退出风险情景推演**（`contracts` 匹配不到账单码，
    `load_context()` 直接 raise），不是"只是少个备注"。原先它只能新增时写、
    写错就只能删行重建，所以 2026-10-27 才开放 PUT 修改。
    """

    broker_symbol: Optional[str] = Field(
        default=None,
        description="券商账单代码（与 bills_group.symbol 对齐，认领实盘持仓用）；传空串=清空")
    is_enabled: Optional[int] = Field(default=None, description="1=参与一键同步")


class OptionMarketOut(BaseModel):
    """期权行情响应 Schema（对应 bds.OptionMarket + `bds_option_info` 的派生字段）。

    `settle` / `delta_opt` 为 None 表示「该品种接口不提供」，不是数据缺失 ——
    股指与 ETF 期权的 akshare 接口本来就没有这两个字段。

    🔴 **2026-10-27 第四轮：`symbol` 字段彻底删除**（不只是不落库，响应里也不再返回）。
    本表的唯一身份就是 `contract_id`，交易所标准码一律去「合约字典」页看 —— 行情表既
    不存它，也不再 join 出来给前端。前端「代码」列因此改为「合约ID」列、搜索框改为
    按 `contract_id` 模糊匹配。

    ⚠️ 以下字段在库里已无物理列，由接口 `join bds_option_info` 现算：

    | 字段 | 现算来源 |
    |---|---|
    | `contract_id` | 本表实列（唯一身份，可直接 join） |
    | `broker_symbol` | `bds_option_config.broker_symbol`（按 `contract_id` 直取） |
    | `underlying_symbol` / `und_key` | `Config.OPTION_VARIETIES`（按 `option_name`） |
    | `option_type` | `bds_option_info.cp`（C→call / P→put） |
    | `delisted_date` | `bds_option_info.last_trade_date` |
    | `days_left` | `last_trade_date − trade_date` |
    | `multiplier` | `bds_option_info.multiplier`（A 类 10180） |

    `info_missing=True` 表示字典里找不到这个 `contract_id`（极端情况：字典被清或该合约
    从未同步过），此时上述派生字段全部为 None。**正常同步路径不会产生这种行**——
    字典里没有的合约现在会被直接跳过并计入 `skip_reasons["字典无此合约"]`。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    contract_id: str = Field(description="openctp 合约ID（唯一身份）")
    broker_symbol: Optional[str] = Field(default=None, description="券商账单代码（ETF 有）")
    trade_date: date = Field(description="行情日期（交易日）")
    underlying_symbol: Optional[str] = Field(default=None, description="标的代码【派生】")
    und_key: Optional[str] = Field(default=None, description="情景分组键【派生】")
    option_type: Optional[str] = Field(default=None, description="call / put【派生】")
    price_strike: Decimal = Field(description="行权价（当日接口口径）")
    delisted_date: Optional[date] = Field(default=None, description="到期日【派生】")
    days_left: Optional[int] = Field(default=None, description="剩余自然日【派生】")
    multiplier: Optional[int] = Field(default=None, description="合约乘数【派生】")
    price: Optional[Decimal] = Field(default=None, description="最新价（盘中实时/盘后收盘）")
    pre_settle: Optional[Decimal] = Field(default=None, description="前结算价")
    settle: Optional[Decimal] = Field(default=None, description="结算价（仅商品期权可得）")
    price_ud: Optional[Decimal] = Field(default=None, description="标的价")
    value_i: Optional[Decimal] = Field(default=None, description="内在价值（钩子落库时计算）")
    value_t: Optional[Decimal] = Field(default=None, description="时间价值（钩子落库时计算，可为负）")
    atm_i: Optional[Decimal] = Field(default=None, description="平值(%)（钩子落库时计算）")
    ratio_t: Optional[Decimal] = Field(default=None, description="时间(%)（钩子落库时计算）")
    ratio_i: Optional[Decimal] = Field(default=None, description="内在(%)（钩子落库时计算）")
    ratio_t_y: Optional[Decimal] = Field(default=None, description="时间(%Y)（钩子落库时计算）")
    ratio_i_y: Optional[Decimal] = Field(default=None, description="内在(%Y)（钩子落库时计算）")
    delta_opt: Optional[Decimal] = Field(default=None, description="delta（仅商品期权，认沽为负）")
    volume: Optional[int] = Field(default=None, description="成交量（ETF 恒 null）")
    open_interest: Optional[int] = Field(default=None, description="持仓量（ETF 恒 null）")
    quote_time: Optional[datetime] = Field(default=None, description="接口返回的行情时点")
    source: str = Field(description="shfe_direct / akshare_board")
    sync_time: datetime = Field(description="本次写入时间")
    info_missing: bool = Field(default=False, description="字典缺该合约 ⇒ 派生字段为空")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class OptionSyncPlanOut(BaseModel):
    """同步前置确认信息（点「行情同步」时弹窗用）。"""

    symbol_count: int = Field(description="启用配置的合约张数")
    group_count: int = Field(description="品种×到期月组合数（= 接口请求次数）")
    option_names: list[str] = Field(description="涉及的期权品种")
    groups: list[dict] = Field(description="组合明细 [{option_name, end_month}]")
