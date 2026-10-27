# -*- coding: utf-8 -*-
"""期权监测（OptionMonitor）业务函数：akshare 期权行情 + gm 标的现价同步入库。

- option_monitor_sync_orm：按到期月同步行情，upsert 到 OptionMonitor 表

🔄 **2026-10-27 到期日算法已迁出本模块**：`_calc_delisted_date()` 的实现搬到
`bds.services.market_data.option_market`（基础数据的算法归基础数据层），
本模块改为**引用**它。同期 `OPTIONS_MARCH` / `RULE_EXERCISE_DATE` 的正源也归
`bds.config`，本模块只读不再定义 —— 依赖方向自此固定为 irs → bds。
"""
import re
from datetime import datetime, time as dtime
from decimal import Decimal, InvalidOperation

from gm.api import *  # noqa: F401,F403  保留原 gm SDK 通配导入（current 等）

from server_fast.common.db import SessionLocal
from server_fast.common.utils import call_with_timeout
from server_fast.app.bds.config import Config as BdsCfg
from server_fast.app.bds.models import TradeDate
from server_fast.app.bds.services.market_data.option_market import _calc_delisted_date
from server_fast.app.irs.models import OptionMonitor
from server_fast.app.irs.service.common import logger


def option_monitor_sync_orm(option_name: str, end_month: str):
    """通过 akshare + gm 获取期权行情并 upsert 到 OptionMonitor 表。

    :param option_name: 期权品种名称（对应 `bds.config.Config.OPTIONS_MARCH` 的 option_name）
    :param end_month: 到期年月，格式 "YYYYMM"（如 "202608"）
    :return: 插入+更新的总条数
    """
    # 1. 从配置查找 underlying_symbol、multiplier、rule_exercise_date
    config_item = None
    for item in BdsCfg.OPTIONS_MARCH:
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
        # 商品期权：option_hist_shfe 需指定 trade_date
        # trade_date 取值规则：交易日且已收盘(>=15:00)用当日；否则取上一个交易日
        now = datetime.now()
        today = now.date()
        is_closed = now.time() >= dtime(15, 0)  # 国内期货 15:00 收盘
        with SessionLocal() as session:
            is_trade_day = session.query(TradeDate.trade_date).filter(
                TradeDate.trade_date == today
            ).first() is not None
            if is_trade_day and is_closed:
                trade_date = today
            else:
                # 非交易日 或 交易日未收盘 → 取上一个交易日
                last_trade = session.query(TradeDate.trade_date).filter(
                    TradeDate.trade_date < today
                ).order_by(TradeDate.trade_date.desc()).first()
                if last_trade is None:
                    logger.info(f"TradeDate 日历无 {today} 之前的交易日")
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
                        # 收盘价可能为空字符串/None/'-'等无效值，跳过无价数据行
                        close_raw = row['收盘价']
                        if close_raw is None or str(close_raw).strip() in ('', '-', 'None', 'nan'):
                            logger.warning(f"商品期权 {raw_code} 收盘价无效，跳过：{close_raw!r}")
                            continue
                        try:
                            price = Decimal(str(close_raw))
                        except InvalidOperation:
                            # ConversionSyntax 是 InvalidOperation 子类，一并捕获
                            logger.warning(f"商品期权 {raw_code} 收盘价无法解析，跳过：{close_raw!r}")
                            continue
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
