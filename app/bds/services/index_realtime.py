# -*- coding: utf-8 -*-
"""指数实时行情获取。

按 Config.INDEX_CODE[symbol].get('data_source') 分发到不同数据源：
- 默认（A 股）：通过 gm api 的 current() 获取最新价（带超时保护）
- 'yfinance'：通过 yfinance.Ticker(yf_ticker).fast_info.last_price 获取最新价

任意子集失败不影响其他子集，整体 try/except 捕获并记录 warning 日志。
"""
import logging

from gm.api import current

from server_fast.app.bds.config import Config as dbsCfg
from server_fast.common.utils import call_with_timeout

logger = logging.getLogger("uvicorn.error")


def fetch_realtime_index_prices(symbols: list[str]) -> tuple[dict[str, float], str | None]:
    """获取指数实时最新价。

    :param symbols: 指数代码列表（应为 Config.INDEX_CODE 的键集合）
    :return: (price_dict, realtime_source) 元组
        - price_dict: {symbol: 最新价} 仅包含成功获取的 symbol
        - realtime_source: 数据来源标签
            * 'gm'      仅 gm 成功
            * 'yfinance' 仅 yfinance 成功
            * 'mixed'   gm 与 yfinance 都有成功项
            * None      全部失败
    """
    price_dict: dict[str, float] = {}
    gm_ok = False
    yf_ok = False

    # 按 data_source 分组：gm 路径合并一次调用，yfinance 路径逐个调用
    gm_symbols: list[str] = []
    yf_items: list[tuple[str, str]] = []  # (symbol, yf_ticker)
    for sym in symbols:
        cfg = dbsCfg.INDEX_CODE.get(sym, {})
        if cfg.get('data_source') == 'yfinance':
            yf_ticker = cfg.get('yf_ticker')
            if yf_ticker:
                yf_items.append((sym, yf_ticker))
        else:
            gm_symbols.append(sym)

    # ---- gm 路径：A 股指数统一一次调用 ----
    if gm_symbols:
        try:
            sv_data = call_with_timeout(current, timeout=10)(
                gm_symbols, fields=['symbol', 'price'])
            for item in sv_data or []:
                sym = item.get('symbol')
                price = item.get('price')
                if sym is None or price is None:
                    # 过滤 price 为 None 的项
                    continue
                try:
                    price_dict[sym] = float(price)
                    gm_ok = True
                except (TypeError, ValueError):
                    # 价格字段无法转 float 时跳过该 symbol
                    continue
        except Exception as e:
            logger.warning(
                "fetch_realtime_index_prices: gm 调用失败，symbols=%s，原因=%s",
                gm_symbols, str(e))

    # ---- yfinance 路径：每个 symbol 单独 try/except ----
    if yf_items:
        try:
            import yfinance as yf
        except ImportError as e:
            logger.warning(
                "fetch_realtime_index_prices: yfinance 未安装，跳过 %d 个标的，原因=%s",
                len(yf_items), str(e))
            yf = None

        if yf is not None:
            for sym, yf_ticker in yf_items:
                try:
                    ticker = yf.Ticker(yf_ticker)
                    price = None
                    # 优先用 fast_info.last_price（更轻量），失败回退到 history
                    try:
                        price = ticker.fast_info.last_price
                    except Exception:
                        hist = ticker.history(period='1d')
                        if hist is not None and not hist.empty:
                            price = hist['Close'].iloc[-1]
                    if price is not None:
                        price_dict[sym] = float(price)
                        yf_ok = True
                except Exception as e:
                    logger.warning(
                        "fetch_realtime_index_prices: yfinance 获取失败，"
                        "symbol=%s, yf_ticker=%s，原因=%s",
                        sym, yf_ticker, str(e))

    # 计算 source_label
    if gm_ok and yf_ok:
        source_label = 'mixed'
    elif gm_ok:
        source_label = 'gm'
    elif yf_ok:
        source_label = 'yfinance'
    else:
        source_label = None

    return price_dict, source_label
