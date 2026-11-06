# -*- coding: utf-8 -*-
"""历史行情同步：gm/yfinance 获取 K 线并入库（**个股 + 指数**共用 `bds_index_history`）。

个股走后复权（ADJUST_POST）、指数走不复权（ADJUST_NONE）；标的类型由证券信息表判定，
不再依赖 `Config.INDEX_CODE` 白名单（见 `resolve_sec_type`）。
"""
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd
from gm.api import history, ADJUST_NONE, ADJUST_POST
from sqlalchemy import func

from server_fast.config import settings
from server_fast.common.utils import call_with_timeout, upsert_df_to_db, df_init_model
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import IndexHistory, SymbolInfo
from server_fast.app.bds.services.market_data.daily_valuation import (
    resolve_daily_end_date,
    MARKET_CLOSE_KLINE,
)
from server_fast.app.bds.services.symbol_info.market_board import (
    INDEX_INDUSTRY,
    resolve_sec_type,
)

# yfinance 容错导入：未安装时设为 None，运行时再判断
try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger("uvicorn.error")

# 单标的同步的确定性跳过清单（用户 2026-11-06 拍板：本轮统一跳过、存量保留不清理）
SKIP_SYMBOL_REASONS = {
    'SPX': '美股指数，本轮不纳入',
    'GC00Y': '黄金期货，本轮不纳入',
    'SHSE.510880': 'ETF（非个股/指数），本轮不纳入',
}

# 目标交易日缓存：一天内批量同步共享同一个值（trade_date 表查询一次就够）
_TARGET_TRADE_DATE_CACHE = {'date': None, 'value': None}


def _get_target_trade_date() -> date:
    """获取本轮 K 线历史行情同步的目标交易日。

    复用每日估值/市值共用的 `resolve_daily_end_date`，close_time=15:30
    （K 线收盘后出齐，比盘后估值的 17:00 早）。
    - 周末/节假日：退化到上一个交易日
    - 交易日 < 15:30：退化到上一个交易日（当日 K 线还没出齐）
    - 交易日 >= 15:30：当日
    """
    from datetime import datetime
    today = datetime.now().date()
    if _TARGET_TRADE_DATE_CACHE['date'] == today:
        return _TARGET_TRADE_DATE_CACHE['value']
    v = resolve_daily_end_date(close_time=MARKET_CLOSE_KLINE)
    _TARGET_TRADE_DATE_CACHE['date'] = today
    _TARGET_TRADE_DATE_CACHE['value'] = v
    return v


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


def _sync_one_index_detail(symbol: str, info: dict):
    """单个指数增量同步，返回 (入库条数, 错误信息)。

    条数：>0 实际入库行数；0 = 无需导入（已是最新 / 当期无数据）；-1 = 失败。
    错误信息为异常原文前 500 字（成功时 None）。

    跳过策略（参照每日市值）：
    - 跳过判据是「库内最新 trade_date ≥ 最新可拉取交易日」，**不调 gm 直接返回 0**；
    - 最新可拉取交易日 = resolve_daily_end_date(close_time=15:30)，自动退化周末/盘前；
    - gm 调用的 end_time 也用这个值。
    """
    _engine = settings.DB_ENGINE
    _mdl = IndexHistory
    try:
        # 本轮目标交易日（与 sync_symbol_history 共用缓存，不会重复查 trade_date 表）
        target_trade_date = _get_target_trade_date()
        # 查询数据库中该 symbol 的最新 trade_date
        with SessionLocal() as session:
            row = (
                session.query(_mdl.trade_date)
                .filter(_mdl.symbol == symbol)
                .order_by(_mdl.trade_date.desc())
                .first()
            )
        max_date = row[0] if row else None
        # 已追平 target_trade_date —— 不调 gm
        if max_date is not None and max_date >= target_trade_date:
            logger.info(f"->{symbol} 最新日期 {max_date} ≥ target={target_trade_date}，跳过 gm")
            return 0, None
        # 增量更新起点：有数据则从最新日期 + 1 天，否则从 listed_date 全量获取
        if max_date is not None:
            start_time = max_date + timedelta(days=1)
        else:
            start_time = info['listed_date']
        end_time = target_trade_date
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
            logger.info(f"->{symbol} gm 接口返回空（start={start_time}, end={end_time}），无新数据")
            return 0, None
        # eob（datetime）转为 date 并重命名为 trade_date
        df['eob'] = pd.to_datetime(df['eob']).dt.date
        df = df.rename(columns={'eob': 'trade_date'})
        df = df_init_model(df, _mdl)  # 清洗、列名映射、字段过滤、类型转换
        if df.empty:
            logger.info(f"->{symbol} df_init_model 后为空（start={start_time}, end={end_time}），无新数据")
            return 0, None
        df = df.replace({np.nan: None})
        _table = _mdl.__table__.name
        _unique_keys = _mdl.unique_keys
        result = upsert_df_to_db(df, _table, _engine, _unique_keys)
        logger.info(f"->{symbol} 成功：{result}")
        return len(df), None
    except Exception as e:
        raw = str(e)
        logger.error(f"->{symbol} 失败：{raw}")
        return -1, raw[:500]


