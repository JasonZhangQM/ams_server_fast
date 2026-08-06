# -*- coding: utf-8 -*-
"""期权监测（OptionMonitor）业务函数：akshare 期权行情 + gm 标的现价同步入库。

- option_monitor_sync_orm：按到期月同步期权行情，upsert 到 OptionMonitor 表
"""
from datetime import date
from decimal import Decimal

from gm.api import *  # noqa: F401,F403  保留原 gm SDK 通配导入（current 等）

from server_fast.common.db import SessionLocal
from server_fast.common.utils import call_with_timeout
from server_fast.app.bds.models import TradeDate
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import OptionMonitor
from server_fast.app.irs.service.common import logger


def _calc_delisted_date(end_month: str, rule: str) -> date:
    """根据到期日规则推算 delisted_date（遇节假日顺延至下一交易日）。

    :param end_month: 到期年月，格式 "YYYYMM"（如 "202608"）
    :param rule: 到期日规则代码（对应 Config.RULE_EXERCISE_DATE，如 'R1'/'R2'）
    :return: 到期日 date
    """
    rule_map = IrsCfg.RULE_EXERCISE_DATE.get(rule)
    if rule_map is None:
        raise ValueError(f"unknown rule_exercise_date: {rule}")
    week_n, weekday = rule_map  # 第几个星期(1-based), 星期几(0=周一)

    year = int(end_month[:4])
    month = int(end_month[4:])

    # 找到该月第 week_n 个星期 weekday 的日期
    # date(year, month, 1).weekday() 得到 1 号的星期几(0=周一)
    first_weekday = date(year, month, 1).weekday()
    # 1 号到目标星期几的偏移天数（可能为负，表示需到下一周）
    offset = (weekday - first_weekday) % 7
    target_day = 1 + offset + (week_n - 1) * 7
    delisted = date(year, month, target_day)

    # 查询 TradeDate 日历，若非交易日则顺延至下一交易日
    with SessionLocal() as session:
        # 判断 delisted 是否为交易日
        is_trade = session.query(TradeDate.trade_date).filter(
            TradeDate.trade_date == delisted
        ).first()
        if not is_trade:
            # 顺延至下一交易日
            next_trade = session.query(TradeDate.trade_date).filter(
                TradeDate.trade_date > delisted
            ).order_by(TradeDate.trade_date.asc()).first()
            if next_trade:
                delisted = next_trade[0]
            # 若日历中无更晚的交易日（极端情况），保留原计算日期
    return delisted


def option_monitor_sync_orm(option_name: str, end_month: str):
    """通过 akshare + gm 获取期权行情并 upsert 到 OptionMonitor 表。

    :param option_name: 期权品种名称（对应 Config.OPTIONS_MARCH 的 option_name）
    :param end_month: 到期年月，格式 "YYYYMM"（如 "202608"）
    :return: 插入+更新的总条数
    """
    # 1. 从配置查找 underlying_symbol、multiplier、rule_exercise_date
    config_item = None
    for item in IrsCfg.OPTIONS_MARCH:
        if item['option_name'] == option_name:
            config_item = item
            break
    if config_item is None:
        raise ValueError(f"unknown option_name: {option_name}")
    underlying_symbol = config_item['underlying_symbol']
    multiplier = int(config_item['multiplier'])
    rule_exercise_date = config_item['rule_exercise_date']

    # 2. 调用 akshare 获取期权行情（函数内导入，避免模块加载时拉起 akshare 依赖）
    import akshare as ak
    df = ak.option_finance_board(symbol=option_name, end_month=end_month)
    if df is None or df.empty:
        logger.info(f"akshare 返回空数据：{option_name} {end_month}")
        return 0

    # 3. 调用 gm current 获取标的现价（gm 终端不可用时降级为 None，钩子已处理 None 情况）
    try:
        ud_data = call_with_timeout(current, timeout=10)(
            [underlying_symbol], fields=['symbol', 'price'])
        price_ud = Decimal(str(ud_data[0]['price'])) if ud_data else None
    except Exception as e:
        logger.error(f"获取标的现价失败：{str(e)}")
        price_ud = None

    # 4. 根据到期日规则推算 delisted_date（遇节假日顺延至下一交易日）
    delisted_date = _calc_delisted_date(end_month, rule_exercise_date)

    # 5. 根据 Config.OPTIONS_MARCH 的 option_type 判断品种格式
    #    股指期权：akshare 返回 'instrument' 列；ETF期权：返回 '合约交易代码' 列
    is_index_option = config_item['option_type'] == '股指期权'
    count_insert = 0
    count_update = 0
    _mdl = OptionMonitor
    with SessionLocal() as session:
        with session.begin():
            for _, row in df.iterrows():
                symbol = None  # 预定义，便于异常日志定位
                try:
                    if is_index_option:
                        # 股指期权（中金所）：instrument 格式 'IO2603-C-3900'
                        symbol = row['instrument']
                        price = Decimal(str(row['lastprice']))
                        parts = row['instrument'].split('-')
                        option_type = 'call' if parts[1] == 'C' else 'put'
                        price_strike = Decimal(parts[2])
                    else:
                        # ETF期权（上交所）：合约交易代码格式 '510500P2608M06250'，第6位 C/P
                        symbol = row['合约交易代码']
                        price = Decimal(str(row['当前价']))
                        price_strike = Decimal(str(row['行权价']))
                        option_type = 'call' if symbol[6] == 'C' else 'put'
                        # 去掉期权代码字母前面的数字（标的代码），如 510500P2608M06250 -> P2608M06250
                        symbol = symbol.lstrip('0123456789')

                    # upsert：先按联合唯一键查询
                    monitor = (
                        session.query(_mdl)
                        .filter(
                            _mdl.underlying_symbol == underlying_symbol,
                            _mdl.price_strike == price_strike,
                            _mdl.delisted_date == delisted_date,
                            _mdl.option_type == option_type,
                        )
                        .one_or_none()
                    )
                    if monitor is not None:
                        # 已存在：更新行情字段（price_ud 为 None 时钩子跳过衍生计算）
                        monitor.price = price
                        monitor.price_ud = price_ud
                        monitor.symbol = symbol
                        count_update += 1
                    else:
                        # 联合键不存在时，按 symbol(unique) 兜底查询，处理 symbol 已存在但联合键变更的情况
                        monitor = (
                            session.query(_mdl)
                            .filter(_mdl.symbol == symbol)
                            .one_or_none()
                        )
                        if monitor is not None:
                            monitor.underlying_symbol = underlying_symbol
                            monitor.price_strike = price_strike
                            monitor.delisted_date = delisted_date
                            monitor.option_type = option_type
                            monitor.multiplier = multiplier
                            monitor.price = price
                            monitor.price_ud = price_ud
                            count_update += 1
                        else:
                            # 插入新记录（含全部配置字段）
                            monitor = _mdl(
                                underlying_symbol=underlying_symbol,
                                price_strike=price_strike,
                                delisted_date=delisted_date,
                                multiplier=multiplier,
                                symbol=symbol,
                                option_type=option_type,
                                price=price,
                                price_ud=price_ud,
                            )
                            session.add(monitor)
                            count_insert += 1
                except Exception as e:
                    logger.error(f"处理期权行失败：{symbol} - {str(e)}")
                    continue
    # session.begin() 退出时自动 commit+flush，触发 before_insert/before_update 钩子
    return count_insert + count_update
