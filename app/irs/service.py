# -*- coding: utf-8 -*-
"""irs 应用业务函数（从 server_dj/apps/irs/api.py 迁移）。

迁移要点：
- 所有函数名、入参、业务逻辑保持不变
- Django ORM -> SQLAlchemy session 等价转换
- obj.save() -> session.add() + session.flush()（触发 before_insert/before_update 事件钩子）
- transaction.atomic() -> with session.begin(): 上下文管理器
- 跨表字段访问通过 relationship（如 monitor.option.underlying.symbol）
- pandas + settings.DB_ENGINE 逻辑保留不改
- gm SDK 调用保留不改
"""
from datetime import date, datetime
from decimal import Decimal
import logging

import numpy as np
import pandas as pd
from gm.api import *  # noqa: F401,F403  保留原 gm SDK 通配导入（history/current/fut_get_continuous_contracts 等）
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from server_fast.common.db import SessionLocal
from server_fast.common.utils import (
    df_init_model,
    get_sql_to_df,
    upsert_df_to_db,
    call_with_timeout,
)
from server_fast.config import settings
from server_fast.app.bds.models import SymbolInfo, TradeDate
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    MonitorDiscount,
    MonitorOption,
    MonitorOptionT,
    MonitorValue,
    SymbolDiscount,
    SymbolKpi,
    SymbolOption,
    SymbolUnderlying,
    SymbolValue,
)

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 的 logger，输出到 stderr 不被缓冲


# =========================================================================
# 内部辅助函数
# =========================================================================


def _flush_and_commit(session, obj=None):
    """统一封装：add（如传入 obj）-> flush（触发事件钩子）-> commit。

    替代 Django 的 obj.save()。在事务块 (with session.begin()) 内不应调用此函数，
    因为 begin 会在退出时自动 commit。
    """
    if obj is not None:
        session.add(obj)
    session.flush()  # 触发 before_insert / before_update 事件钩子，计算衍生字段
    session.commit()


def _mark_dirty_and_flush(session, obj):
    """强制标记对象为 dirty 并 flush，触发 before_update 钩子。

    适用于 SQLAlchemy 因字段值未变而不标记 dirty 的场景（如 q.delisted_date = q.delisted_date）。
    """
    # 显式标记任一字段为修改状态，确保 before_update 钩子被触发
    flag_modified(obj, "id")
    session.flush()


# =========================================================================
# Excel 文件导入相关
# =========================================================================


# excel文件直接导入数据库
def upsert_model_excel_sql(folder, mdl):
    '''
    folder: excel文件所在文件夹
    mdl: 数据库model
    engine: 数据库engine
    '''
    _engine = settings.DB_ENGINE
    file_names = [
        f.name for f in folder.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
        and (not f.name.startswith('~'))
    ]
    _table = mdl.__table__.name  # SQLAlchemy: 用 __table__.name 替代 _meta.db_table
    _unique_keys = mdl.unique_keys
    result = 0
    for file_name in file_names:
        logger.info(f'导入文件:{file_name}')
        df = pd.read_excel(folder / file_name, dtype=str)
        df = df_init_model(df, mdl)
        if not df.empty:
            result = upsert_df_to_db(df, _table, _engine, _unique_keys)
            logger.info(f'->成功:{result}')
    return result


# =========================================================================
# 估值行情更新（SymbolValue.hlc 字段）
# =========================================================================


# 获取symbols上年度末至本年度最近的最高价、最低价和收盘价
def get_history_em_df(symbols: list) -> pd.DataFrame:
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


# 根据获取的数据分析symbol的上年收盘价，本年最高价、最低价和最近收盘价，并计算涨跌幅度
def handle_hlc_df(history_data: pd.DataFrame):
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


