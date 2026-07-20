# -*- coding: utf-8 -*-
"""bds 黄金储备业务函数。

包含 IMF SDMX JSON API 数据获取与 upsert 入库逻辑：
- 通过 IFS 数据集 RAXG_USD 指标获取各国央行月度黄金储备（USD 计价）
- 单国家增量同步 / 全量同步
"""
import calendar
import logging

import numpy as np
import pandas as pd
from datetime import date, timedelta
from sqlalchemy import func

from server_fast.config import settings
from server_fast.common.utils import *  # noqa: F401,F403
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import GoldReserve

logger = logging.getLogger("uvicorn.error")

# IMF SDMX JSON API 基础 URL（IFS 数据集，频率 M=月度，指标 RAXG_USD=黄金储备 USD）
_IMF_SDMX_API_BASE = "https://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData/IFS"


def _fetch_gold_reserve_from_imf(imf_code, start_period, end_period):
    """通过 IMF SDMX JSON API 获取单国黄金储备数据，返回标准化的 DataFrame。

    接口：GET {BASE}/M.{imf_code}.RAXG_USD?startPeriod={YYYY-MM}&endPeriod={YYYY-MM}

    SDMX-JSON 响应结构：
    - structure.dimensions.observation[0].values: 时间索引列表 [{'id': 'YYYY-MM'}, ...]
    - dataSets[0].series 第一个 series 的 observations: {'0': [OBS_VALUE, ...], ...}

    rpt_date 转换：YYYY-MM -> 月末日日期（通过 calendar.monthrange 取月末）

    :return: DataFrame，列包含 rpt_date, gold_holdings_usd；接口异常/无数据返回空 DataFrame
    """
    url = f"{_IMF_SDMX_API_BASE}/M.{imf_code}.RAXG_USD"
    params = {"startPeriod": start_period, "endPeriod": end_period}
    logger.info(f"->gold[{imf_code}] 调用 IMF SDMX API: {start_period} ~ {end_period}")

    data = fetch_json_with_timeout(url, params, timeout=30)

    # 解析 SDMX-JSON 结构
    data_sets = data.get("dataSets", [])
    if not data_sets:
        return pd.DataFrame()

    series_dict = data_sets[0].get("series", {})
    if not series_dict:
        return pd.DataFrame()

    # 取第一个 series（键形如 "M.US.RAXG_USD"）
    first_series = next(iter(series_dict.values()))
    observations = first_series.get("observations", {})
    if not observations:
        return pd.DataFrame()

    # 时间索引列表：structure.dimensions.observation[0].values
    time_values = data["structure"]["dimensions"]["observation"][0]["values"]

    rows = []
    for idx, time_info in enumerate(time_values):
        period = time_info.get("id")  # 'YYYY-MM'
        obs = observations.get(str(idx))
        if not period or not obs:
            continue
        obs_value = obs[0] if len(obs) > 0 else None
        # 跳过缺失值（None 或空字符串）
        if obs_value is None or obs_value == "":
            continue
        # YYYY-MM -> 月末日
        year, month = map(int, period.split("-"))
        last_day = calendar.monthrange(year, month)[1]
        rpt_date = date(year, month, last_day)
        rows.append({"rpt_date": rpt_date, "gold_holdings_usd": obs_value})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["gold_holdings_usd"] = pd.to_numeric(df["gold_holdings_usd"], errors="coerce")
    return df


def upsert_gold_reserve_sql(country_code):
    """同步单个国家央行黄金储备数据并 upsert 入库。

    增量策略：
    - 查询 DB 最大 rpt_date，加 1 天得到下月首日，取 YYYY-MM 作为 startPeriod
    - 无数据时从 2010-01 起
    - 终点为当前月

    返回值：插入/更新条数（int），异常返回 -1。
    """
    _engine = settings.DB_ENGINE
    _mdl = GoldReserve

    meta = dbsCfg.GOLD_RESERVE_COUNTRIES.get(country_code)
    if meta is None:
        logger.warning(f"未知国家代码：{country_code}")
        return -1

    imf_code = meta["imf_code"]
    country_name = meta["country_name"]

    logger.info(f"黄金储备 {country_code}（{country_name}）获取并导入")
    try:
        # 增量起点：max(rpt_date) 为月末，加 1 天得到下月首日
        with SessionLocal() as db:
            max_date = (
                db.query(func.max(_mdl.rpt_date))
                .filter(_mdl.country_code == country_code)
                .scalar()
            )
        if max_date is not None:
            start_period = (max_date + timedelta(days=1)).strftime("%Y-%m")
        else:
            start_period = "2010-01"

        end_period = date.today().strftime("%Y-%m")

        df = _fetch_gold_reserve_from_imf(imf_code, start_period, end_period)
        if df is None or df.empty:
            logger.info(f"->gold[{country_code}] 无需导入")
            return 0

        # 添加元信息列
        df["country_code"] = country_code
        df["country_name"] = country_name
        df["unit"] = "百万美元"
        df["frequency"] = "M"

        # 过滤无效行
        df = df[df["rpt_date"].notna() & df["gold_holdings_usd"].notna()]
        if df.empty:
            logger.info(f"->gold[{country_code}] 无需导入")
            return 0

        # 选择目标列并入库
        cols = ["country_code", "country_name", "rpt_date", "gold_holdings_usd", "unit", "frequency"]
        df = df[cols]
        df = df.replace({np.nan: None})

        upsert_df_to_db(df, _mdl.__table__.name, _engine, _mdl.unique_keys)
        count = len(df)
        logger.info(f"->gold[{country_code}] 成功：{count}")
        return count
    except Exception as e:
        logger.error(f"->gold[{country_code}] 失败：{str(e)}")
        return -1


def upsert_all_gold_reserves_sql():
    """遍历 Config.GOLD_RESERVE_COUNTRIES 全量同步所有国家黄金储备。

    单国家失败不中断（try/except 记录 -1），
    返回 {country_code: count, ...} 结果字典。
    """
    steps = {}
    logger.info("全量同步各国央行黄金储备")
    for country_code in dbsCfg.GOLD_RESERVE_COUNTRIES:
        try:
            steps[country_code] = upsert_gold_reserve_sql(country_code)
        except Exception as e:
            logger.error(f"->gold[{country_code}] 失败：{str(e)}")
            steps[country_code] = -1
    return steps
