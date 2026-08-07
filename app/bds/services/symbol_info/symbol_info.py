# -*- coding: utf-8 -*-
"""证券信息同步：东财 Excel 导入证券基本信息。"""
import logging

import numpy as np
import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import *
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import SymbolInfo

logger = logging.getLogger("uvicorn.error")


def upsert_symbol_info_excel_sql():
    """导入证券基本信息（东财 Excel 全量 upsert）。"""
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
        df = pd.read_excel(_folder / file_name, dtype=str)
        df = df.map(lambda x: x.strip() if isinstance(x, str) else x)
        df.columns = df.columns.str.strip()
        df = df.rename(columns=_mdl.map_fields())
        df = df[filter_in_cols(df.columns, _mdl.db_fields())]
        df['symbol'] = df['symbol'].map(
            lambda x: _map_market_code[x[0]]) + '.' + df['symbol']
        df[_fields_replace] = df[_fields_replace].replace('—', np.nan)
        df = df.astype(filter_dtypes(df.columns, _mdl.to_dtype()))
        if not df.empty:
            df = df.replace({np.nan: None})
            _table = _mdl.__table__.name
            _unique_keys = _mdl.unique_keys
            result = upsert_df_to_db(df, _table, _engine, _unique_keys)
            logger.info(f"->成功：{result}")
        else:
            logger.info(f"->无需导入：{file_name}")
