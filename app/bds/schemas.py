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
# 期权行情（bds_option_config / bds_option_market）
# ============================================================================


class OptionConfigOut(BaseModel):
    """期权合约档案 + 同步清单响应 Schema（对应 bds.OptionConfig）。

    规格字段（cp/strike/multiplier/margin_type/expiry_date）为 None 表示「尚未补全」，
    不是数据错误 —— 观察型合约允许只填代码与品种。同步时会自动回填。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="交易所标准期权代码")
    broker_symbol: Optional[str] = Field(default=None, description="券商账单代码（对账用）")
    underlying_symbol: str = Field(description="标的代码")
    und_key: str = Field(description="情景分组键: csi300/csi500etf/au")
    cp: Optional[str] = Field(default=None, description="C=认购 P=认沽")
    strike: Optional[Decimal] = Field(default=None, description="行权价（统一本位）")
    multiplier: Optional[int] = Field(default=None, description="合约乘数")
    margin_type: Optional[str] = Field(default=None, description="保证金公式族 cffex/etf/shex")
    expiry_date: Optional[date] = Field(default=None, description="到期日")
    option_name: str = Field(description="akshare 品种名")
    end_month: Optional[str] = Field(default=None, description="到期年月 YYYYMM；商品期权为 ALL")
    is_enabled: int = Field(description="1=参与一键同步")
    source: str = Field(description="manual=手工 / holding=从持仓导入")
    note: Optional[str] = Field(default=None, description="备注")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class OptionConfigCreate(BaseModel):
    """新增同步配置入参。

    ⚠️ 不继承 OptionConfigOut：id / create_time / update_time 是库生成的派生字段，
    混进 Save schema 会导致「无默认值的必填 id」这类恒 422 的缺陷（已有前车之鉴）。
    """

    symbol: str = Field(description="交易所标准期权代码（会做归一化：去账单前缀、去 ETF 标的数字前缀）")
    broker_symbol: Optional[str] = Field(default=None, description="券商账单代码（可选）")
    option_name: str = Field(description="akshare 品种名，须在 IrsCfg.OPTIONS_MARCH 中")
    end_month: Optional[str] = Field(default=None, description="到期年月 YYYYMM；商品期权填 ALL")
    underlying_symbol: Optional[str] = Field(default=None, description="标的代码；留空则按品种自动取")
    und_key: Optional[str] = Field(default=None, description="情景分组键；留空则按标的自动取")
    cp: Optional[str] = Field(default=None, description="C=认购 P=认沽；留空则等同步回填")
    strike: Optional[Decimal] = Field(default=None, description="行权价（统一本位）；留空则等同步回填")
    multiplier: Optional[int] = Field(default=None, description="合约乘数；留空则等同步回填")
    margin_type: Optional[str] = Field(default=None, description="保证金公式族；留空则等同步回填")
    expiry_date: Optional[date] = Field(default=None, description="到期日；留空则等同步回填")
    is_enabled: int = Field(default=1, description="1=参与一键同步")
    note: Optional[str] = Field(default=None, description="备注")


class OptionConfigUpdate(BaseModel):
    """修改配置（身份字段 symbol / option_name 不可改）。

    `is_enabled`/`note` 是人工列（铁律 4.9）：同步任务只写 bds_option_market，永不覆盖本表。
    规格字段（cp/strike/multiplier/margin_type/expiry_date）允许人工修正——
    历史上到期日确实手填错过（au2612 填 11-23，权威值是 11-24）。
    **一律 None 视为「不改」**，避免 PUT 全字段覆盖把未传的列清空。
    """

    is_enabled: Optional[int] = Field(default=None, description="1=参与一键同步")
    note: Optional[str] = Field(default=None, description="备注")
    cp: Optional[str] = Field(default=None, description="C=认购 P=认沽；None=不改")
    strike: Optional[Decimal] = Field(default=None, description="行权价；None=不改")
    multiplier: Optional[int] = Field(default=None, description="合约乘数；None=不改")
    margin_type: Optional[str] = Field(default=None, description="保证金公式族；None=不改")
    expiry_date: Optional[date] = Field(default=None, description="到期日；None=不改")


class OptionMarketOut(BaseModel):
    """期权行情响应 Schema（对应 bds.OptionMarket）。

    `settle` / `delta_opt` 为 None 表示「该品种接口不提供」，不是数据缺失 ——
    股指与 ETF 期权的 akshare 接口本来就没有这两个字段。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键")
    symbol: str = Field(description="交易所标准期权代码")
    broker_symbol: Optional[str] = Field(default=None, description="券商账单代码（ETF 有）")
    trade_date: date = Field(description="行情日期（交易日）")
    underlying_symbol: str = Field(description="标的代码")
    und_key: str = Field(description="情景分组键")
    option_type: str = Field(description="call / put")
    price_strike: Decimal = Field(description="行权价")
    delisted_date: Optional[date] = Field(default=None, description="到期日")
    days_left: Optional[int] = Field(default=None, description="剩余自然日")
    multiplier: int = Field(description="合约乘数")
    price: Optional[Decimal] = Field(default=None, description="最新价（盘中实时/盘后收盘）")
    pre_settle: Optional[Decimal] = Field(default=None, description="前结算价")
    settle: Optional[Decimal] = Field(default=None, description="结算价（仅商品期权可得）")
    price_ud: Optional[Decimal] = Field(default=None, description="标的价")
    value_i: Optional[Decimal] = Field(default=None, description="内在价值")
    value_t: Optional[Decimal] = Field(default=None, description="时间价值")
    delta_opt: Optional[Decimal] = Field(default=None, description="delta（仅商品期权，认沽为负）")
    volume: Optional[int] = Field(default=None, description="成交量（ETF 恒 null）")
    open_interest: Optional[int] = Field(default=None, description="持仓量（ETF 恒 null）")
    quote_time: Optional[datetime] = Field(default=None, description="接口返回的行情时点")
    source: str = Field(description="shfe_direct / akshare_board")
    sync_time: datetime = Field(description="本次写入时间")
    create_time: datetime = Field(description="创建时间")
    update_time: datetime = Field(description="更新时间")


class OptionSyncPlanOut(BaseModel):
    """同步前置确认信息（点「行情同步」时弹窗用）。"""

    symbol_count: int = Field(description="启用配置的合约张数")
    group_count: int = Field(description="品种×到期月组合数（= 接口请求次数）")
    option_names: list[str] = Field(description="涉及的期权品种")
    groups: list[dict] = Field(description="组合明细 [{option_name, end_month}]")


class OptionConfigBackfillResult(BaseModel):
    """规格回填结果。

    从 `bds_option_market` 最新快照补齐 config 中为空的合约规格（cp/strike/multiplier/
    expiry_date），`margin_type` 由品种名派生。**只填空值、不覆盖已有值**——
    已有值可能是人工修正过的（如 au2612 到期日从错的 11-23 改成 11-24）。
    """

    filled: int = Field(description="补全条数")
    skipped: int = Field(description="规格已齐全、跳过的条数")
    no_data: list[dict] = Field(
        default_factory=list, description="无行情快照、无法补全的合约 [{symbol, reason}]")