# 更新估值配置中的行情数据
def update_symbol_value_hlc_sql():
    _engine = settings.DB_ENGINE
    _mdl = SymbolValue
    with SessionLocal() as session:
        queryset = session.query(_mdl).all()
        sv_dict = {item.symbol: item.id for item in queryset}
    history_data = get_history_em_df(list(sv_dict.keys()))
    hlc_df = handle_hlc_df(history_data)
    if not hlc_df.empty:  # 更新bills_group表
        df_in = df_init_model(hlc_df, _mdl)
        _table = _mdl.__table__.name
        _unique_keys = _mdl.unique_keys
        _fields_update = _mdl.fields_hlc_update
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys, _fields_update)
        logger.info(f'->更新成功:{result}')
    else:
        logger.info("->无需更新")


# =========================================================================
# 估值指标入库（SymbolKpi / MonitorValue）
# =========================================================================


# 估值指标入库(SymbolKpi)
def symbol_value_em_orm():
    '''
    通过orm的更新price字段,modle中的自定义save方法更新估值字段
    '''
    mdl = SymbolValue
    _mdl_mv = SymbolKpi
    count_insert = 0
    count_update = 0
    with SessionLocal() as session:
        # 一次性加载所有 SymbolValue 的 id 与 symbol，构造 {symbol: id} 字典
        rows = session.query(mdl.id, mdl.symbol).all()
        sv_dict = {row.symbol: row.id for row in rows}
        # 用事务块替代 transaction.atomic()，退出时自动 commit / rollback
        with session.begin():
            for sv_symbol, sv_id in sv_dict.items():
                try:
                    monitor = (
                        session.query(_mdl_mv)
                        .filter(_mdl_mv.symbol_value_id == sv_id)
                        .one_or_none()
                    )
                    if monitor is not None:
                        # 存在则触发 save（仅 flush 触发 before_update 钩子）
                        session.flush()
                        count_update += 1
                    else:
                        # 不存在则新建记录：先查询关联 SymbolValue，再构造 SymbolKpi
                        symbol_value = session.query(mdl).filter(
                            mdl.id == sv_id).one()
                        monitor = _mdl_mv(symbol_value=symbol_value)
                        session.add(monitor)
                        session.flush()  # 触发 before_insert 钩子计算 last_ratio 等
                        count_insert += 1
                except Exception as e:
                    logger.error(f"处理 symbol {sv_symbol} 失败：{str(e)}")
                    continue
    return count_insert, count_update


# 实时估值数据入库(MonitorValue)
def monitor_value_em_orm():
    '''
    通过orm的更新price字段,modle中的自定义save方法更新估值字段
    '''
    mdl = SymbolValue
    _mdl_mv = MonitorValue
    with SessionLocal() as session:
        rows = session.query(mdl.id, mdl.symbol).all()
        sv_dict = {row.symbol: row.id for row in rows}
    # 获取实时行情（带超时保护，防止 gm 终端未启动时无限阻塞）
    try:
        sv_data = call_with_timeout(current, timeout=10)(
            list(sv_dict.keys()), fields=['symbol', 'price', 'high'])
    except Exception as e:
        logger.error(f"******获取实时行情失败：{str(e)}")
        raise e
    sv_data_dict = {
        item['symbol']: {'price': item['price'], 'high': item['high']}
        for item in sv_data
    }
    count_insert = 0
    count_update = 0
    with SessionLocal() as session:
        with session.begin():
            for sv_symbol, sv_id in sv_dict.items():
                if sv_symbol not in sv_data_dict.keys():  # 无实时行情则跳过
                    logger.warning(f"无实时行情：{sv_symbol}")
                    continue
                price_d = Decimal(str(sv_data_dict[sv_symbol]['price']))
                price_h = Decimal(str(sv_data_dict[sv_symbol]['high']))
                try:
                    monitor = (
                        session.query(_mdl_mv)
                        .filter(_mdl_mv.symbol_value_id == sv_id)
                        .one_or_none()
                    )
                    if monitor is not None:
                        monitor.price = price_d
                        if (monitor.rh is None) or (monitor.rh < price_h):
                            monitor.rh = price_h
                        session.flush()  # 触发 before_update 钩子计算 pv_*/bg_d_*/hd_*
                        count_update += 1
                    else:
                        # 不存在则新建记录
                        symbol_value = session.query(mdl).filter(
                            mdl.id == sv_id).one()
                        monitor = _mdl_mv(
                            symbol_value=symbol_value,  # 关联 SymbolValue
                            price=price_d,
                            rh=price_h,
                        )
                        session.add(monitor)
                        session.flush()  # 触发 before_insert 钩子计算
                        count_insert += 1
                except Exception as e:
                    logger.error(f"处理 symbol {sv_symbol} 失败：{str(e)}")
                    continue
    return count_insert, count_update