def get_index_infos_from_symbol_info() -> dict:
    """从证券信息表取「A股指数」标的，组装成与 `Config.INDEX_CODE` 同构的 info。

    行情同步链路的**来源已切换为行业口径**（用户 2026-11-06 拍板 A 路线）：
    - 类型判定、标的清单都取自 `bds_symbol_info.industry='A股指数'`；
    - `Config.INDEX_CODE` 只在信息表查不到时兜底（空表回退），另外为「上市日为空」
      提供起点兜底（见 `_resolve_history_start`）。
    - 两边漂移由 `check_index_config_consistency()` 告警，不阻断同步。

    返回 {symbol: {sec_name, listed_date, data_source}}。
    """
    with SessionLocal() as session:
        rows = (
            session.query(SymbolInfo.symbol, SymbolInfo.name, SymbolInfo.online_date)
            .filter(SymbolInfo.industry == INDEX_INDUSTRY)
            .order_by(SymbolInfo.symbol)
            .all()
        )
    if not rows:
        logger.warning('证券信息表无 industry=「A股指数」的标的，回退 Config.INDEX_CODE 兜底')
        return dict(dbsCfg.INDEX_CODE)
    infos = {}
    for r in rows:
        cfg = dbsCfg.INDEX_CODE.get(r.symbol) or {}
        infos[r.symbol] = {
            'sec_name': r.name,
            # 上市日为空时用 Config 的 listed_date 兜底（个股不做兜底，见 sync_symbol_history）
            'listed_date': r.online_date or cfg.get('listed_date'),
            'data_source': 'gm',
        }
    return infos


def sync_one_index(symbol: str, info: dict | None = None) -> int:
    """单指数同步入口（供「一键同步」任务按指数逐项回报进度）。

    info 为 None 时按证券信息表（industry='A股指数'）组装，Config 兜底。
    返回：>0 条数 / 0 无需导入 / -1 失败。异常已在此吞掉，不向上抛。
    """
    if info is None:
        info = get_index_infos_from_symbol_info().get(symbol) or dbsCfg.INDEX_CODE.get(symbol)
    if not info:
        logger.warning(f'->{symbol} 不在证券信息表「A股指数」中，也无 Config 兜底')
        return -1
    count, _err = _sync_one_index_detail(symbol, info)
    return count


def upsert_index_history_sql():
    """同步全部「A股指数」的历史行情并 upsert 入库。

    标的来源改为证券信息表 `industry='A股指数'`（不再是 Config.INDEX_CODE 白名单），
    逐个调用 `_sync_one_index_detail`，单个 symbol 失败不中断后续步骤，
    返回 (steps, errors)：steps 记录每个 symbol 的获取条数，errors 记录失败原因。
    """
    steps = {}  # 记录每个 symbol 的获取条数
    errors = {}
    logger.info("指数历史行情获取并导入（来源：证券信息表 industry=A股指数）")
    for symbol, info in get_index_infos_from_symbol_info().items():
        count, err = _sync_one_index_detail(symbol, info)
        steps[symbol] = count
        if err:
            errors[symbol] = err
    return steps, errors


# ============================================================================
# 个股 + 指数：单标的历史行情同步（POST /bds/sync/history）
# ============================================================================

def _resolve_history_start(info, sec_type: str, max_date, target_trade_date: date):
    """返回 (增量起点, 跳过原因)。起点为 None 且原因为空表示「无需导入」（已是最新）。

    跳过判据：`max_date >= target_trade_date` —— 已追平最新可拉取的交易日，**不再调 gm**。

    - 库内已有该标的：`MAX(trade_date) + 1`（不重刷已有历史，后复权只新增行）。
    - 库内无数据：个股从 `online_date` 起；指数从 `online_date` 起，
      为空时用 `Config.INDEX_CODE[symbol]['listed_date']` 兜底（Config 仅作兜底，不做类型判定）。
    - 上市日期仍为空 ⇒ 跳过（用户拍板：不做固定起点兜底），原因随返回值入库。
    """
    if max_date is not None and max_date >= target_trade_date:
        return None, ''
    if max_date is not None:
        return max_date + timedelta(days=1), None

    start = info.online_date
    if start is None and sec_type == 'index':
        start = (dbsCfg.INDEX_CODE.get(info.symbol) or {}).get('listed_date')
    if start is None:
        return None, '上市日期（online_date）为空，不做固定起点兜底'
    return start, None


