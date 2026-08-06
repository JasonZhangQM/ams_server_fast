# -*- coding: utf-8 -*-
"""估值监测（ValueMonitor）业务函数：年度行情更新 + 实时行情入库。

- update_value_monitor_hlc_sql：gm history 拉取上年末至今行情，更新 py_close/y_high/y_low
- update_value_monitor_em_orm：gm current 获取最新价，ORM flush 触发钩子计算 pv_*
"""
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
from gm.api import *  # noqa: F401,F403  保留原 gm SDK 通配导入（history/current/ADJUST_PREV 等）
from sqlalchemy import func

from server_fast.common.db import SessionLocal
from server_fast.common.utils import (
    df_init_model,
    upsert_df_to_db,
    call_with_timeout,
)
from server_fast.config import settings
from server_fast.app.bds.models import TradeDate
from server_fast.app.irs.models import ValueMonitor
from server_fast.app.irs.service.common import logger


def get_history_em_df(symbols: list):
    """获取 symbols 上年度末至本年度最近的最高价、最低价和收盘价。"""
    _mdl = TradeDate
    last_year = (datetime.now().year) - 1
    # 查询上一年度最后一个交易日：MySQL YEAR() 函数等价 Django __year 查找
    with SessionLocal() as session:
        last_trade_date_row = (
            session.query(_mdl.trade_date)
            .filter(func.year(_mdl.trade_date) == last_year)
            .order_by(_mdl.trade_date.desc())
            .first()
        )
    last_trade_date = last_trade_date_row[0] if last_trade_date_row else None
    today = date.today()
    history_data = history(
        symbol=symbols,
        frequency='1d',
        start_time=last_trade_date,
        end_time=today,
        fields='eob,symbol,close,high,low',
        adjust=ADJUST_PREV,
        df=True,
    )
    return history_data


def handle_hlc_df(history_data):
    """根据获取的数据分析 symbol 的上年收盘价、本年最高价/最低价和最近收盘价。"""
    symbols = set(history_data['symbol'])
    hlc_list = []
    for symbol in symbols:
        symbol_data = history_data[history_data['symbol'] == symbol]
        py_close = symbol_data['close'].iloc[0]
        last_close = symbol_data['close'].iloc[-1]
        y_high = symbol_data['high'].max()
        y_low = symbol_data['low'].min()
        hlc_list.append({
            'symbol': symbol,
            'py_close': py_close,
            'y_high': y_high,
            'y_low': y_low,
            'last_close': last_close})
        hlc_df = pd.DataFrame(hlc_list)
    return hlc_df


# 更新估值监测(ValueMonitor)的年度行情数据
def update_value_monitor_hlc_sql():
    """拉取 ValueMonitor 所有代码的年度行情，更新 py_close/y_high/y_low 三列。

    复用 get_history_em_df + handle_hlc_df（与模型无关），仅更新 fields_hlc_update 指定的三列，
    保护 pp_el/pp_l/pp_m/pp_h/pp_eh 等用户手动配置字段不被覆盖。
    """
    _engine = settings.DB_ENGINE
    _mdl = ValueMonitor
    with SessionLocal() as session:
        queryset = session.query(_mdl.symbol).all()
    symbols = [row[0] for row in queryset]
    if not symbols:  # 空表跳过，避免无意义调用 gm 接口
        logger.info("->无需更新")
        return
    history_data = get_history_em_df(symbols)
    hlc_df = handle_hlc_df(history_data)
    if not hlc_df.empty:
        df_in = df_init_model(hlc_df, _mdl)
        _table = _mdl.__table__.name
        _unique_keys = _mdl.unique_keys
        _fields_update = _mdl.fields_hlc_update
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys, _fields_update)
        logger.info(f'->更新成功:{result}')
    else:
        logger.info("->无需更新")


def update_value_monitor_em_orm():
    """通过 gm current 获取实时行情，ORM 更新 ValueMonitor.price 并 flush 触发钩子。

    钩子直接读本表 pp_*/py_close 字段计算指标。
    返回 (count_insert, count_update)，其中 insert 恒为 0（记录已存在）。
    """
    _mdl = ValueMonitor
    with SessionLocal() as session:
        rows = session.query(_mdl.id, _mdl.symbol).all()
        vm_dict = {row.symbol: row.id for row in rows}
    if not vm_dict:  # 空表跳过，避免无意义调用 gm 接口
        return 0, 0
    # 获取实时行情（带超时保护，防止 gm 终端未启动时无限阻塞）
    try:
        vm_data = call_with_timeout(current, timeout=10)(
            list(vm_dict.keys()), fields=['symbol', 'price'])
    except Exception as e:
        logger.error(f"******获取实时行情失败：{str(e)}")
        raise e
    vm_data_dict = {item['symbol']: item['price'] for item in vm_data}
    count_update = 0
    with SessionLocal() as session:
        with session.begin():
            for vm_symbol, vm_id in vm_dict.items():
                if vm_symbol not in vm_data_dict:  # 无实时行情则跳过
                    logger.warning(f"无实时行情：{vm_symbol}")
                    continue
                price_d = Decimal(str(vm_data_dict[vm_symbol]))
                try:
                    monitor = session.query(_mdl).filter(_mdl.id == vm_id).one_or_none()
                    if monitor is not None:
                        monitor.price = price_d
                        session.flush()  # 触发 before_update 钩子计算 pv_*
                        count_update += 1
                except Exception as e:
                    logger.error(f"处理 symbol {vm_symbol} 失败：{str(e)}")
                    continue
    return 0, count_update