# =========================================================================
# 期权实时行情相关（MonitorOption）
# =========================================================================


# 从excele文件更新期权实时行情(不能同步期权与标的数据)
def monitor_option_em_excel_orm():
    '''
    1、调取em接口获取期权标的实时行情
    2、从excel获取期权实时行情
    '''
    _folder = IrsCfg.FOLDER_OPTION_PRICE
    with SessionLocal() as session:
        queryset = session.query(MonitorOption).all()
        ud_dict = {  # 创建标的字典,避免实时数据获取失败
            item.option.underlying.symbol: item.price_ud for item in queryset
        }
        si_dict = {
            item.symbol: {
                'id': item.id,
                # 通过关系访问：MonitorOption.option.underlying.symbol
                'ud_symbol': item.option.underlying.symbol,
            }
            for item in queryset
        }
    try:  # 调取em接口获取标的实时行情（带超时保护）
        ud_data = call_with_timeout(current, timeout=10)(
            list(ud_dict.keys()), fields=['symbol', 'price'])
        ud_dict = {item['symbol']: item['price'] for item in ud_data}
    except Exception as e:
        logger.error(f"******获取实时行情失败：{str(e)}")
    file_names = [
        f.name for f in _folder.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
        and (not f.name.startswith('~'))
    ]
    count_update = 0
    count_not_exist = 0
    with SessionLocal() as session:
        for file_name in file_names:
            df = pd.read_excel(_folder / file_name, dtype=str)
            sp_dict = df.set_index('代码')['最新'].to_dict()
            for symbol, item in si_dict.items():
                if symbol not in sp_dict:  # 无实时行情则跳过
                    logger.warning(f"无实时行情：{symbol}")
                    continue
                price = Decimal(str(sp_dict[symbol]))
                price_ud = Decimal(str(ud_dict[item['ud_symbol']]))
                try:
                    monitor = (
                        session.query(MonitorOption)
                        .filter(MonitorOption.id == item['id'])
                        .one_or_none()
                    )
                    if monitor is None:
                        logger.warning(f"{symbol},不存在")
                        continue
                    # 仅价格变化时更新
                    if (monitor.price_ud != price_ud) or (monitor.price != price):
                        monitor.price = price
                        monitor.price_ud = price_ud
                        session.flush()  # 触发 before_update 钩子计算 atm_i/value_t/ratio_* 等
                        count_update += 1
                    else:
                        count_not_exist += 1
                except Exception as e:
                    logger.error(f"处理 symbol {symbol} 失败：{str(e)}")
                    continue
        session.commit()
    return count_update, count_not_exist


# 从excel获取期权和标的实时行情
def monitor_option_excel_orm():
    _folder = IrsCfg.FOLDER_OPTION_PRICE
    _ud_market_dict = IrsCfg.map_ud_market()
    with SessionLocal() as session:
        queryset = session.query(MonitorOption).all()  # 获取所有期权
        si_dict = {  # 期权字典
            item.symbol: {
                'id': item.id,
                # 跨表访问：MonitorOption.option.underlying.symbol
                'ud_symbol': item.option.underlying.symbol,
            }
            for item in queryset
        }
    file_names = [
        f.name for f in _folder.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
        and (not f.name.startswith('~'))
    ]
    result = 0
    with SessionLocal() as session:
        for file_name in file_names:
            df = pd.read_excel(_folder / file_name, dtype=str)
            # 处理期权标的代码(交易所.代码)
            df['代码'] = df['代码'].apply(
                lambda x: _ud_market_dict[x] if x in _ud_market_dict.keys() else x)
            sp_dict = df.set_index('代码')['最新'].to_dict()
            for symbol, item in si_dict.items():
                if symbol not in sp_dict:  # 无实时行情则跳过
                    logger.warning(f"无实时行情：{symbol}")
                    continue
                price = Decimal(str(sp_dict[symbol]))
                price_ud = Decimal(str(sp_dict[item['ud_symbol']]))
                try:
                    monitor = (
                        session.query(MonitorOption)
                        .filter(MonitorOption.id == item['id'])
                        .one_or_none()
                    )
                    if monitor is None:
                        logger.warning(f"{symbol},不存在")
                        continue
                    # 每次都更新
                    monitor.price = price
                    monitor.price_ud = price_ud
                    session.flush()  # 触发 before_update 钩子计算
                    result += 1
                except Exception as e:
                    logger.error(f"处理 symbol {symbol} 失败：{str(e)}")
                    continue
        session.commit()
    return result


