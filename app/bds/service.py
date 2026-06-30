# -*- coding: utf-8 -*-
"""bds 应用业务函数（从 server_dj/apps/bds/api.py 迁移）。

迁移要点：
- 移除 Django 专用 import（django.conf、django.db）。
- settings 改从 server_fast.config 导入；工具函数与模型改用 server_fast 路径。
- Django 的 _mdl._meta.db_table 改为 SQLAlchemy 的 _mdl.__table__.name。
- 保留 pandas + settings.DB_ENGINE 逻辑（不改为 session）。
"""
import pandas as pd
import numpy as np
import logging
from gm.api import *
from datetime import datetime
from server_fast.config import settings
from server_fast.common.utils import *
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import *

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 的 logger，输出到 stderr 不被缓冲


# 获取交易日历并存入数据库
def insert_trade_date_em_sql():
    '''
    获取交易日历并存入数据库
    field = 'trade_date'
    mdl = TradeDate
    '''
    _engine = settings.DB_ENGINE
    _field = 'trade_date'
    _mdl = TradeDate
    logger.info("交易日历获取并导入")
    max_date = get_field_max_sql(_field,_mdl,_engine)
    today_year = datetime.today().year
    if max_date is None:
        max_year = 1991
    else:
        max_year = max_date.year
    if max_year >= today_year:
        logger.info("->已经有最新数据，无需调取接口")
        return None
    df = get_trading_dates_by_year( #调取em接口获取交易日历
        'SHSE', max_year, datetime.today().year)
    df = df[df['trade_date']!='']
    if max_date:
        df = df[df['trade_date']>str(max_date)]
    if not df.empty: # 交易日数据不为空
        df = df_init_model(df,_mdl) # 过滤字段
        _table = _mdl.__table__.name  # SQLAlchemy 表名（原 Django: _meta.db_table）
        df.to_sql(_table,_engine,if_exists='append', index=False)
        logger.info(f"->成功 {len(df)}")
    else:
        logger.info("->无需导入")

# 导入证券基本信息(东财)
def upsert_symbol_info_excel_sql():
    _engine = settings.DB_ENGINE
    _mdl = SymbolInfo
    _folder = dbsCfg.FOLDER_SYMBOL
    _map_market_code = dbsCfg.MAP_MARKET_CODE
    _fields_replace = _mdl.fields_replace

    logger.info("证券基本信息导入")
    file_names = [
        f.name for f in _folder.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
            and (not f.name.startswith('~'))
            ]
    for file_name in file_names:
        df = pd.read_excel(_folder/file_name,dtype=str)
        df = df.map( # 去除空格
            lambda x: x.strip() if isinstance(x, str) else x)
        df = df.rename(columns=_mdl.map_fields()) # 外部列名映射数据库字段
        df = df[filter_in_cols(df.columns,_mdl.db_fields())] # 过滤字段
        df['symbol'] = df['symbol'].map( # 代码市场映射
            lambda x: _map_market_code[x[0]])+'.'+df['symbol']
        df[_fields_replace] = df[_fields_replace].replace('—', np.nan)
        df = df.astype(filter_dtypes(df.columns,_mdl.to_dtype())) # 转换数据类型
        if not df.empty:
            df = df.replace({np.nan: None})
            _table = _mdl.__table__.name  # SQLAlchemy 表名（原 Django: _meta.db_table）
            _unique_keys = _mdl.unique_keys
            result = upsert_df_to_db(df, _table, _engine, _unique_keys)
            logger.info(f"->成功：{result}")
        else:
            logger.info(f"->无需导入：{file_name}")