def sync_symbol_history(symbol: str) -> dict:
    """同步**单个标的**的历史行情：个股后复权、指数不复权。

    返回 `{status, message, reason?, count?}`，status ∈ success/no_data/skipped/error：
    - `skipped` 是「按规则确定性不处理」（类型无法判定 / 上市日为空 / 明确跳过清单），
      批量侧不重试，但会把 reason 写入任务 errors（用户要求跳过原因入库）。
    - `no_data` 表示已是最新或 gm 无新数据，属正常结束。

    口径（用户 2026-11-06 拍板）：
    - 类型判定用证券信息表：`industry='A股指数'` → index；否则 `market` 非空 → stock。
    - `adjust`：个股 `post`（后复权）/ 指数 `none`（指数不存在后复权概念）。
    - 增量只追加新行，**不重刷已有历史**（存量行 sec_type/adjust 保持 NULL）。

    跳过策略（参照每日市值）：
    - 跳过判据是「库内最新 trade_date ≥ 最新可拉取交易日」，**不调 gm 直接返回 no_data**；
    - 最新可拉取交易日 = resolve_daily_end_date(close_time=15:30)，自动退化：
      周末/节假日 → 上一交易日；交易日 < 15:30 → 上一交易日（当日 K 线未出齐）。
    - gm 调用的 end_time 也用这个值，避免给 gm 传周末日期导致空返回。
    """
    if not symbol:
        return {'status': 'error', 'message': 'symbol 不能为空'}

    reason = SKIP_SYMBOL_REASONS.get(symbol)
    if reason:
        logger.info(f"->{symbol} 按规则跳过：{reason}")
        return {'status': 'skipped', 'message': f'{symbol} 跳过：{reason}', 'reason': reason}

    # 本轮目标交易日（一天内所有标的共享同一个值）
    target_trade_date = _get_target_trade_date()

    try:
        # 一次会话取齐「标的静态信息 + 库内最新交易日」，避免两次连接
        with SessionLocal() as session:
            info = session.query(SymbolInfo).filter(SymbolInfo.symbol == symbol).first()
            if info is None:
                return {'status': 'skipped',
                        'message': f'证券信息表无此标的：{symbol}',
                        'reason': '证券信息表（bds_symbol_info）无此代码'}
            sec_type = resolve_sec_type(info.industry, info.market)
            if sec_type is None:
                reason = ('类型无法判定：industry 不是「A股指数」且 market 为空'
                          '（market 不能单独判指数）')
                logger.info(f"->{symbol} 跳过：{reason}")
                return {'status': 'skipped', 'message': f'{symbol} 跳过：{reason}',
                        'reason': reason}
            max_date = (
                session.query(func.max(IndexHistory.trade_date))
                .filter(IndexHistory.symbol == symbol)
                .scalar()
            )

            start_time, skip_reason = _resolve_history_start(info, sec_type, max_date, target_trade_date)
            if skip_reason:
                logger.info(f"->{symbol} 跳过：{skip_reason}")
                return {'status': 'skipped', 'message': f'{symbol} 跳过：{skip_reason}',
                        'reason': skip_reason}
            if start_time is None:
                # max_date 已追平 target_trade_date —— 不调 gm，直接返回
                logger.info(f"->{symbol} 已是最新（{max_date} ≥ target={target_trade_date}），跳过 gm")
                return {'status': 'no_data',
                        'message': f'已是最新交易日：{symbol}（{max_date} ≥ {target_trade_date}）',
                        'count': 0}

            # 个股后复权、指数不复权；同时落 sec_type/adjust 两列便于列表筛选与审计
            is_stock = sec_type == 'stock'
            adjust_label = 'post' if is_stock else 'none'
            # gm 调用的 end_time 对齐 target_trade_date（避免周末传过去浪费一次 gm 调用）
            end_time = target_trade_date
        # ⚠️ gm 调用放在会话之外：单只首次全量可能耗时数十秒，不能占着数据库连接等。
        # 超时 150s 与 sync_runner.HISTORY_TIMEOUT(180s) 配套：个股从上市日全量取数较慢。
        adjust = ADJUST_POST if is_stock else ADJUST_NONE
        df = call_with_timeout(history, timeout=150)(
            symbol=symbol,
            frequency='1d',
            start_time=start_time,
            end_time=end_time,
            fields='eob,symbol,open,high,low,close,amount,volume',
            adjust=adjust,
            df=True,
        )
        if df is None or df.empty:
            logger.info(f"->{symbol} gm 接口返回空（start={start_time}, end={end_time}），无新数据")
            return {'status': 'no_data',
                    'message': f'{symbol} gm 返回空（start={start_time}, end={end_time}），无新数据',
                    'count': 0}

        df['eob'] = pd.to_datetime(df['eob']).dt.date
        df = df.rename(columns={'eob': 'trade_date'})
        df = df_init_model(df, IndexHistory)
        # 两列由同步链路写入（不在 cols_map_fields 里），必须在字段过滤之后补
        df['sec_type'] = sec_type
        df['adjust'] = adjust_label
        df = df.replace({np.nan: None})
        result = upsert_df_to_db(df, IndexHistory.__table__.name,
                                 settings.DB_ENGINE, IndexHistory.unique_keys)
        logger.info(f"->{symbol} 成功（{sec_type}/{adjust_label}）：{result}")
        return {'status': 'success', 'count': len(df),
                'message': f'{symbol} 导入 {len(df)} 条（{sec_type}/{adjust_label}）'}
    except Exception as e:
        raw = str(e)
        logger.error(f"->{symbol} 历史行情失败：{raw}")
        return {'status': 'error', 'message': raw[:500]}