# =========================================================================
# 期权配置与 T 型报价相关（SymbolOption / MonitorOptionT）
# =========================================================================


# SymbolOption更新到期日(通过触发orm的save方法)
def symbol_option_update_self_orm():
    with SessionLocal() as session:
        query = session.query(SymbolOption).all()
        result = 0
        for q in query:  # 循环T型报价
            # 显式赋值（同值），并强制标记 dirty 以触发 before_update 钩子重算 days_left/value_per
            q.delisted_date = q.delisted_date
            _mark_dirty_and_flush(session, q)
            result += 1
        session.commit()
    return result


# 期权T型报价数据入库(MonitorOptionT)
def monitor_option_t_orm():
    with SessionLocal() as session:
        query_t = session.query(MonitorOptionT).all()
        result = 0
        for t in query_t:  # 循环T型报价
            # 查询对应的认购期权
            option_c = (
                session.query(MonitorOption)
                .filter(
                    MonitorOption.option_id == t.option_id,
                    MonitorOption.option_type == 'call',
                )
                .one()
            )
            t.price_ud = option_c.price_ud
            t.price_c = option_c.price
            t.value_t_c = option_c.value_t
            t.value_i_c = option_c.value_i
            t.ratio_t_c = option_c.ratio_t
            t.ratio_i_c = option_c.ratio_i
            t.ratio_t_y_c = option_c.ratio_t_y
            t.ratio_i_y_c = option_c.ratio_i_y
            # 查询对应的认沽期权
            option_p = (
                session.query(MonitorOption)
                .filter(
                    MonitorOption.option_id == t.option_id,
                    MonitorOption.option_type == 'put',
                )
                .one()
            )
            t.price_p = option_p.price
            t.value_t_p = option_p.value_t
            t.value_i_p = option_p.value_i
            t.ratio_t_p = option_p.ratio_t
            t.ratio_i_p = option_p.ratio_i
            t.ratio_t_y_p = option_p.ratio_t_y
            t.ratio_i_y_p = option_p.ratio_i_y
            session.flush()  # 触发 before_update 钩子（MonitorOptionT 无计算逻辑，但仍 flush 保持一致）
            result += 1
        session.commit()
    return result


# =========================================================================
# 期货贴水相关（SymbolDiscount / MonitorDiscount）
# =========================================================================


# 真实合约列表
def real_symbols_em(symbol_con_list) -> dict:
    '''
    通过连续合约查询对应真实合约
    symbol_con_list = ['CFFEX.IC00', 'CFFEX.IC01']
    --------------------->>>>>>>>>>>>>>>>>>>>>>>>>>
    {'CFFEX.IC2512':'CFFEX.IC00', 'CFFEX.IC2601':'CFFEX.IC01'}
    '''
    symbols_dict = {}  # 真实合约列表
    for symbol_con in symbol_con_list:
        real_symbol = fut_get_continuous_contracts(
            csymbol=symbol_con)[0]['symbol']
        symbols_dict[real_symbol] = symbol_con
    return symbols_dict


