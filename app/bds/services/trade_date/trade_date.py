# -*- coding: utf-8 -*-
"""交易日历同步：东财 em 接口获取交易日历并入库。"""
import logging
from datetime import datetime

import pandas as pd
from gm.api import *

from server_fast.config import settings
from server_fast.common.utils import *
from server_fast.app.bds.models import TradeDate

logger = logging.getLogger("uvicorn.error")


def insert_trade_date_em_sql():
    """获取交易日历并存入数据库。

    返回值：新增的交易日数量（int），0 表示无需更新
    """
    _engine = settings.DB_ENGINE
    _field = 'trade_date'
    _mdl = TradeDate
    logger.info("交易日历获取并导入")
    max_date = get_field_max_sql(_field, _mdl, _engine)
    today_year = datetime.today().year
    if max_date is None:
        max_year = 1991
    else:
        max_year = max_date.year
    if max_year >= today_year:
        logger.info("->已经有最新数据，无需调取接口")
        return 0
    df = get_trading_dates_by_year('SHSE', max_year, datetime.today().year)
    df = df[df['trade_date'] != '']
    if max_date:
        df = df[df['trade_date'] > str(max_date)]
    if not df.empty:
        df = df_init_model(df, _mdl)
        _table = _mdl.__table__.name
        df.to_sql(_table, _engine, if_exists='append', index=False)
        logger.info(f"->成功 {len(df)}")
        return len(df)
    else:
        logger.info("->无需导入")
        return 0
