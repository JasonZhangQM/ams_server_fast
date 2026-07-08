# -*- coding: utf-8 -*-
"""bds 应用业务函数（从 server_dj/apps/bds/api.py 迁移）。

迁移要点：
- 移除 Django 专用 import（django.conf、django.db）。
- settings 改从 server_fast.config 导入；工具函数与模型改用 server_fast 路径。
- Django 的 _mdl._meta.db_table 改为 SQLAlchemy 的 _mdl.__table__.name。
- 保留 pandas + settings.DB_ENGINE 逻辑（不改为 session）。
"""
import pandas as pd
import numpy as np
import logging
from gm.api import *
from datetime import datetime, date, timedelta
from server_fast.config import settings
from server_fast.common.utils import *
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import *

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 的 logger，输出到 stderr 不被缓冲


# 获取交易日历并存入数据库
def insert_trade_date_em_sql():
    '''
    获取交易日历并存入数据库
    field = 'trade_date'
    mdl = TradeDate

    返回值：新增的交易日数量（int），0 表示无需更新
    '''
    _engine = settings.DB_ENGINE
    _field = 'trade_date'
    _mdl = TradeDate
    logger.info("交易日历获取并导入")
    max_date = get_field_max_sql(_field,_mdl,_engine)
    today_year = datetime.today().year
    if max_date is None:
        max_year = 1991
    else:
        max_year = max_date.year
    if max_year >= today_year:
        logger.info("->已经有最新数据，无需调取接口")
        return 0
    df = get_trading_dates_by_year( #调取em接口获取交易日历
        'SHSE', max_year, datetime.today().year)
    df = df[df['trade_date']!='']
    if max_date:
        df = df[df['trade_date']>str(max_date)]
    if not df.empty: # 交易日数据不为空
        df = df_init_model(df,_mdl) # 过滤字段
        _table = _mdl.__table__.name  # SQLAlchemy 表名（原 Django: _meta.db_table）
        df.to_sql(_table,_engine,if_exists='append', index=False)
        logger.info(f"->成功 {len(df)}")
        return len(df)
    else:
        logger.info("->无需导入")
        return 0

# 导入证券基本信息(东财)
def upsert_symbol_info_excel_sql():
    _engine = settings.DB_ENGINE
    _mdl = SymbolInfo
    _folder = dbsCfg.FOLDER_SYMBOL
    _map_market_code = dbsCfg.MAP_MARKET_CODE
    _fields_replace = _mdl.fields_replace

    logger.info("证券基本信息导入")
    file_names = [
        f.name for f in _folder.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
            and (not f.name.startswith('~'))
            ]
    for file_name in file_names:
        df = pd.read_excel(_folder/file_name,dtype=str)
        df = df.map( # 去除空格
            lambda x: x.strip() if isinstance(x, str) else x)
        df.columns = df.columns.str.strip()  # 去除列名首尾空格（如 ' 所属行业' → '所属行业'）
        df = df.rename(columns=_mdl.map_fields()) # 外部列名映射数据库字段
        df = df[filter_in_cols(df.columns,_mdl.db_fields())] # 过滤字段
        df['symbol'] = df['symbol'].map( # 代码市场映射
            lambda x: _map_market_code[x[0]])+'.'+df['symbol']
        df[_fields_replace] = df[_fields_replace].replace('—', np.nan)
        df = df.astype(filter_dtypes(df.columns,_mdl.to_dtype())) # 转换数据类型
        if not df.empty:
            df = df.replace({np.nan: None})
            _table = _mdl.__table__.name  # SQLAlchemy 表名（原 Django: _meta.db_table）
            _unique_keys = _mdl.unique_keys
            result = upsert_df_to_db(df, _table, _engine, _unique_keys)
            logger.info(f"->成功：{result}")
        else:
            logger.info(f"->无需导入：{file_name}")


