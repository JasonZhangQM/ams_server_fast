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
from sqlalchemy import func, text
from sqlalchemy.orm.attributes import flag_modified

from server_fast.common.db import SessionLocal
from server_fast.common.utils import (
    df_init_model,
    upsert_df_to_db,
    call_with_timeout,
)
from server_fast.config import settings
from server_fast.app.bds.models import SymbolInfo, TradeDate
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    DiscountMonitor,
    MonitorOption,
    MonitorOptionT,
    MonitorValue,
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
    # 文件夹不存在时提前报错，避免 iterdir 抛出难以理解的异常
    if not folder.exists():
        raise FileNotFoundError(f"Excel 文件夹不存在：{folder}")
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
# 期货贴水相关（DiscountMonitor，合并配置+监测单表）
# =========================================================================


# 真实合约列表
def real_symbols_em(symbol_con_list) -> dict:
    '''
    通过连续合约查询对应真实合约
    symbol_con_list = ['CFFEX.IC00', 'CFFEX.IC01']
    --------------------->>>>>>>>>>>>>>>>>>>>>>>>>>
    {'CFFEX.IC2512':'CFFEX.IC00', 'CFFEX.IC2601':'CFFEX.IC01'}

    注：部分远期连续合约（如 CFFEX.IF04/CFFEX.IM04）在 gm SDK 中可能返回空列表，
    此处跳过并记录 warning，避免单个合约缺失导致整个同步中断。
    '''
    symbols_dict = {}  # 真实合约列表
    # gm 终端不可用时 fut_get_continuous_contracts 会阻塞，加超时保护
    _get_contracts = call_with_timeout(fut_get_continuous_contracts, timeout=30)
    for symbol_con in symbol_con_list:
        contracts = _get_contracts(csymbol=symbol_con)
        if not contracts:  # 空列表容错：远期连续合约可能未生成
            logger.warning(f"连续合约 {symbol_con} 未返回真实合约，已跳过")
            continue
        real_symbol = contracts[0]['symbol']
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
    # gm 终端不可用时 get_symbol_infos 会阻塞，加超时保护
    df = call_with_timeout(get_symbol_infos, timeout=30)(
        sec_type1=1040, symbols=list(symbols_dict.keys()), df=True)
    df = df[['symbol', 'underlying_symbol', 'delisted_date']]
    df['symbol_con'] = df['symbol'].map(symbols_dict)
    return df


# 从 Config 同步贴水配置（symbol_type/con_name 从配置取数，清理多余记录）
def upsert_discount_monitor_config_sql():
    '''
    从 Config.SYMBOL_CON_LIST 读取连续合约字典，UPSERT 到 irs_discount_monitor 表。
    写入 symbol_con/symbol_type/con_name 三列；已存在记录更新 symbol_type/con_name
    （Config 变更可同步），其余字段保留。同时删除不在 Config 中的多余记录。
    '''
    _engine = settings.DB_ENGINE
    _mdl = DiscountMonitor
    _table = _mdl.__table__.name
    _unique_keys = _mdl.unique_keys
    # 从 Config 字典构造含 4 列的 DataFrame（is_main 为新记录提供默认值，不参与更新）
    records = [
        {'symbol_con': k, 'symbol_type': v['symbol_type'], 'con_name': v['con_name'], 'is_main': False}
        for k, v in IrsCfg.SYMBOL_CON_LIST.items()
    ]
    df = pd.DataFrame(records)
    # update_columns 指定 symbol_type/con_name：已存在记录更新这两列，is_main 仅用于新记录插入
    result = upsert_df_to_db(
        df, _table, _engine, _unique_keys, update_columns=['symbol_type', 'con_name'])
    logger.info(f'->成功:{result}')
    # 清理数据库中不在 Config 的多余记录（如已删除的 IF04/IM04）
    config_keys = list(IrsCfg.SYMBOL_CON_LIST.keys())
    with _engine.connect() as conn:
        placeholders = ', '.join([f':k{i}' for i in range(len(config_keys))])
        params = {f'k{i}': k for i, k in enumerate(config_keys)}
        delete_result = conn.execute(text(
            f'DELETE FROM {_table} WHERE symbol_con NOT IN ({placeholders})'
        ), params)
        conn.commit()
        if delete_result.rowcount > 0:
            logger.info(f'->清理多余记录:{delete_result.rowcount}条')
    return result


# 更新贴水基础数据
def upsert_discount_monitor_em_sql():
    '''
    1、导出DiscountMonitor表数据所有数据
    2、调取em接口获取真实合约列表(real_symbols_em)
    3、调取em接口获取合约基本信息(symbol_infos_em)
    4、调整数据并存入输入库
    '''
    _engine = settings.DB_ENGINE
    _mdl = DiscountMonitor
    logger.info("真实合约及合约基本信息")
    with SessionLocal() as session:
        rows = session.query(_mdl.id, _mdl.symbol_con).all()
        sd_dict = {row.symbol_con: row.id for row in rows}
    symbols_dict = real_symbols_em(list(sd_dict.keys()))  # 获取真实合约列表
    df = symbol_infos_em(symbols_dict)  # 获取合约基本信息
    # symbol_type/con_name 由 Config 同步时写入（upsert_discount_monitor_config_sql），em 同步不覆盖
    df['is_main'] = False  # 重置主力标志
    if not df.empty:
        df = df_init_model(df, _mdl)
        _table = _mdl.__table__.name
        _unique_keys = _mdl.unique_keys
        result = upsert_df_to_db(df, _table, _engine, _unique_keys)
        logger.info(f'->成功:{result}')
    else:
        logger.info("->无数据")


# 更新主力合约标志（直接 UPDATE DiscountMonitor.is_main，不再通过 DataFrame UPSERT）
def update_is_main_em_sql():
    _mdl = DiscountMonitor
    _symbol_con_zl = IrsCfg.SYMBOL_CON_ZL
    logger.info("主力合约标识")
    # 查询所有合约的 id 与 symbol
    with SessionLocal() as session:
        rows = session.query(_mdl.id, _mdl.symbol).all()
    # 获取主力真实合约集合
    symbols_zl = real_symbols_em(_symbol_con_zl)
    main_symbol_set = set(symbols_zl.keys())
    # 直接批量 UPDATE is_main 字段（True/False）
    count = 0
    with SessionLocal() as session:
        with session.begin():
            for row in rows:
                is_main = row.symbol in main_symbol_set
                session.query(_mdl).filter(_mdl.id == row.id).update(
                    {"is_main": is_main}, synchronize_session=False)
                count += 1
    logger.info(f'->成功:{count}')


# 计算标的升贴水收益率（操作 DiscountMonitor 单表，无 JOIN，合并后无 insert 仅 update）
def discount_yield_em_orm():
    '''
    1、分别获取期货及期货标的symbol合并之后一次调取em接口获取实时行情
    2、更新price/price_ud/position，由事件钩子自动计算贴水等相关指标
    3、合并后无insert，仅update
    '''
    _mdl = DiscountMonitor
    with SessionLocal() as session:
        rows = session.query(
            _mdl.id, _mdl.symbol, _mdl.symbol_ud
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
        data = call_with_timeout(current, timeout=30)(  # 获取期货及期货标的实时行情（带超时保护）
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
                        session.query(_mdl)
                        .filter(_mdl.id == sd['id'])
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
                except Exception as e:
                    logger.error(f"处理 symbol {symbol} 失败：{str(e)}")
                    continue
    return 0, count_update