# 查询合约基本信息
def symbol_infos_em(symbols_dict: dict) -> pd.DataFrame:
    '''
    {'CFFEX.IC2512':'CFFEX.IC00', 'CFFEX.IC2601':'CFFEX.IC01'}
    --------------------->>>>>>>>>>>>>>>>>>>>>>>>>>
    symbol     underlying_symbol delisted_date  symbol_con
    CFFEX.IC2512    SHSE.000905     2025-12-19  CFFEX.IC00
    CFFEX.IC2601    SHSE.000905     2025-12-19  CFFEX.IC01
    -----------------------------------------------
    symbol: 合约代码(索引)
    underlying_symbol: 合约标的资产
    delisted_date: 到期日
    symbol_con: 连续合约
    '''
    df = get_symbol_infos(
        sec_type1=1040, symbols=list(symbols_dict.keys()), df=True)
    df = df[['symbol', 'underlying_symbol', 'delisted_date']]
    df['symbol_con'] = df['symbol'].map(symbols_dict)
    return df


# 更新贴水基础数据
def upsert_symbol_discount_em_sql():
    '''
    1、导出SymbolDiscount表数据所有数据
    2、调取em接口获取真实合约列表(real_symbols_em)
    3、调取em接口获取合约基本信息(symbol_infos_em)
    4、调整数据并存入输入库
    '''
    _engine = settings.DB_ENGINE
    _mdl = SymbolDiscount
    logger.info("真实合约及合约基本信息")
    with SessionLocal() as session:
        rows = session.query(_mdl.id, _mdl.symbol_con).all()
        sd_dict = {row.symbol_con: row.id for row in rows}
    symbols_dict = real_symbols_em(list(sd_dict.keys()))  # 获取真实合约列表
    df = symbol_infos_em(symbols_dict)  # 获取合约基本信息
    df['symbol_type'] = df['symbol_con'].apply(
        lambda x: f'{x.split(".")[0]}.{x.split(".")[1][:2]}')
    df['is_main'] = False  # 重置主力标志
    if not df.empty:
        df = df_init_model(df, _mdl)
        _table = _mdl.__table__.name
        _unique_keys = _mdl.unique_keys
        result = upsert_df_to_db(df, _table, _engine, _unique_keys)
        logger.info(f'->成功:{result}')
    else:
        logger.info("->无数据")


# 更新主力合约标志
def update_is_main_em_sql():
    _engine = settings.DB_ENGINE
    _mdl = SymbolDiscount
    _symbol_con_zl = IrsCfg.SYMBOL_CON_ZL
    logger.info("主力合约标识")
    sql = f'''
        SELECT symbol,symbol_con,is_main FROM {_mdl.__table__.name};
        '''
    df = get_sql_to_df(sql, _engine)  # 获取所有合约及主力标志
    symbols_zl = real_symbols_em(_symbol_con_zl)  # 获取主力真实合约
    df['is_main'] = False  # 重置主力标志
    df.loc[df['symbol'].isin(  # 设置主力合约
        set(symbols_zl.keys())), 'is_main'] = True
    df = df_init_model(df, _mdl)
    if not df.empty:
        _table = _mdl.__table__.name
        _unique_keys = _mdl.unique_keys
        _update_clumns = _mdl.update_is_main
        result = upsert_df_to_db(
            df, _table, _engine, _unique_keys, _update_clumns)
        logger.info(f'->成功:{result}')
    else:
        logger.info("->无数据")


# 更新所有合约贴水数据并更新主力标志
def upsert_discount_em_sql():
    upsert_symbol_discount_em_sql()
    update_is_main_em_sql()


