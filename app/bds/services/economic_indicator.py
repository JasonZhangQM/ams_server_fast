# -*- coding: utf-8 -*-
"""bds 经济指标业务函数（从 service.py 拆分）。

包含美国宏观经济指标的获取与 upsert 入库逻辑：
- FRED API 优先（fred_series_id 配置时）
- akshare fallback（三种列模式 A/B/C）
- 单指标同步 / 全量同步
"""
import logging

import numpy as np
import pandas as pd
from datetime import date, datetime, timedelta
from sqlalchemy import func

from server_fast.config import settings
from server_fast.common.utils import *  # noqa: F401,F403（upsert_df_to_db / call_with_timeout / fetch_json_with_timeout 等）
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import EconomicIndicator

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 的 logger


def upsert_economic_indicator_sql(indicator_code):
    """同步单个美国宏观经济指标数据并 upsert 入库。

    数据源优先级：
    1. FRED API（圣路易斯联储聚合 Fed/BLS/BEA/Census/ISM/CB 等原始源）
       - 配置 fred_series_id 时使用 FRED
       - fred_units: lin=原始值 pch=环比 pct pc1=同比 pct pca=复合年化 pct
       - FRED 不提供预期值（value_expected 置空），前值通过 shift(1) 计算
    2. akshare（fallback）：fred_series_id 缺失时回退到 akshare 三种列模式（A/B/C）

    增量策略：
    - 月度/季度指标：查询 DB 最大 report_date，仅导入新增行；无数据从 2010-01-01 全量
    - 日度指标（YIELD_*）：获取最近 365 天数据

    返回值：插入/更新条数（int），异常返回 -1。
    """
    _engine = settings.DB_ENGINE
    _mdl = EconomicIndicator

    # 获取指标元信息
    meta = dbsCfg.ECONOMIC_INDICATORS.get(indicator_code)
    if meta is None:
        logger.warning(f"未知经济指标代码：{indicator_code}")
        return -1

    logger.info(f"经济指标 {indicator_code}（{meta['name']}）获取并导入")
    try:
        fred_series_id = meta.get('fred_series_id')

        if fred_series_id:
            # ===== FRED API 路径 =====
            df = _fetch_economic_indicator_from_fred(indicator_code, meta)
        else:
            # ===== akshare fallback 路径 =====
            df = _fetch_economic_indicator_from_akshare(indicator_code, meta)

        if df is None or df.empty:
            logger.info(f"->{indicator_code} 无需导入")
            return 0

        # 添加元信息列
        df['indicator_code'] = indicator_code
        df['indicator_name'] = meta['name']
        df['category'] = meta['category']
        df['country'] = meta['country']
        df['unit'] = meta['unit']
        df['frequency'] = meta['frequency']

        # 日期列转换（空值/异常值转为 NaT 后过滤）
        df['report_date'] = pd.to_datetime(df['report_date'], errors='coerce').dt.date
        if 'pub_date' in df.columns:
            df['pub_date'] = pd.to_datetime(df['pub_date'], errors='coerce').dt.date

        # 数值列转换（非数值转为 NaN 后过滤）
        for col in ['value', 'value_prev', 'value_expected']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 过滤掉 report_date 或 value 为 NaN 的行
        df = df[df['report_date'].notna() & df['value'].notna()]

        # 增量过滤
        # 日度指标（frequency=daily）：仅保留最近 365 天
        # 月度/季度指标：查询 DB 最大 report_date，仅导入新增；无数据从 2010-01-01 全量
        if meta['frequency'] == 'daily':
            cutoff = date.today() - timedelta(days=365)
            df = df[df['report_date'] >= cutoff]
        else:
            with SessionLocal() as db:
                max_date = (
                    db.query(func.max(_mdl.report_date))
                    .filter(_mdl.indicator_code == indicator_code)
                    .scalar()
                )
            if max_date is not None:
                df = df[df['report_date'] > max_date]
            else:
                df = df[df['report_date'] >= date(2010, 1, 1)]

        if df.empty:
            logger.info(f"->{indicator_code} 无需导入")
            return 0

        # 选择目标列并入库
        cols = ['indicator_code', 'indicator_name', 'category', 'country', 'report_date',
                'pub_date', 'value', 'value_prev', 'value_expected', 'unit', 'frequency']
        df = df[[c for c in cols if c in df.columns]]
        df = df.replace({np.nan: None})

        upsert_df_to_db(df, _mdl.__table__.name, _engine, _mdl.unique_keys)
        count = len(df)
        logger.info(f"->{indicator_code} 成功：{count}")
        return count
    except Exception as e:
        logger.error(f"->{indicator_code} 失败：{str(e)}")
        return -1


