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

    ECONOMIC_INDICATORS = {  # 美国核心宏观经济指标配置（代码 -> 元信息）
        # 主数据源：FRED API（圣路易斯联储聚合 Fed/BLS/BEA/Census/ISM/CB 等原始源）
        # fred_units: lin=原始值 pc1=一阶差分百分比(月环比) pca=同比百分比变化
        # 保留 akshare_func/col_pattern/col_name 作为 fallback（fred_series_id 缺失时使用）
        'FED_FUNDS_RATE':     {'name': '联邦基金利率',    'category': '利率',    'unit': '%',   'frequency': 'per_fomc',  'fred_series_id': 'FEDFUNDS', 'fred_units': 'lin', 'akshare_func': 'macro_bank_usa_interest_rate',     'col_pattern': 'A'},
        'CPI_YOY':            {'name': 'CPI同比',         'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'CPIAUCSL', 'fred_units': 'pca', 'akshare_func': 'macro_usa_cpi_yoy',                'col_pattern': 'B'},
        'CPI_MOM':            {'name': 'CPI月率',         'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'CPIAUCSL', 'fred_units': 'pc1', 'akshare_func': 'macro_usa_cpi_monthly',            'col_pattern': 'A'},
        'CORE_CPI_MOM':       {'name': '核心CPI月率',     'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'CPILFESL', 'fred_units': 'pc1', 'akshare_func': 'macro_usa_core_cpi_monthly',       'col_pattern': 'A'},
        'CORE_PCE_YOY':       {'name': '核心PCE年率',     'category': '通胀',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'PCEPILFE', 'fred_units': 'pca', 'akshare_func': 'macro_usa_core_pce_price',         'col_pattern': 'A'},
        'NONFARM_PAYROLL':    {'name': '非农就业新增',    'category': '就业',    'unit': '万人','frequency': 'monthly',   'fred_series_id': 'PAYEMS',   'fred_units': 'lin', 'akshare_func': 'macro_usa_non_farm',               'col_pattern': 'A'},
        'UNEMPLOYMENT_RATE':  {'name': '失业率',          'category': '就业',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'UNRATE',   'fred_units': 'lin', 'akshare_func': 'macro_usa_unemployment_rate',      'col_pattern': 'A'},
        'GDP_QOQ':            {'name': 'GDP季环比',       'category': '增长',    'unit': '%',   'frequency': 'quarterly', 'fred_series_id': 'GDP',      'fred_units': 'pc1', 'akshare_func': 'macro_usa_gdp_monthly',            'col_pattern': 'A'},
        'ISM_MFG_PMI':        {'name': 'ISM制造业PMI',    'category': '制造业',  'unit': '',    'frequency': 'monthly',   'akshare_func': 'macro_usa_ism_pmi',                'col_pattern': 'A'},
        'CONSUMER_CONFIDENCE':{'name': '消费者信心指数',  'category': '消费',    'unit': '',    'frequency': 'monthly',   'fred_series_id': 'UMCSENT',  'fred_units': 'lin', 'akshare_func': 'macro_usa_cb_consumer_confidence', 'col_pattern': 'A'},
        'RETAIL_SALES_MOM':   {'name': '零售销售月率',    'category': '消费',    'unit': '%',   'frequency': 'monthly',   'fred_series_id': 'RSAFS',    'fred_units': 'pc1', 'akshare_func': 'macro_usa_retail_sales',           'col_pattern': 'A'},
        'YIELD_2Y':           {'name': '2年期美债收益率', 'category': '收益率',  'unit': '%',   'frequency': 'daily',     'fred_series_id': 'DGS2',     'fred_units': 'lin', 'akshare_func': 'bond_zh_us_rate',                  'col_pattern': 'C', 'col_name': '美国国债收益率2年'},
        'YIELD_10Y':          {'name': '10年期美债收益率','category': '收益率',  'unit': '%',   'frequency': 'daily',     'fred_series_id': 'DGS10',    'fred_units': 'lin', 'akshare_func': 'bond_zh_us_rate',                  'col_pattern': 'C', 'col_name': '美国国债收益率10年'},
        'YIELD_SPREAD_2Y10Y': {'name': '2Y-10Y利差',      'category': '收益率',  'unit': '%',   'frequency': 'daily',     'fred_series_id': 'T10Y2Y',   'fred_units': 'lin', 'akshare_func': 'bond_zh_us_rate',                  'col_pattern': 'C', 'col_name': '美国国债收益率10年-2年'},
    }
