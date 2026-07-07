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
        'SHSE.000300':{'sec_name':'深证300','listed_date':'2005-04-08'},
        'SHSE.000510':{'sec_name':'中证A500','listed_date':'2024-09-23'},
        'SHSE.000905':{'sec_name':'中证500','listed_date':'2007-01-15'},
        'SHSE.000852':{'sec_name':'中证1000','listed_date':'2014-10-17'},
        'SHSE.000688':{'sec_name':'科创50','listed_date':'2020-07-23'},
        'SZSE.399006':{'sec_name':'创业板指','listed_date':'2010-06-01'},
    }

       