# -*- coding: utf-8 -*-
"""指数历史行情同步：gm/yfinance 获取指数历史 K 线并入库。"""
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd
from gm.api import history, ADJUST_NONE

from server_fast.config import settings
from server_fast.common.utils import call_with_timeout, upsert_df_to_db, df_init_model
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import IndexHistory

# yfinance 容错导入：未安装时设为 None，运行时再判断
try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger("uvicorn.error")


def _fetch_index_history_from_yfinance(symbol, info, start_time, end_time):
    """通过 yfinance 拉取指数历史行情，返回标准化 DataFrame。

    yfinance 返回的 DataFrame 列映射：
    - Date (index) → eob (保持 datetime 类型，与 gm 路径一致，由调用方统一转 trade_date)
    - Open → open
    - High → high
    - Low → low
    - Adj Close → close (优先使用复权价；若无则用 Close)
    - Volume → volume
    - amount = close * volume (yfinance 不直接提供成交额，用近似值)
    - symbol 列填充为传入的 symbol 值

    参数：
    - symbol: 配置中的 symbol 键（如 'SP500'）
    - info: 配置字典（含 yf_ticker）
    - start_time: date 类型，开始日期
    - end_time: date 类型，结束日期

    返回：
    - DataFrame，列包含 eob, symbol, open, high, low, close, volume, amount
    - yfinance 不可用、调用失败或返回空时返回 None
    """
    try:
        if yf is None:
            logger.warning("yfinance 未安装，无法拉取 yfinance 数据源指数行情")
            return None
        # yfinance 默认在 ~/.cache/py-yfinance 维护 SQLite 时区缓存
        # 某些环境下默认缓存路径无写权限会抛 OperationalError，改为项目可写目录
        try:
            import os
            cache_dir = os.path.join(os.path.expanduser('~'), '.cache', 'py-yfinance')
            os.makedirs(cache_dir, exist_ok=True)
            yf.set_tz_cache_location(cache_dir)
        except Exception:
            pass  # 设置失败也继续尝试，可能旧版 yfinance 无此 API
        # 使用 Ticker.history 而非 yf.download：后者依赖 requests-cache 写 SQLite
        # 在某些环境下缓存路径无写权限会抛 OperationalError 导致下载失败
        ticker = yf.Ticker(info['yf_ticker'])
        df = ticker.history(
            start=start_time.isoformat(),
            end=end_time.isoformat(),
            auto_adjust=False,
            raise_errors=False,
        )
        if df is None or df.empty:
            return None
        df = df.reset_index()  # Date 索引转列
        # 列重命名：Date → eob，OHLCV 标准化
        df = df.rename(columns={
            'Date': 'eob',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Volume': 'volume',
        })
        # close 列：优先使用 Adj Close，否则用 Close
        if 'Adj Close' in df.columns:
            df['close'] = df['Adj Close']
        elif 'Close' in df.columns:
            df['close'] = df['Close']
        # amount = close * volume（yfinance 不直接提供成交额，用近似值）
        df['amount'] = df['close'] * df['volume']
        # 填充 symbol 列
        df['symbol'] = symbol
        # 选取并顺序化目标列，eob 保持 datetime 类型，由调用方统一转 date
        df = df[['eob', 'symbol', 'open', 'high', 'low', 'close', 'volume', 'amount']]
        return df
    except Exception as e:
        logger.error(f"yfinance 拉取 {symbol} 失败：{str(e)}")
        return None


def upsert_index_history_sql():
    """循环获取 INDEX_CODE 中所有指数的历史行情并 upsert 入库。

    增量更新策略：
    - 数据库已有该 symbol 数据：从最新日期 + 1 天开始获取
    - 数据库无该 symbol 数据：从 listed_date 开始全量获取

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的获取条数。
    """
    _engine = settings.DB_ENGINE
    _mdl = IndexHistory
    _index_code = dbsCfg.INDEX_CODE
    steps = {}  # 记录每个 symbol 的获取条数
    logger.info("指数历史行情获取并导入")
    for symbol, info in _index_code.items():
        try:
            # 查询数据库中该 symbol 的最新 trade_date
            with SessionLocal() as session:
                row = (
                    session.query(_mdl.trade_date)
                    .filter(_mdl.symbol == symbol)
                    .order_by(_mdl.trade_date.desc())
                    .first()
                )
            max_date = row[0] if row else None
            # 优化：若最新日期已是今日，说明当日已收盘且数据已入库，跳过接口调用
            today = date.today()
            if max_date is not None and max_date >= today:
                logger.info(f"->{symbol} 最新日期 {max_date} 已是今日，跳过同步")
                steps[symbol] = 0
                continue
            # 增量更新起点：有数据则从最新日期 + 1 天，否则从 listed_date 全量获取
            if max_date is not None:
                start_time = max_date + timedelta(days=1)
            else:
                start_time = info['listed_date']
            end_time = today
            # 根据 data_source 选择数据源：yfinance 走新路径，默认走 gm
            if info.get('data_source') == 'yfinance':
                df = _fetch_index_history_from_yfinance(symbol, info, start_time, end_time)
            else:
                # 调用 gm 接口获取历史行情（带超时保护，防止 gm 终端未启动时阻塞）
                # fields 包含 amount/volume 用于量价分析；position 为期货专用字段，
                # gm 对股票指数不返回，同步时该列缺失由 df_init_model 过滤后入库为 null
                df = call_with_timeout(history, timeout=30)(
                    symbol=symbol,
                    frequency='1d',
                    start_time=start_time,
                    end_time=end_time,
                    fields='eob,symbol,open,high,low,close,amount,volume',
                    adjust=ADJUST_NONE,
                    df=True,
                )
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            # eob（datetime）转为 date 并重命名为 trade_date
            df['eob'] = pd.to_datetime(df['eob']).dt.date
            df = df.rename(columns={'eob': 'trade_date'})
            df = df_init_model(df, _mdl)  # 清洗、列名映射、字段过滤、类型转换
            if not df.empty:
                df = df.replace({np.nan: None})
                _table = _mdl.__table__.name
                _unique_keys = _mdl.unique_keys
                result = upsert_df_to_db(df, _table, _engine, _unique_keys)
                logger.info(f"->{symbol} 成功：{result}")
                steps[symbol] = len(df)
            else:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps
