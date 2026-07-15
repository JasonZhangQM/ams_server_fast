# -*- coding: utf-8 -*-
from pathlib import Path

# apps配置
class Config:
    def __init__(self):
        pass

    FOLDER_SYMBOL = Path('C:\BaiduSyncdisk\账单\代码信息')

    MAP_MARKET_CODE = { #代码市场映射
        '6':'SHSE',
        '0':'SZSE','3':'SZSE',
        '4':'BJSE','8':'BJSE','9':'BJSE'
    }
    
    INDEX_CODE = { #指数代码映射
        'SHSE.000001':{'sec_name':'上证指数','listed_date':'1991-07-15'},
        'SHSE.000300':{'sec_name':'沪深300','listed_date':'2005-04-08'},
        # 'SHSE.000510':{'sec_name':'中证A500','listed_date':'2024-09-23'},
        'SHSE.000905':{'sec_name':'中证500','listed_date':'2007-01-15'},
        'SHSE.000852':{'sec_name':'中证1000','listed_date':'2014-10-17'},
        # 'SHSE.000688':{'sec_name':'科创50','listed_date':'2020-07-23'},
        'SZSE.399006':{'sec_name':'创业板指','listed_date':'2010-06-01'},
    }

    ECONOMIC_INDICATORS = {  # 各国核心宏观经济指标配置（代码 -> 元信息）
        # 主数据源：FRED API（圣路易斯联储聚合 Fed/BLS/BEA/Census/ISM/CB 等原始源）
        # fred_units: lin=原始值 pch=环比百分比变化 pc1=同比百分比变化 pca=复合年化变化率
        # 保留 akshare_func/col_pattern/col_name 作为 fallback（fred_series_id 缺失时使用）
        'FED_FUNDS_RATE':     {'name': '联邦基金利率',    'country': '美国', 'category': '利率',    'unit': '%',   'frequency': 'per_fomc',  'fred_series_id': 'FEDFUNDS', 'fred_units': 'lin', 'akshare_func': 'macro_bank_usa_interest_rate',     'col_pattern': 'A'},
        'CPI_YOY':            {'name': 'CPI同比',         'country': '美国', 'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'CPIAUCSL', 'fred_units': 'pc1', 'akshare_func': 'macro_usa_cpi_yoy',                'col_pattern': 'B'},
        'CPI_MOM':            {'name': 'CPI月率',         'country': '美国', 'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'CPIAUCSL', 'fred_units': 'pch', 'akshare_func': 'macro_usa_cpi_monthly',            'col_pattern': 'A'},
        'CORE_CPI_MOM':       {'name': '核心CPI月率',     'country': '美国', 'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'CPILFESL', 'fred_units': 'pch', 'akshare_func': 'macro_usa_core_cpi_monthly',       'col_pattern': 'A'},
        'CORE_PCE_YOY':       {'name': '核心PCE年率',     'country': '美国', 'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'PCEPILFE', 'fred_units': 'pc1', 'akshare_func': 'macro_usa_core_pce_price',         'col_pattern': 'A'},
        'NONFARM_PAYROLL':    {'name': '非农就业新增',    'country': '美国', 'category': '就业',    'unit': '万人','frequency': 'monthly',   'fred_series_id': 'PAYEMS',   'fred_units': 'lin', 'akshare_func': 'macro_usa_non_farm',               'col_pattern': 'A'},
        'UNEMPLOYMENT_RATE':  {'name': '失业率',          'country': '美国', 'category': '就业',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'UNRATE',   'fred_units': 'lin', 'akshare_func': 'macro_usa_unemployment_rate',      'col_pattern': 'A'},
        'GDP_QOQ':            {'name': 'GDP季环比',       'country': '美国', 'category': '增长',    'unit': '%',   'frequency': 'quarterly', 'fred_series_id': 'GDP',      'fred_units': 'pch', 'akshare_func': 'macro_usa_gdp_monthly',            'col_pattern': 'A'},
        'ISM_MFG_PMI':        {'name': 'ISM制造业PMI',    'country': '美国', 'category': '制造业',  'unit': '',    'frequency': 'monthly',   'akshare_func': 'macro_usa_ism_pmi',                'col_pattern': 'A'},
        'CONSUMER_CONFIDENCE':{'name': '消费者信心指数',  'country': '美国', 'category': '消费',    'unit': '',    'frequency': 'monthly',   'fred_series_id': 'UMCSENT',  'fred_units': 'lin', 'akshare_func': 'macro_usa_cb_consumer_confidence', 'col_pattern': 'A'},
        'RETAIL_SALES_MOM':   {'name': '零售销售月率',    'country': '美国', 'category': '消费',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'RSAFS',    'fred_units': 'pch', 'akshare_func': 'macro_usa_retail_sales',           'col_pattern': 'A'},
        'YIELD_2Y':           {'name': '2年期美债收益率', 'country': '美国', 'category': '收益率',  'unit': '%',   'frequency': 'daily',     'fred_series_id': 'DGS2',     'fred_units': 'lin', 'akshare_func': 'bond_zh_us_rate',                  'col_pattern': 'C', 'col_name': '美国国债收益率2年'},
        'YIELD_10Y':          {'name': '10年期美债收益率','country': '美国', 'category': '收益率',  'unit': '%',   'frequency': 'daily',     'fred_series_id': 'DGS10',    'fred_units': 'lin', 'akshare_func': 'bond_zh_us_rate',                  'col_pattern': 'C', 'col_name': '美国国债收益率10年'},
        'YIELD_SPREAD_2Y10Y': {'name': '2Y-10Y利差',      'country': '美国', 'category': '收益率',  'unit': '%',   'frequency': 'daily',     'fred_series_id': 'T10Y2Y',   'fred_units': 'lin', 'akshare_func': 'bond_zh_us_rate',                  'col_pattern': 'C', 'col_name': '美国国债收益率10年-2年'},
        # 中国宏观指标：FRED 不提供中国数据，仅通过 wscn 日历数据源同步
        'CN_REAL_ESTATE_INVEST': {'name': '房地产开发投资同比', 'country': '中国', 'category': '投资', 'unit': '%', 'frequency': 'monthly'},
        'CN_M1_YOY':           {'name': 'M1货币供应同比',   'country': '中国', 'category': '货币',   'unit': '%',   'frequency': 'monthly'},
        'CN_M2_YOY':           {'name': 'M2货币供应同比',   'country': '中国', 'category': '货币',   'unit': '%',   'frequency': 'monthly'},
        # 加拿大宏观指标：FRED 不提供加拿大数据，仅通过 wscn 日历数据源同步
        'CA_POLICY_RATE':      {'name': '加拿大央行政策利率', 'country': '加拿大', 'category': '利率', 'unit': '%', 'frequency': 'per_boc'},
    }

    # 华尔街见闻日历数据源：wscn_ticker -> indicator_code 映射
    # 数据源接口：GET https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas?start={ts}&end={ts}
    # 覆盖 11 个美国指标（3 个收益率指标 wscn 无对应，仍由 FRED 提供）+ 3 个中国指标 + 1 个加拿大指标
    # importance/revised/forecast/public_date 由 wscn 接口返回，补充 FRED 缺失字段
    WSCN_INDICATOR_MAP = {
        'US191228': 'FED_FUNDS_RATE',      # FOMC利率决策
        'US111017': 'CPI_YOY',             # CPI同比
        'US111044': 'CPI_MOM',             # CPI环比
        'US111045': 'CORE_CPI_MOM',        # 核心CPI环比
        'US111034': 'CORE_PCE_YOY',        # 核心PCE物价指数同比
        'US121058': 'NONFARM_PAYROLL',     # 非农就业人口变动
        'US121050': 'UNEMPLOYMENT_RATE',   # 失业率
        'US101000': 'GDP_QOQ',             # 实际GDP年化季环比
        'US151132': 'ISM_MFG_PMI',         # ISM制造业指数
        'US151146': 'CONSUMER_CONFIDENCE', # 谘商会消费者信心指数
        'US171206': 'RETAIL_SALES_MOM',    # 零售销售环比
        'CN161376': 'CN_REAL_ESTATE_INVEST', # 全国房地产开发投资
        'CN191441': 'CN_M1_YOY',           # M1货币供应同比
        'CN191442': 'CN_M2_YOY',           # M2货币供应同比
        'CA193210': 'CA_POLICY_RATE',      # 加拿大央行政策利率
    }
