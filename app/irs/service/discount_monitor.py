# -*- coding: utf-8 -*-
"""贴水监测（DiscountMonitor）业务函数：配置同步 + 真实合约更新 + 主力标志 + 实时贴水。

- upsert_discount_monitor_config_sql：从 Config 同步 symbol_type/con_name，清理多余记录
- upsert_discount_monitor_em_sql：gm 获取真实合约及基本信息
- update_is_main_em_sql：更新主力合约标志
- discount_yield_em_orm：gm current 获取实时行情，触发钩子计算贴水指标
"""
from decimal import Decimal

import pandas as pd
from gm.api import *  # noqa: F401,F403  保留原 gm SDK 通配导入（fut_get_continuous_contracts/get_symbol_infos/current 等）
from sqlalchemy import text

from server_fast.common.db import SessionLocal
from server_fast.common.utils import (
    df_init_model,
    upsert_df_to_db,
    call_with_timeout,
)
from server_fast.config import settings
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import DiscountMonitor
from server_fast.app.irs.service.common import logger


def real_symbols_em(symbol_con_list) -> dict:
    """通过连续合约查询对应真实合约。

    :param symbol_con_list: ['CFFEX.IC00', 'CFFEX.IC01']
    :return: {'CFFEX.IC2512':'CFFEX.IC00', 'CFFEX.IC2601':'CFFEX.IC01'}

    注：部分远期连续合约（如 CFFEX.IF04/CFFEX.IM04）在 gm SDK 中可能返回空列表，
    此处跳过并记录 warning，避免单个合约缺失导致整个同步中断。
    """
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


def symbol_infos_em(symbols_dict: dict) -> pd.DataFrame:
    """查询合约基本信息。

    :param symbols_dict: {'CFFEX.IC2512':'CFFEX.IC00', ...}
    :return: DataFrame[symbol, underlying_symbol, delisted_date, symbol_con]
    """
    # gm 终端不可用时 get_symbol_infos 会阻塞，加超时保护
    df = call_with_timeout(get_symbol_infos, timeout=30)(
        sec_type1=1040, symbols=list(symbols_dict.keys()), df=True)
    df = df[['symbol', 'underlying_symbol', 'delisted_date']]
    df['symbol_con'] = df['symbol'].map(symbols_dict)
    return df


def upsert_discount_monitor_config_sql():
    """从 Config 同步贴水配置（symbol_type/con_name 从配置取数，清理多余记录）。

    从 Config.SYMBOL_CON_LIST 读取连续合约字典，UPSERT 到 irs_discount_monitor 表。
    写入 symbol_con/symbol_type/con_name 三列；已存在记录更新 symbol_type/con_name
    （Config 变更可同步），其余字段保留。同时删除不在 Config 中的多余记录。
    """
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


def upsert_discount_monitor_em_sql():
    """更新贴水基础数据：真实合约列表 + 合约基本信息入库。

    1、导出 DiscountMonitor 表数据
    2、调取 em 接口获取真实合约列表(real_symbols_em)
    3、调取 em 接口获取合约基本信息(symbol_infos_em)
    4、调整数据并存入数据库
    """
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


def update_is_main_em_sql():
    """更新主力合约标志（直接 UPDATE DiscountMonitor.is_main，不再通过 DataFrame UPSERT）。"""
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


def discount_yield_em_orm():
    """计算标的升贴水收益率（操作 DiscountMonitor 单表，无 JOIN，合并后无 insert 仅 update）。

    1、分别获取期货及期货标的 symbol 合并之后一次调取 em 接口获取实时行情
    2、更新 price/price_ud/position，由事件钩子自动计算贴水等相关指标
    3、合并后无 insert，仅 update
    """
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
    symbol_list = list(  # 期货及期货标的 symbol
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