# 循环获取 INDEX_CODE 中所有指数的历史行情并 upsert 入库
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
            # 增量更新起点：有数据则从最新日期 + 1 天，否则从 listed_date 全量获取
            if max_date is not None:
                start_time = max_date + timedelta(days=1)
            else:
                start_time = info['listed_date']
            end_time = date.today()
            # 调用 gm 接口获取历史行情（带超时保护，防止 gm 终端未启动时阻塞）
            df = call_with_timeout(history, timeout=30)(
                symbol=symbol,
                frequency='1d',
                start_time=start_time,
                end_time=end_time,
                fields='eob,symbol,open,high,low,close',
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


# 循环获取 INDEX_CODE 中所有指数指定日期的成分股并追加入库
def upsert_index_constituent_sql(trade_date=None):
    """循环获取 INDEX_CODE 中所有指数指定日期的成分股并追加入库。

    同步策略：
    - trade_date 为 None：获取每个指数最新交易日的成分股
    - trade_date 指定：获取每个指数该日期的成分股

    成分股变更检测：若当前获取的成分股集合与数据库中该 index_code 最新已保存
    交易日的成分股集合一致，则跳过不保存，避免存储无变化的冗余快照。

    单个 index_code 失败不中断后续步骤，返回 steps 字典记录每个 index_code 的结果
    （1=已保存，0=未变化或空数据跳过，-1=失败）。
    """
    _engine = settings.DB_ENGINE
    _mdl = IndexConstituent
    _index_code = dbsCfg.INDEX_CODE
    steps = {}  # 记录每个 index_code 的保存结果
    logger.info("指数成分股获取并导入")

    def _normalize_weight(w):
        """权重归一化：NaN/None 统一转 0.0，其余保留 4 位小数。"""
        if w is None or pd.isna(w):
            return 0.0
        return round(float(w), 4)

    # trade_date 转字符串供 API 使用，None 表示获取最新交易日数据
    trade_date_str = trade_date.strftime('%Y-%m-%d') if trade_date else None

    for index_code, info in _index_code.items():
        try:
            # ---- 查询数据库中该 index_code 最新已保存的 trade_date 及其成分股集合 ----
            with SessionLocal() as session:
                row = (
                    session.query(_mdl.trade_date)
                    .filter(_mdl.index_code == index_code)
                    .order_by(_mdl.trade_date.desc())
                    .first()
                )
                max_date = row[0] if row else None

                if max_date is not None:
                    last_rows = (
                        session.query(_mdl.symbol, _mdl.weight)
                        .filter(
                            _mdl.index_code == index_code,
                            _mdl.trade_date == max_date,
                        )
                        .all()
                    )
                    last_saved_set = {
                        (r.symbol, _normalize_weight(r.weight)) for r in last_rows
                    }
                else:
                    last_saved_set = set()

            # ---- 调用 gm 接口获取成分股（带超时保护，防止 gm 终端未启动时阻塞） ----
            df = call_with_timeout(stk_get_index_constituents, timeout=30)(
                index=index_code,
                trade_date=trade_date_str,
            )
            # API 返回空 DataFrame 时跳过（非交易日或数据尚未更新）
            if df is None or df.empty:
                logger.info(f"->{index_code} API 返回空数据，跳过")
                steps[index_code] = 0
                continue

            # 仅取需要的四列
            df = df[['index', 'symbol', 'weight', 'trade_date']]
            # 构建当前成分股集合用于变更检测
            current_set = set(zip(
                df['symbol'],
                df['weight'].apply(_normalize_weight),
            ))
            # 成分股集合未变化则跳过不保存
            if current_set == last_saved_set:
                logger.info(f"->{index_code} 成分股未变化，跳过")
                steps[index_code] = 0
                continue

            # 重命名列 index → index_code（index 为 SQL 保留字）
            df = df.rename(columns={'index': 'index_code'})
            # 转换 trade_date 为 date 类型（API 返回字符串格式 %Y-%m-%d）
            df['trade_date'] = pd.to_datetime(
                df['trade_date'], format='%Y-%m-%d'
            ).dt.date
            # weight 转 float，NaN 统一转 0
            df['weight'] = df['weight'].apply(_normalize_weight)
            # 追加写入数据库
            df.to_sql(_mdl.__table__.name, _engine, if_exists='append', index=False)

            logger.info(f"->{index_code} 成功保存")
            steps[index_code] = 1
        except Exception as e:
            # 单步失败不中断后续 index_code，记录错误并继续
            logger.error(f"->{index_code} 失败：{str(e)}")
            steps[index_code] = -1
    return steps
