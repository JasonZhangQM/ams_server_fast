# -*- coding: utf-8 -*-
"""标的监测（UnderlyingMonitor）业务函数：年度行情更新 + 实时行情入库。

- update_underlying_monitor_hlc_sql：拉取年初至今行情，更新 py_close/y_high/y_low
- update_underlying_monitor_em_orm：gm current 获取最新价，ORM flush 触发钩子计算 pv_*
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
from server_fast.app.irs.models import UnderlyingMonitor, _compute_underlying_monitor
from server_fast.app.irs.service.common import logger


def get_history_em_df(symbols: list):
    """获取 symbols 上年度末至本年度最近的最高价、最低价和收盘价。"""
    _mdl = TradeDate
    last_year = (datetime.now().year) - 1
    # 查询上一年度最后一个交易日：用 MySQL YEAR() 函数过滤
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


def handle_hlc_df(history_data) -> dict:
    """把 gm history 结果归并成 ``{symbol: {py_close, y_high, y_low}}``。

    2026-11-20 由「返回 DataFrame」改为「返回 dict」，两个原因：
    - 旧版把 ``hlc_df = pd.DataFrame(hlc_list)`` 写在 for 循环体内（缩进错误），
      symbols 为空时抛 UnboundLocalError。
    - gm 覆盖不到的外盘（GC00Y / SPX，实测 history 恒返回空）**不会出现在返回里**。
      旧版只遍历「返回里的 symbol」，于是这两家被静默漏掉 —— 这正是三列冻结的
      直接原因。改成 dict 后，调用方可用 ``symbol not in fetched`` 明确识别
      「gm 没有这家」，从而走兜底逻辑，而不是当它不存在。
    """
    if history_data is None or len(history_data) == 0:
        return {}
    out = {}
    for symbol, g in history_data.groupby('symbol'):
        g = g.sort_values('eob')
        out[symbol] = {
            'py_close': g['close'].iloc[0],  # 首根 = 上年最后一个交易日
            'y_high': g['high'].max(),
            'y_low': g['low'].min(),
        }
    return out


def _as_dec(v):
    """numpy / float / Decimal / None → Decimal；无法转换时返回 None。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return None if d.is_nan() else d


_TOL = Decimal("0.0001")
"""一致性校验的相对容差（万分之一）。

gm 返回的 high/low 是两位小数，而 price 可能带更多位（如 000905 当日
low=6006.08、price=6006.0796），直接比较会误报「年内最低高于现价」。
按万分之一放宽，既滤掉这种舍入噪声，又抓得住真正的滞后
（GC00Y 3,525 vs 3,955.40 差 12%，远超容差）。
"""


def _recalc_pv() -> int:
    """重算全表 pv_*（复用模型钩子，不另写一套公式）。返回处理行数。

    必须单独做：``upsert_df_to_db`` 走原生 SQL，绕过 SQLAlchemy 的
    before_update 钩子，写完 py_close/y_high/y_low 后 pv_yh/pv_yl/pv_yy
    仍停在上一次 price 更新时算出的旧值 —— 表现就是「档位表里年低偏离还
    是老数字」。这里对每行调用同一个钩子函数并 commit，钩子内部 setattr
    触发 UPDATE，值没变的行不会产生额外写入。
    """
    _mdl = UnderlyingMonitor
    n = 0
    with SessionLocal() as session:
        with session.begin():
            for monitor in session.query(_mdl).all():
                _compute_underlying_monitor(None, None, monitor)
                n += 1
    return n