# 计算标的升贴水收益率
def discount_yield_em_sql():  # 有问题,不用
    '''
    1、分别获取期货及期货标的symbol合并之后一次调取em接口获取实时行情
    2、计算贴水等相关指标
    3、更新数据库
    '''
    _engine = settings.DB_ENGINE
    _mdl_md = MonitorDiscount
    _mdl_d = SymbolDiscount
    sql = f'''
        SELECT {','.join(_mdl_d.fields_yiels)}
        FROM {_mdl_d.__table__.name};
        '''
    df = get_sql_to_df(sql, _engine)
    df['symbol_real_id'] = df['id']
    symbol_list = list(set(df['symbol_ud'])) + list(set(df['symbol']))
    data = call_with_timeout(current, timeout=10)(  # 获取期货及期货标的实时行情（带超时保护）
        symbol_list, fields=['symbol', 'price', 'cum_position'])
    symbol_dict = {
        item['symbol']: {'price': item['price'], 'position': item['cum_position']}
        for item in data
    }
    df['price_ud'] = df['symbol_ud'].apply(
        lambda x: symbol_dict[x]['price'])
    df['price'] = df['symbol'].apply(
        lambda x: symbol_dict[x]['price'])
    df['position'] = df['symbol'].apply(
        lambda x: symbol_dict[x]['position'])
    # 计算指标
    df['days_left'] = df['delisted_date'].apply(
        lambda x: (x - date.today()).days)
    df['discount'] = df['price_ud'] - df['price']
    df['ratio'] = df['discount'] / df['price'] * 100
    df['ratio_y'] = np.where(
        df['days_left'] != 0,  # 条件：days_left不等于0
        df['ratio'] * 365 / df['days_left'],  # 满足条件时的计算逻辑
        None,  # 不满足条件时赋值为None
    )
    if not df.empty:
        _table = _mdl_md.__table__.name
        _unique_keys = _mdl_md.unique_keys
        _update_columns = _mdl_md.fields_update_sql
        df_in = df_init_model(df, _mdl_md)
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys, _update_columns)
    else:
        result = 0
    return result


def discount_yield_em_orm():
    '''
    1、分别获取期货及期货标的symbol合并之后一次调取em接口获取实时行情
    2、计算贴水等相关指标
    3、更新数据库
    '''
    _mdl_md = MonitorDiscount
    _mdl_d = SymbolDiscount
    with SessionLocal() as session:
        rows = session.query(
            _mdl_d.id, _mdl_d.symbol, _mdl_d.symbol_ud
        ).all()
        sd_dict = {  # 贴水标的字典
            row.symbol: {
                'id': row.id,
                'symbol_ud': row.symbol_ud,
            }
            for row in rows
        }
    # 获取实时行情
    symbol_list = list(  # 期货及期货标的symbol
        sd_dict.keys()) + list(
        {v['symbol_ud'] for k, v in sd_dict.items()})
    try:
        data = call_with_timeout(current, timeout=10)(  # 获取期货及期货标的实时行情（带超时保护）
            symbol_list, fields=['symbol', 'price', 'cum_position'])
    except Exception as e:
        logger.error(f"******获取实时行情失败：{str(e)}")
        raise e
    data_dict = {
        item['symbol']: {
            'price': item['price'],
            'position': item['cum_position'],
        }
        for item in data
    }
    count_insert = 0
    count_update = 0
    with SessionLocal() as session:
        with session.begin():
            for symbol, sd in sd_dict.items():
                if symbol not in data_dict.keys():  # 无实时行情则跳过
                    logger.warning(f"无实时行情：{symbol}")
                    continue
                price_ud = Decimal(str(data_dict[sd['symbol_ud']]['price']))
                price = Decimal(str(data_dict[symbol]['price']))
                position = Decimal(str(data_dict[symbol]['position']))
                try:
                    monitor = (
                        session.query(_mdl_md)
                        .filter(_mdl_md.symbol_real_id == sd['id'])
                        .one_or_none()
                    )
                    if monitor is not None:
                        # 仅价格变化时更新
                        if (monitor.price != price or monitor.price_ud != price_ud):
                            monitor.price = price
                            monitor.price_ud = price_ud
                            monitor.position = position
                            session.flush()  # 触发 before_update 钩子计算 days_left/discount/ratio_*
                            count_update += 1
                    else:
                        # 不存在则新建记录
                        symbol_discount = session.query(_mdl_d).filter(
                            _mdl_d.id == sd['id']).one()
                        monitor = _mdl_md(
                            symbol_real=symbol_discount,  # 关联
                            price=price,
                            price_ud=price_ud,
                            position=position,
                        )
                        session.add(monitor)
                        session.flush()  # 触发 before_insert 钩子计算
                        count_insert += 1
                except Exception as e:
                    logger.error(f"处理 symbol {symbol} 失败：{str(e)}")
                    continue
    return count_insert, count_update
