# -*- coding: utf-8 -*-
"""期权监测（OptionMonitor）业务函数：akshare 期权行情 + gm 标的现价同步入库。

- option_monitor_sync_orm：按到期月同步期权行情，upsert 到 OptionMonitor 表
"""
import calendar
import re
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
    :param rule: 到期日规则代码（对应 Config.RULE_EXERCISE_DATE，如 'R1'/'R2'/'R3'）
    :return: 到期日 date
    """
    # 未知规则校验：R3 配置值为 None（占位），需排除在 None 校验之外
    if rule != 'R3' and IrsCfg.RULE_EXERCISE_DATE.get(rule) is None:
        raise ValueError(f"unknown rule_exercise_date: {rule}")

    year = int(end_month[:4])
    month = int(end_month[4:])

    # R3：商品期权，标的期货合约交割月（end_month）前第一月的倒数第五个交易日
    if rule == 'R3':
        # 计算"前第一月"：若 month==1 则跨年至上年 12 月
        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1
        prev_first = date(prev_year, prev_month, 1)
        prev_last = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
        # 查询该月所有交易日，按降序取倒数第 5 个
        with SessionLocal() as session:
            trade_dates = session.query(TradeDate.trade_date).filter(
                TradeDate.trade_date >= prev_first,
                TradeDate.trade_date <= prev_last,
            ).order_by(TradeDate.trade_date.desc()).limit(5).all()
        if len(trade_dates) < 5:
            raise ValueError(
                f"TradeDate 日历数据不足：{prev_year}-{prev_month:02d} 交易日不足 5 条"
            )
        return trade_dates[4][0]

    # R1/R2：按 (week_n, weekday) 规则计算
    week_n, weekday = IrsCfg.RULE_EXERCISE_DATE[rule]  # 第几个星期(1-based), 星期几(0=周一)

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
    option_type_cfg = config_item['option_type']
    # 商品期权（如黄金期权）走 option_hist_shfe 分支；股指/ETF 期权走 option_finance_board
    is_commodity_option = (option_type_cfg == '商品期权')

    # 2. 调用 akshare 获取期权行情（函数内导入，避免模块加载时拉起 akshare 依赖）
    import akshare as ak
    if is_commodity_option:
        # 商品期权：option_hist_shfe 需指定 trade_date，取今日或日历中 ≤ today 的最大交易日
        today = date.today()
        with SessionLocal() as session:
            is_trade = session.query(TradeDate.trade_date).filter(
                TradeDate.trade_date == today
            ).first()
            if is_trade:
                trade_date = today
            else:
                last_trade = session.query(TradeDate.trade_date).filter(
                    TradeDate.trade_date <= today
                ).order_by(TradeDate.trade_date.desc()).first()
                if last_trade is None:
                    logger.info(f"TradeDate 日历无 {today} 及之前的交易日")
                    return 0
                trade_date = last_trade[0]
        df = ak.option_hist_shfe(
            symbol=option_name, trade_date=trade_date.strftime('%Y%m%d'))
        if df is None or df.empty:
            logger.info(f"akshare 返回空数据：{option_name} {end_month} trade_date={trade_date}")
            return 0
    else:
        # 股指/ETF 期权：option_finance_board 按 end_month 拉取
        df = ak.option_finance_board(symbol=option_name, end_month=end_month)
        if df is None or df.empty:
            logger.info(f"akshare 返回空数据：{option_name} {end_month}")
            return 0

    # 3. 股指/ETF 期权：标的固定、end_month 唯一，循环前算一次 delisted_date 与 price_ud
    #    商品期权：标的随到期月变化（SHFE.au2609、SHFE.au2610...），按 code_month 缓存
    is_index_option = (option_type_cfg == '股指期权')
    # 黄金期权合约代码正则：au2609C648 = 标的au+月份2609+C认购+行权价648
    commodity_re = re.compile(r'^au(\d{4})([CP])(\d+(?:\.\d+)?)$')

    if not is_commodity_option:
        # 股指/ETF 期权：循环前计算 delisted_date 与 price_ud
        try:
            ud_data = call_with_timeout(current, timeout=10)(
                [underlying_symbol], fields=['symbol', 'price'])
            price_ud = Decimal(str(ud_data[0]['price'])) if ud_data else None
        except Exception as e:
            logger.error(f"获取标的现价失败：{str(e)}")
            price_ud = None
        delisted_date = _calc_delisted_date(end_month, rule_exercise_date)

    # 商品期权缓存：同 code_month 的 delisted_date 与 price_ud 只算一次（避免重复查日历/gm）
    delisted_cache = {}   # code_month -> delisted_date
    price_ud_cache = {}   # code_month -> price_ud

    count_insert = 0
    count_update = 0
    _mdl = OptionMonitor
    with SessionLocal() as session:
        with session.begin():
            for _, row in df.iterrows():
                symbol = None  # 预定义，便于异常日志定位
                try:
                    if is_commodity_option:
                        # 商品期权（上期所）：合约代码列 + 收盘价列
                        # 不按到期月筛选，trade_date 当日所有到期月数据全部入库
                        raw_code = str(row['合约代码'])
                        m = commodity_re.match(raw_code)
                        if m is None:
                            logger.error(f"商品期权合约代码格式不符：{raw_code}")
                            continue
                        code_month, cp_flag, strike_str = m.group(1), m.group(2), m.group(3)
                        symbol = raw_code
                        option_type = 'call' if cp_flag == 'C' else 'put'
                        price_strike = Decimal(strike_str)
                        price = Decimal(str(row['收盘价']))
                        # 按合约自身的到期月计算 delisted_date（缓存避免重复查日历）
                        if code_month not in delisted_cache:
                            end_month_full = f"20{code_month}"  # 2609 -> 202609
                            delisted_cache[code_month] = _calc_delisted_date(
                                end_month_full, rule_exercise_date)
                        delisted_date = delisted_cache[code_month]
                        # 按合约自身的到期月查询标的现价（缓存避免重复调 gm）
                        if code_month not in price_ud_cache:
                            ud_symbol = f"{underlying_symbol}{code_month}"  # SHFE.au2609
                            try:
                                ud_data = call_with_timeout(current, timeout=10)(
                                    [ud_symbol], fields=['symbol', 'price'])
                                price_ud_cache[code_month] = Decimal(str(ud_data[0]['price'])) if ud_data else None
                            except Exception as e:
                                logger.error(f"获取标的现价失败：{ud_symbol} - {str(e)}")
                                price_ud_cache[code_month] = None
                        price_ud = price_ud_cache[code_month]
                    elif is_index_option:
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