def update_underlying_monitor_hlc_sql() -> int:
    """更新 py_close / y_high / y_low 并重算 pv_*，返回写入的标的数（0 = 无数据）。

    2026-11-20 三处修正，对应「现价低于年内最低」事故：

    1. **外盘兜底**：gm ``history`` 覆盖不到 GC00Y / SPX（实测 rows=[]）。
       旧版让这两家的三列永久冻结在首次录入值，于是 11-20 出现
       「现价 3,525 < 年内最低 3,955.40」。现在对 gm 无历史的标的按当日价
       增量推进（``y_low=min(旧, 现价)``、``y_high=max(旧, 现价)``），
       每天至少前进一格，不会再冻住。
    2. **一致性校验**：即便 gm 有历史，当日 low 也可能尚未入库
       （实测 11-17~11-20 连续四天，y_low 停在 11-16 的低点）。
       写入前用现价校验 ``[y_low, y_high]``，越界即按现价修正并打 error，
       不让自相矛盾的数据静默落库。
    3. **重算 pv_*** ：见 ``_recalc_pv``。

    ``fields_hlc_update`` 只含这三列，故 pp_* 等用户手工配置不会被覆盖。
    """
    _mdl = UnderlyingMonitor
    with SessionLocal() as session:
        rows = session.query(
            _mdl.symbol, _mdl.price, _mdl.py_close, _mdl.y_high, _mdl.y_low
        ).all()
    if not rows:  # 空表跳过，避免无意义调用 gm 接口
        logger.info("->无需更新")
        return 0

    symbols = [r[0] for r in rows]
    fetched = handle_hlc_df(get_history_em_df(symbols))

    hlc_list, no_history = [], []
    for symbol, price, py_close, y_high, y_low in rows:
        src = fetched.get(symbol)
        if src:
            py_close = src['py_close']
            y_high = src['y_high']
            y_low = src['y_low']
        else:
            # gm 拿不到历史（外盘）：用当日价增量推进，避免三列冻结
            no_history.append(symbol)
            if price is not None:
                py_close = price if py_close is None else py_close
                y_high = price if y_high is None else max(y_high, price)
                y_low = price if y_low is None else min(y_low, price)

        # 一致性校验：无条件让 [y_low, y_high] 包住现价（只放宽、不收紧）。
        # 语义是「年内最低不可能高于现价」——越界一定是数据问题，宁可放宽也不能
        # 让矛盾值落库。但日志只在**超出容差**时记：high/low 是两位小数、price
        # 位数更多，000905 那种 6006.08 vs 6006.0796 的差属于舍入，不该刷 error。
        p = _as_dec(price)
        if p is not None:
            lo, hi = _as_dec(y_low), _as_dec(y_high)
            if lo is None or p < lo:
                if lo is not None and p < lo * (Decimal("1") - _TOL):
                    logger.error(
                        f"->{symbol} 年内最低 {lo} 高于现价 {p}：数据源滞后，"
                        f"按现价修正（原值作废）"
                    )
                y_low = price
            if hi is None or p > hi:
                if hi is not None and p > hi * (Decimal("1") + _TOL):
                    logger.error(
                        f"->{symbol} 年内最高 {hi} 低于现价 {p}：数据源滞后，"
                        f"按现价修正（原值作废）"
                    )
                y_high = price

        hlc_list.append({
            'symbol': symbol,
            'py_close': py_close,
            'y_high': y_high,
            'y_low': y_low,
        })

    if no_history:
        logger.warning(
            "->gm 无历史、按现价增量推进：" + "、".join(no_history)
        )

    df_in = df_init_model(pd.DataFrame(hlc_list), _mdl)
    upsert_df_to_db(
        df_in,
        _mdl.__table__.name,
        settings.DB_ENGINE,
        _mdl.unique_keys,
        _mdl.fields_hlc_update,
    )
    recalc = _recalc_pv()
    logger.info(
        f'->年度行情更新 {len(hlc_list)} 条，重算 pv_* {recalc} 条'
        f'（gm 无历史 {len(no_history)} 条）'
    )
    return len(hlc_list)


def update_underlying_monitor_em_orm():
    """通过 gm current 获取实时行情，ORM 更新 UnderlyingMonitor.price 并 flush 触发钩子。

    钩子直接读本表 pp_*/py_close 字段计算指标。
    返回 (count_insert, count_update)，其中 insert 恒为 0（记录已存在）。
    """
    _mdl = UnderlyingMonitor
    with SessionLocal() as session:
        rows = session.query(_mdl.id, _mdl.symbol).all()
        um_dict = {row.symbol: row.id for row in rows}
    if not um_dict:  # 空表跳过，避免无意义调用 gm 接口
        return 0, 0
    # 获取实时行情（带超时保护，防止 gm 终端未启动时无限阻塞）
    try:
        um_data = call_with_timeout(current, timeout=10)(
            list(um_dict.keys()), fields=['symbol', 'price'])
    except Exception as e:
        logger.error(f"******获取实时行情失败：{str(e)}")
        raise e
    um_data_dict = {item['symbol']: item['price'] for item in um_data}
    count_update = 0
    with SessionLocal() as session:
        with session.begin():
            for underlying_symbol, um_id in um_dict.items():
                if underlying_symbol not in um_data_dict:  # 无实时行情则跳过
                    logger.warning(f"无实时行情：{underlying_symbol}")
                    continue
                price_d = Decimal(str(um_data_dict[underlying_symbol]))
                try:
                    monitor = session.query(_mdl).filter(_mdl.id == um_id).one_or_none()
                    if monitor is not None:
                        monitor.price = price_d
                        session.flush()  # 触发 before_update 钩子计算 pv_*
                        count_update += 1
                except Exception as e:
                    logger.error(f"处理 symbol {underlying_symbol} 失败：{str(e)}")
                    continue
    return 0, count_update