def _fetch_economic_indicator_from_fred(indicator_code, meta):
    """通过 FRED API 获取单个经济指标数据，返回标准化的 DataFrame。

    FRED API 统一返回 {observations: [{date, value}, ...]} 结构，
    无需 A/B/C 列模式分支。前值通过 shift(1) 计算，预期值置空。

    :return: DataFrame，列包含 report_date/value/value_prev/value_expected/pub_date
    """
    series_id = meta['fred_series_id']
    units = meta.get('fred_units', 'lin')
    frequency = meta['frequency']

    # 增量起点：日度取最近 365 天，月度/季度取 DB 最大日期+1 或 2010-01-01
    if frequency == 'daily':
        observation_start = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    else:
        with SessionLocal() as db:
            max_date = (
                db.query(func.max(EconomicIndicator.report_date))
                .filter(EconomicIndicator.indicator_code == indicator_code)
                .scalar()
            )
        if max_date is not None:
            observation_start = (max_date + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            observation_start = "2010-01-01"

    # 调用 FRED API（fetch_json_with_timeout 自带超时保护）
    api_key = settings.FRED_API_KEY
    if not api_key:
        raise RuntimeError("FRED_API_KEY 未配置，请在 .env 中设置 "
                           "（免费注册：https://fredaccount.stlouisfed.org/apikeys）")

    params = {
        'series_id': series_id,
        'api_key': api_key,
        'file_type': 'json',
        'observation_start': observation_start,
        'sort_order': 'asc',
        'limit': 100000,  # FRED 单次最大 100000 条，足够覆盖 16 年月度数据
    }
    if units and units != 'lin':
        # lin 为原始值，无需传 units 参数；pch/pc1/pca 为 FRED 内置转换
        params['units'] = units

    url = f"{settings.FRED_API_BASE}/series/observations"
    logger.info(f"->{indicator_code} 调用 FRED API: series_id={series_id}, "
                f"units={units}, start={observation_start}")

    data = fetch_json_with_timeout(url, params, timeout=30)
    observations = data.get('observations', [])
    if not observations:
        return pd.DataFrame()

    # 构建 DataFrame：FRED 返回的 date 为 "YYYY-MM-DD"，value 为字符串（"." 表示缺失）
    df = pd.DataFrame(observations)
    df = df.rename(columns={'date': 'report_date', 'value': 'value'})

    # FRED 用 "." 表示缺失值，转为 NaN
    df['value'] = df['value'].replace('.', np.nan)

    # 前值：通过 shift(1) 计算（FRED 不提供前值字段）
    df['value_prev'] = df['value'].shift(1)
    # 预期值：FRED 不提供，置空
    df['value_expected'] = None
    # pub_date：FRED 不提供发布日期，置空
    df['pub_date'] = None

    # 若使用 pch/pc1/pca 转换，第一条数据的 value_prev 为 NaN（无前序数据），属正常
    # 对于月度指标的增量场景，shift(1) 会取到上次同步的最后一期作为前值，
    # 但本次只取增量部分，shift(1) 在增量数据内部计算前值。
    # 完整前值需查询 DB 最后一期——此处简化处理，依赖 upsert 覆盖。
    return df


def _fetch_economic_indicator_from_akshare(indicator_code, meta):
    """akshare fallback 路径：保留原三种列模式（A/B/C）处理逻辑。

    当指标未配置 fred_series_id 时使用此路径。
    """
    import akshare as ak
    ak_func = getattr(ak, meta['akshare_func'])
    col_pattern = meta['col_pattern']

    # 根据 col_pattern 调用 akshare 函数获取 DataFrame
    if col_pattern == 'C':
        # 日度指标（bond_zh_us_rate）：获取最近 365 天数据
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        df = call_with_timeout(ak_func, timeout=30)(
            start_date=start_date, end_date=end_date)
    else:
        # 月度/季度指标：全量获取
        df = call_with_timeout(ak_func, timeout=30)()

    if df is None or df.empty:
        return None

    # 根据 col_pattern 清洗 DataFrame 列结构，统一为 ORM 字段名
    if col_pattern == 'A':
        # 模式A：['商品','日期','今值','预测值','前值']
        df = df.rename(columns={
            '日期': 'report_date', '今值': 'value',
            '预测值': 'value_expected', '前值': 'value_prev',
        })
        df['pub_date'] = None
    elif col_pattern == 'B':
        # 模式B：['时间','发布日期','现值','前值']
        df = df.rename(columns={
            '时间': 'report_date', '发布日期': 'pub_date',
            '现值': 'value', '前值': 'value_prev',
        })
        df['value_expected'] = None
    elif col_pattern == 'C':
        # 模式C：bond_zh_us_rate，提取 '日期' 列和 col_name 指定的列
        col_name = meta.get('col_name')
        df = df.rename(columns={
            '日期': 'report_date', col_name: 'value',
        })
        df['pub_date'] = None
        df['value_expected'] = None
        df['value_prev'] = None

    return df


def upsert_all_economic_indicators_sql():
    """遍历 Config.ECONOMIC_INDICATORS 全量同步所有经济指标。

    单指标失败不中断（try/except 记录 -1），
    返回 {indicator_code: count, ...} 结果字典。
    """
    steps = {}
    logger.info("全量同步美国宏观经济指标")
    for indicator_code in dbsCfg.ECONOMIC_INDICATORS:
        try:
            steps[indicator_code] = upsert_economic_indicator_sql(indicator_code)
        except Exception as e:
            logger.error(f"->{indicator_code} 失败：{str(e)}")
            steps[indicator_code] = -1
    return steps