def validate_history_targets(symbols) -> dict:
    """校验历史行情的目标清单（「监测标的」与「手写清单」共用），**只读**。

    规则：
    - 去空白、去重（保留首次出现顺序），重复项记入 `duplicates`；
    - 不在 `bds_symbol_info` → 不可用（证券信息表是唯一真源）；
    - `resolve_sec_type` 判不出类型（industry 非「A股指数」且 market 为空）→ 不可用；
    - 命中 `SKIP_SYMBOL_REASONS`（SPX / GC00Y / SHSE.510880）→ 不可用并给原因。

    返回值：
      {items: [{symbol, name, sec_type, ok, reason?}], valid: [symbol], invalid: [...], duplicates: [symbol]}
    """
    seen, uniq, duplicates = set(), [], []
    for raw in symbols or []:
        code = (raw or '').strip()
        if not code:
            continue
        if code in seen:
            duplicates.append(code)
            continue
        seen.add(code)
        uniq.append(code)

    rows = []
    if uniq:
        with SessionLocal() as session:
            rows = (
                session.query(SymbolInfo.symbol, SymbolInfo.name,
                              SymbolInfo.industry, SymbolInfo.market)
                .filter(SymbolInfo.symbol.in_(uniq))
                .all()
            )
    info_map = {r.symbol: r for r in rows}

    items, valid, invalid = [], [], []
    for code in uniq:
        row = info_map.get(code)
        name = row.name if row else None
        sec_type = resolve_sec_type(row.industry, row.market) if row else None
        # 跳过清单优先判定：这些代码即使类型可判，本轮也不处理
        reason = SKIP_SYMBOL_REASONS.get(code)
        if reason is None and row is None:
            reason = '证券信息表（bds_symbol_info）无此代码'
        if reason is None and sec_type is None:
            reason = '类型无法判定：industry 不是「A股指数」且 market 为空'
        item = {'symbol': code, 'name': name, 'sec_type': sec_type, 'ok': reason is None}
        if reason:
            item['reason'] = reason
            invalid.append(item)
        else:
            valid.append(code)
        items.append(item)
    return {'items': items, 'valid': valid, 'invalid': invalid, 'duplicates': duplicates}


def check_index_config_consistency() -> list[str]:
    """校验 `Config.INDEX_CODE` 与证券信息表 `industry='A股指数'` 的一致性（只告警不改数据）。

    行情同步链路的类型判定已改为行业口径，Config 仅在「上市日为空时兜底」和
    展示名上保留作用。两边一旦漂移（信息表被误改 / Config 漏配），必须让人看得见，
    否则会出现「Config 里有、但同步链路不认」这类静默偏差。
    返回告警文案列表（空列表 = 一致）。
    """
    with SessionLocal() as session:
        rows = (
            session.query(SymbolInfo.symbol, SymbolInfo.name)
            .filter(SymbolInfo.industry == INDEX_INDUSTRY)
            .all()
        )
    info_map = {r.symbol: r.name for r in rows}
    cfg_map = dbsCfg.INDEX_CODE
    warnings = []
    for code in sorted(set(cfg_map) - set(info_map)):
        warnings.append(f'Config.INDEX_CODE 含 {code}，但证券信息表 industry 不是「{INDEX_INDUSTRY}」')
    for code in sorted(set(info_map) - set(cfg_map)):
        warnings.append(f'证券信息表标为「{INDEX_INDUSTRY}」的 {code} 未收录进 Config.INDEX_CODE')
    for code in sorted(set(cfg_map) & set(info_map)):
        cfg_name = (cfg_map[code] or {}).get('sec_name')
        if cfg_name and info_map[code] and cfg_name != info_map[code]:
            warnings.append(f'{code} 名称不一致：Config={cfg_name} / 信息表={info_map[code]}')
    for w in warnings:
        logger.warning(f'[指数配置一致性] {w}')
    return warnings

