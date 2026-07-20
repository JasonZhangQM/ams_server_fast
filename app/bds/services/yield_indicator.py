# -*- coding: utf-8 -*-
"""bds 收益率指标业务函数。

独立于 economic_indicator.py，仅通过 FRED API 同步 4 个日频收益率指标
（YIELD_2Y/YIELD_10Y/YIELD_SPREAD_2Y10Y/YIELD_TIPS_10Y）到 bds_yield_indicator 表。

与 economic_indicator.py 的差异：
- 数据源仅 FRED API `/series/observations`，无 akshare/wscn 回退
- 仅保留 FRED observation 中的 date/value 两个字段（realtime_start/realtime_end 已废弃）
- 不计算 value_prev、不设 value_expected/pub_date 为 null（这些 wscn 专用字段新表不含）
- 增量策略：DB 有数据时从 max(report_date)+1 起拉，无数据从 2010-01-01 起拉
"""
import logging

import numpy as np
import pandas as pd
from datetime import date, timedelta
from sqlalchemy import func

from server_fast.config import settings
from server_fast.common.utils import *  # noqa: F401,F403（upsert_df_to_db / call_with_timeout / fetch_json_with_timeout 等）
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import YieldIndicator

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 的 logger


def _ensure_table():
    """幂等创建 bds_yield_indicator 表（checkfirst=True，已存在则跳过）。

    项目硬约束：不修改任何已有表，仅创建新表。
    在首次同步时调用，避免 import 时触发 DDL。
    """
    try:
        YieldIndicator.__table__.create(settings.DB_ENGINE, checkfirst=True)
    except Exception as e:
        # 表已存在或创建失败均不阻断同步流程，upsert_df_to_db 会进一步抛错
        logger.warning(f"创建 bds_yield_indicator 表失败（可忽略已存在情况）：{e}")


def _fetch_yield_from_fred(indicator_code, meta):
    """通过 FRED API 获取单个收益率指标数据，返回标准化的 DataFrame。

    FRED API `/series/observations` 每个 observation 含 4 个字段：
    - date: 报告日期（YYYY-MM-DD 字符串）
    - value: 数值（字符串，"." 表示缺失）
    - realtime_start/realtime_end: FRED 实时数据范围（已废弃，不入库）

    增量策略：
    - DB 有数据时从 max(report_date)+1 起拉
    - 无数据时从 2010-01-01 起拉

    :return: DataFrame，列包含 report_date/value
    """
    series_id = meta['fred_series_id']
    units = meta.get('fred_units', 'lin')

    # 增量起点：DB 已有数据则从 max(report_date)+1 起拉，否则从 2010-01-01 起拉
    with SessionLocal() as db:
        max_date = (
            db.query(func.max(YieldIndicator.report_date))
            .filter(YieldIndicator.indicator_code == indicator_code)
            .scalar()
        )
    if max_date is not None:
        observation_start = (max_date + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        observation_start = "2010-01-01"

    # 调用 FRED API（fetch_json_with_timeout 自带超时保护与重试）
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
        'limit': 100000,  # FRED 单次最大 100000 条，足够覆盖 16 年日度数据
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

    # 构建 DataFrame，仅保留 FRED observation 的 date/value 字段
    df = pd.DataFrame(observations)

    # 字段映射：date -> report_date；realtime_start/realtime_end 已废弃，不入库
    df = df.rename(columns={'date': 'report_date'})

    # FRED 用 "." 表示缺失值，转为 NaN
    df['value'] = df['value'].replace('.', np.nan)

    return df


def upsert_yield_indicator_sql(indicator_code):
    """同步单个收益率指标数据并 upsert 入库。

    流程：
    1. 从 Config.YIELD_INDICATORS 获取元信息，不存在则 warning 返回 -1
    2. 调用 _fetch_yield_from_fred 获取数据（增量策略）
    3. 添加元信息列（indicator_code/indicator_name/indicator_short_name/category/country/unit/frequency）
    4. 使用 upsert_df_to_db 入库

    返回值：插入/更新条数（int），异常返回 -1。
    """
    _engine = settings.DB_ENGINE
    _mdl = YieldIndicator

    # 获取指标元信息
    meta = dbsCfg.YIELD_INDICATORS.get(indicator_code)
    if meta is None:
        logger.warning(f"未知收益率指标代码：{indicator_code}")
        return -1

    logger.info(f"收益率指标 {indicator_code}（{meta['name']}）获取并导入")
    try:
        # 首次同步时确保表存在（幂等）
        _ensure_table()

        df = _fetch_yield_from_fred(indicator_code, meta)
        if df is None or df.empty:
            logger.info(f"->{indicator_code} 无需导入")
            return 0

        # 添加元信息列
        df['indicator_code'] = indicator_code
        df['indicator_name'] = meta['name']
        df['indicator_short_name'] = meta['short_name']
        df['category'] = meta['category']
        df['country'] = meta['country']
        df['unit'] = meta['unit']
        df['frequency'] = meta['frequency']

        # 日期列转换（FRED 返回 YYYY-MM-DD 字符串 -> date）
        df['report_date'] = pd.to_datetime(df['report_date'], errors='coerce').dt.date

        # 数值列转换（非数值转为 NaN 后过滤）
        df['value'] = pd.to_numeric(df['value'], errors='coerce')

        # 过滤掉 report_date 或 value 为 NaN 的行
        df = df[df['report_date'].notna() & df['value'].notna()]
        if df.empty:
            logger.info(f"->{indicator_code} 无需导入")
            return 0

        # 选择目标列并入库
        cols = ['indicator_code', 'indicator_name', 'indicator_short_name', 'category', 'country',
                'report_date', 'value', 'unit', 'frequency']
        df = df[cols]
        df = df.replace({np.nan: None})

        upsert_df_to_db(df, _mdl.__table__.name, _engine, _mdl.unique_keys)
        count = len(df)
        logger.info(f"->{indicator_code} 成功：{count}")
        return count
    except Exception as e:
        logger.error(f"->{indicator_code} 失败：{str(e)}")
        return -1


def upsert_all_yield_indicators_sql():
    """遍历 Config.YIELD_INDICATORS 全量同步所有收益率指标。

    单指标失败不中断（try/except 记录 -1），
    返回 {indicator_code: count, ...} 结果字典。
    """
    steps = {}
    logger.info("全量同步美债收益率指标")
    for indicator_code in dbsCfg.YIELD_INDICATORS:
        try:
            steps[indicator_code] = upsert_yield_indicator_sql(indicator_code)
        except Exception as e:
            logger.error(f"->{indicator_code} 失败：{str(e)}")
            steps[indicator_code] = -1
    return steps
