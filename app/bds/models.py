# -*- coding: utf-8 -*-
"""bds 应用的 SQLAlchemy 2.0 模型。

由 Django (server_dj/apps/bds/models.py) 迁移而来：
- SymbolInfo: 证券信息
- TradeDate: 交易日历

继承 (Base, BaseModel)：Base 来自 server_fast.common.db，
BaseModel mixin 提供 id/create_time/update_time 通用字段及
to_dtype/map_fields/db_fields/to_dict 方法。
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from server_fast.common.db import Base
from server_fast.common.models import BaseModel


class SymbolInfo(Base, BaseModel):
    """证券信息模型（对应表 bds_symbol_info）。"""

    __tablename__ = "bds_symbol_info"

    # ---- 基础信息 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="代码")
    name: Mapped[str] = mapped_column(String(32), nullable=False, comment="名称")
    industry: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="所属行业")
    online_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="上市日期")

    # ---- 行情/估值指标：除 price 精度为 4 位外，其余均为 Numeric(12, 2) ----
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最新价")
    rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="涨幅%")
    pe_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PE(静)")
    pe_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PE(TTM)")
    pb: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PB")
    dy_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="股息(TTM%)")
    roe: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="ROE(%)")
    yoy_in: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="总收入(%)")
    yoy_np: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="净利润(%)")
    gpm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="毛利率(%)")
    dar: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="负债率(%)")

    # ---- 附加类属性：被 BaseModel 的 map_fields() 等方法使用 ----
    # 字段匹配外部表头：{db_field: [别名...]}
    cols_map_fields = {
        "symbol": ["代码"],
        "name": ["名称"],
        "industry": ["所属行业"],
        "online_date": ["上市日期"],
        "offline_date": ["退市日期"],
        "price": ["最新", "最新价"],
        "rate": ["涨幅%"],
        "pe_lyr": ["市盈率(静)"],
        "pe_ttm": ["市盈率(TTM)"],
        "pb": ["市净率"],
        "dy_ttm": ["股息率TTM%"],
        "roe": ["加权净资产收益率%"],
        "yoy_in": ["营业总收入同比%"],
        "yoy_np": ["归属净利润同比%"],
        "gpm": ["销售毛利率%"],
        "dar": ["资产负债比率%"],
    }
    # 唯一键字段列表
    unique_keys = ["symbol"]
    # 需要 '—' 替换的字段列表
    fields_replace = [
        "online_date", "price", "rate", "pe_lyr", "pe_ttm", "pb", "dy_ttm",
        "roe", "yoy_in", "yoy_np", "gpm", "dar",
    ]

    # 表级参数：保留原 Django Meta.indexes 中的命名索引（与 unique=True 互不冲突）
    __table_args__ = (
        Index("k_bds_symbol_info_symbol", "symbol"),
    )

    def __str__(self) -> str:
        """模型实例字符串表示，便于调试与日志输出。"""
        return f"{self.symbol}-{self.name}"


class TradeDate(Base, BaseModel):
    """交易日历模型（对应表 bds_trade_date）。"""

    __tablename__ = "bds_trade_date"

    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日")

    def __str__(self) -> str:
        """模型实例字符串表示，便于调试与日志输出。"""
        return f"交易日:{self.trade_date}"


class IndexHistory(Base, BaseModel):
    """指数历史行情模型（对应表 bds_index_history）。"""

    __tablename__ = "bds_index_history"

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="代码")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")
    open: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="开盘价")
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最高价")
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最低价")
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="收盘价")

    # 联合唯一约束 + 索引
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uk_bds_index_history_symbol_date"),
        Index("k_bds_index_history_symbol", "symbol"),
    )

    # BaseModel 工具方法所需元数据
    cols_map_fields = {
        "symbol": ["代码"],
        "trade_date": ["交易日期"],
        "open": ["开盘价"],
        "high": ["最高价"],
        "low": ["最低价"],
        "close": ["收盘价"],
    }
    unique_keys = ["symbol", "trade_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.trade_date}"


class IndexConstituent(Base, BaseModel):
    """指数成分股模型（对应表 bds_index_constituent）。"""

    __tablename__ = "bds_index_constituent"

    index_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="指数代码")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="成分股代码")
    weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True, comment="权重")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")

    __table_args__ = (
        UniqueConstraint("index_code", "symbol", "trade_date", name="uk_bds_index_constituent"),
        Index("k_bds_index_constituent_index_code", "index_code"),
        Index("k_bds_index_constituent_trade_date", "trade_date"),
    )

    unique_keys = ["index_code", "symbol", "trade_date"]

    def __str__(self) -> str:
        return f"{self.index_code}-{self.symbol}-{self.trade_date}"


class FundBalance(Base, BaseModel):
    """资产负债表模型（对应表 bds_fund_balance）。"""

    __tablename__ = "bds_fund_balance"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 资产类字段 ----
    mny_cptl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="货币资金")
    acct_rcv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="应收账款")
    invt: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="存货")
    ttl_cur_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="流动资产合计")
    fix_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="固定资产")
    lt_eqy_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="长期股权投资")
    intg_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="无形资产")
    gw: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="商誉")
    ttl_ncur_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="非流动资产合计")
    ttl_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="资产总计")

    # ---- 负债类字段 ----
    sht_ln: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="短期借款")
    acct_pay: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="应付账款")
    ttl_cur_liab: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="流动负债合计")
    lt_ln: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="长期借款")
    ttl_ncur_liab: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="非流动负债合计")
    ttl_liab: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="负债合计")

    # ---- 权益类字段 ----
    cptl_rsv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="资本公积")
    ret_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="未分配利润")
    ttl_eqy_pcom: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="归母股东权益合计")
    ttl_eqy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="股东权益合计")

    # 联合唯一约束：按 symbol + rpt_date 去重
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_fund_balance"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class FundIncome(Base, BaseModel):
    """利润表模型（对应表 bds_fund_income）。"""

    __tablename__ = "bds_fund_income"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 收入类字段 ----
    ttl_inc_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业总收入")
    inc_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业收入")
    # ---- 成本费用类字段 ----
    ttl_cost_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业总成本")
    cost_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业成本")
    exp_sell: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="销售费用")
    exp_adm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="管理费用")
    exp_rd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="研发费用")
    exp_fin: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="财务费用")
    # ---- 其他经营收益 ----
    inc_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="投资收益")
    inc_fv_chg: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="公允价值变动")
    # ---- 利润类字段 ----
    oper_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业利润")
    ttl_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="利润总额")
    inc_tax: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="所得税费用")
    net_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="净利润")
    net_prof_pcom: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="归母净利润")
    # ---- 每股收益 ----
    eps_base: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="基本每股收益")
    eps_dil: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="稀释每股收益")
    # ---- 综合收益及其他 ----
    inc_noper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业外收入")
    exp_noper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业外支出")
    ttl_comp_inc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="综合收益总额")

    # 联合唯一约束：按 symbol + rpt_date 去重
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_fund_income"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class FundCashflow(Base, BaseModel):
    """现金流量表模型（对应表 bds_fund_cashflow）。"""

    __tablename__ = "bds_fund_cashflow"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 经营活动现金流 ----
    cash_rcv_sale: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="销售商品、提供劳务收到的现金")
    cf_in_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="经营活动现金流入小计")
    cash_pur_gds_svc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="购买商品、接受劳务支付的现金")
    cash_pay_emp: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="支付给职工以及为职工支付的现金")
    cash_pay_tax: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="支付的各项税费")
    cf_out_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="经营活动现金流出小计")
    net_cf_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="经营活动产生的现金流量净额")
    # ---- 投资活动现金流 ----
    cash_rcv_sale_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="收回投资收到的现金")
    cf_in_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="投资活动现金流入小计")
    pur_fix_intg_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="购建固定资产、无形资产和其他长期资产支付的现金")
    net_cf_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="投资活动产生的现金流量净额")
    # ---- 筹资活动现金流 ----
    brw_rcv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="取得借款收到的现金")
    cf_in_fin: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="筹资活动现金流入小计")
    cash_rpay_brw: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="偿还债务支付的现金")
    net_cf_fin: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="筹资活动产生的现金流量净额")
    # ---- 汇总 ----
    net_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="净利润")
    efct_er_chg_cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="汇率变动对现金及现金等价物的影响")
    net_incr_cash_eq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="现金及现金等价物净增加额")
    cash_cash_eq_bgn: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="期初现金及现金等价物余额")
    cash_cash_eq_end: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="期末现金及现金等价物余额")

    # 联合唯一约束：按 symbol + rpt_date 去重
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_fund_cashflow"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class FinanceDeriv(Base, BaseModel):
    """财务指标模型（对应表 bds_finance_deriv）。"""

    __tablename__ = "bds_finance_deriv"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 收益率字段 ----
    roe: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净资产收益率ROE(摊薄)")
    roe_weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净资产收益率ROE(加权)")
    roe_avg: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净资产收益率ROE(平均)")
    roa: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="总资产报酬率ROA")
    roic: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="投入资本回报率ROIC")
    # ---- 盈利能力字段 ----
    sale_gpm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="销售毛利率")
    sale_npm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="销售净利率")
    ebitda_toi: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="EBITDA/营业总收入")
    ebit_toi: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="息税前利润/营业总收入")
    # ---- 偿债能力字段 ----
    ast_liab_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="资产负债率")
    curr_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="流动比率")
    quick_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="速动比率")
    liab_eqy_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="产权比率")
    # ---- 营运能力字段 ----
    ttl_ast_turnover_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="总资产周转率")
    acct_rcv_turnover_days: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="应收账款周转天数(含应收票据)")
    inv_turnover_days: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="存货周转天数")
    # ---- 增长能力字段 ----
    net_prof_pcom_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="归属母公司股东的净利润同比增长率")
    ttl_inc_oper_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="营业总收入同比增长率")
    net_prof_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净利润同比增长率")
    ttl_asset_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="总资产同比增长率")

    # 联合唯一约束：按 symbol + rpt_date 去重
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_finance_deriv"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class DailyValuation(Base, BaseModel):
    """估值指标模型（对应表 bds_daily_valuation）。

    每日交易数据（非财报周期数据），元数据仅 symbol + trade_date，
    无 rpt_type/data_type/pub_date。
    """

    __tablename__ = "bds_daily_valuation"

    # ---- 元数据字段（每日交易数据，无 rpt_type/data_type/pub_date） ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")

    # ---- 市盈率 PE 字段 ----
    pe_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(TTM)")
    pe_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新年报LYR)")
    pe_mrq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新报告期MRQ)")
    pe_ttm_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(TTM)扣除非经常性损益")
    pe_lyr_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新年报LYR)扣除非经常性损益")
    pe_mrq_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新报告期MRQ)扣除非经常性损益")
    # ---- 市净率 PB 字段 ----
    pb_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市净率(最新年报LYR)")
    pb_mrq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市净率(最新报告期MRQ)")
    # ---- 市现率 PCF 字段 ----
    pcf_ttm_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(经营现金流,TTM)")
    pcf_ttm_ncf: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(现金净流量,TTM)")
    pcf_lyr_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(经营现金流,最新年报LYR)")
    pcf_lyr_ncf: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(现金净流量,最新年报LYR)")
    # ---- 市销率 PS 字段 ----
    ps_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市销率(TTM)")
    ps_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市销率(最新年报LYR)")
    ps_mrq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市销率(最新报告期MRQ)")
    # ---- PEG 字段 ----
    peg_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="历史PEG值(当年年报增长率)")
    peg_1q: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="历史PEG值(当年1季*4较上年年报增长率)")
    peg_3q: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="历史PEG值(当年3季*4/3较上年年报增长率)")
    # ---- 股息率 DY 字段 ----
    dy_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="股息率(滚动12月TTM)")
    dy_lfy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="股息率(上一财年LFY)")

    # 联合唯一约束：按 symbol + trade_date 去重（每日估值无修正概念）
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uk_bds_daily_valuation"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "trade_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.trade_date}"
