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
        akshare_func = meta.get('akshare_func')

        if fred_series_id:
            # ===== FRED API 路径 =====
            df = _fetch_economic_indicator_from_fred(indicator_code, meta)
        elif akshare_func:
            # ===== akshare fallback 路径 =====
            df = _fetch_economic_indicator_from_akshare(indicator_code, meta)
        else:
            # 既无 FRED 也无 akshare 配置（如中国指标），仅通过 wscn 同步
            logger.info(f"->{indicator_code} 无 FRED/akshare 配置，请通过 wscn 同步")
            return 0

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
        cols = ['indicator_code', 'indicator_name', 'indicator_short_name', 'category', 'country', 'report_date',
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


# 华尔街见闻日历接口基础 URL
_WSCN_API_BASE = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"


def _fetch_wscn_batch(start_date: date, end_date: date) -> list:
    """拉取 wscn 日历接口单批数据（自然月跨度，≤31 天安全）。

    :param start_date: 批次起始日期（含）
    :param end_date: 批次结束日期（含）
    :return: items 列表，接口异常时返回空列表
    """
    import time

    # 系统时区 UTC+8 与 wscn 一致，直接用 mktime 转换
    start_ts = int(time.mktime(start_date.timetuple()))
    end_ts = int(time.mktime(end_date.timetuple())) + 86399  # 当天 23:59:59

    logger.info(f"->wscn 拉取 {start_date} ~ {end_date}")
    try:
        data = fetch_json_with_timeout(
            _WSCN_API_BASE,
            {"start": start_ts, "end": end_ts},
            timeout=30,
        )
        return data.get("data", {}).get("items", [])
    except Exception as e:
        logger.warning(f"->wscn 拉取失败 {start_date}~{end_date}：{str(e)}")
        return []


def upsert_economic_indicator_from_wscn_sql():
    """通过华尔街见闻日历接口同步经济指标数据并 upsert 入库。

    数据源：GET https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas
    - 无需鉴权，start/end 为 Unix 时间戳（秒），最大跨度 31 天
    - 接口不支持服务端过滤（country/wscn_ticker 参数均被忽略），需客户端过滤

    增量策略：以 pub_date 为基准，按指标独立计算已发布记录的 max(pub_date)+1 天为起点
    （无数据从 2015-01-01），全局起点取各指标起点最小值，按自然月分批调用 API，
    今日为终点。客户端按指标级 pub_date 过滤，避免已入库指标重复 upsert。
    未发布记录（value 为空）不参与 max 计算，确保下次拉取时能覆盖为已发布数据。

    合并存储：wscn 补充 FRED 缺失的 forecast/importance/revised/pub_date 字段，
    同 (indicator_code, report_date) 记录通过 upsert 覆盖更新。
    未发布数据（actual 为空）也入库，value 置空，下次拉取已发布时 upsert 覆盖。

    返回值：插入/更新条数（int），异常返回 -1。
    """
    _engine = settings.DB_ENGINE
    _mdl = EconomicIndicator
    wscn_map = dbsCfg.WSCN_INDICATOR_MAP  # wscn_ticker -> indicator_code

    logger.info("华尔街见闻日历数据源同步经济指标")
    try:
        # 1. 按指标独立计算 max(pub_date)，全局起点取各指标起点最小值
        #    避免跨指标 max 导致落后指标漏拉（如 A 已最新但 B 无数据时 B 不会被跳过）
        #    仅以已发布记录（value IS NOT NULL）为基准，未发布记录不参与 max 计算，
        #    确保未发布数据下次仍会被拉取并覆盖为已发布数据
        mapped_codes = list(set(wscn_map.values()))
        with SessionLocal() as db:
            rows_db = (
                db.query(_mdl.indicator_code, func.max(_mdl.pub_date))
                .filter(_mdl.indicator_code.in_(mapped_codes))
                .filter(_mdl.value.isnot(None))
                .group_by(_mdl.indicator_code)
                .all()
            )
        # 每个指标的 max(pub_date) 字典
        code_to_max_pub: dict = {code: max_pub for code, max_pub in rows_db if max_pub is not None}

        # 各指标起点 = max(pub_date)+1 或 2015-01-01，全局起点取最小值
        code_starts: dict = {}
        for code in mapped_codes:
            max_pub = code_to_max_pub.get(code)
            code_starts[code] = (max_pub + timedelta(days=1)) if max_pub else date(2015, 1, 1)
        start_date = min(code_starts.values()) if code_starts else date(2015, 1, 1)

        end_date = date.today()
        if start_date > end_date:
            logger.info("->wscn 无需导入（已是最新）")
            return 0

        logger.info(f"->wscn 增量范围：{start_date} ~ {end_date}")
        logger.info(f"->wscn 各指标起点：{code_starts}")

        # 2. 按自然月分批拉取（API 最大跨度 31 天，自然月 28-31 天均安全）
        all_items: list = []
        current = start_date
        while current <= end_date:
            # 计算当月最后一天
            if current.month == 12:
                next_first = date(current.year + 1, 1, 1)
            else:
                next_first = date(current.year, current.month + 1, 1)
            batch_end = min(next_first - timedelta(days=1), end_date)

            all_items.extend(_fetch_wscn_batch(current, batch_end))
            current = batch_end + timedelta(days=1)

        if not all_items:
            logger.info("->wscn 无需导入")
            return 0

        # 3. 客户端过滤 + 字段映射
        # 过滤条件：wscn_ticker 在映射表中 + calendar_type==FD
        #          + pub_date > 该指标已入库 max(pub_date)（按指标独立增量，避免重复 upsert）
        # 注意：actual 为空的未发布数据也入库（value 置 None），下次拉取已发布时 upsert 覆盖
        rows = []
        for item in all_items:
            wscn_ticker = item.get("wscn_ticker", "")
            if wscn_ticker not in wscn_map:
                continue
            if item.get("calendar_type") != "FD":
                continue

            indicator_code = wscn_map[wscn_ticker]
            meta = dbsCfg.ECONOMIC_INDICATORS.get(indicator_code, {})

            # pub_date: Unix 时间戳 -> date
            pub_ts = item.get("public_date")
            pub_date = None
            if pub_ts:
                try:
                    pub_date = datetime.fromtimestamp(int(pub_ts)).date()
                except (ValueError, TypeError):
                    pass

            # 按指标独立增量：跳过 pub_date <= 该指标已入库 max(pub_date) 的旧记录
            # max(pub_date) 仅统计已发布记录（value IS NOT NULL），
            # 因此未发布记录不会被跳过，确保下次拉取时能覆盖为已发布数据
            max_pub_for_code = code_to_max_pub.get(indicator_code)
            if max_pub_for_code is not None:
                if pub_date is None or pub_date <= max_pub_for_code:
                    continue

            rows.append({
                "indicator_code": indicator_code,
                "indicator_name": meta.get("name", ""),
                "indicator_short_name": meta.get("short_name", ""),
                "category": meta.get("category", ""),
                "country": meta.get("country", "美国"),
                "report_date": item.get("observation_date", ""),
                "pub_date": pub_date,
                "value": item.get("actual", ""),
                "value_prev": item.get("previous", ""),
                "value_expected": item.get("forecast", ""),
                "importance": item.get("importance"),
                "revised": item.get("revised", ""),
                "title": item.get("title", ""),
                "foresight": item.get("foresight", ""),
                "unit": meta.get("unit"),
                "frequency": meta.get("frequency"),
            })

        if not rows:
            logger.info("->wscn 无需导入（过滤后无数据）")
            return 0

        # 4. DataFrame 清洗
        df = pd.DataFrame(rows)
        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce").dt.date
        # 数值列：空字符串/非数字 -> NaN
        for col in ["value", "value_prev", "value_expected", "revised"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["importance"] = pd.to_numeric(df["importance"], errors="coerce")

        # 过滤无效行：仅要求 report_date 有效，value 允许为空（未发布数据）
        df = df[df["report_date"].notna()]
        if df.empty:
            logger.info("->wscn 无需导入（清洗后无数据）")
            return 0

        # 5. 去重：同 (indicator_code, report_date) 保留 pub_date 最新的一条
        df = df.sort_values("pub_date", ascending=False, na_position="last").drop_duplicates(
            subset=["indicator_code", "report_date"], keep="first"
        )

        # 6. 入库
        cols = ['indicator_code', 'indicator_name', 'indicator_short_name', 'category', 'country', 'report_date',
                'pub_date', 'value', 'value_prev', 'value_expected', 'importance', 'revised',
                'title', 'foresight', 'unit', 'frequency']
        df = df[cols]
        df = df.replace({np.nan: None})

        upsert_df_to_db(df, _mdl.__table__.name, _engine, _mdl.unique_keys)
        count = len(df)
        logger.info(f"->wscn 成功：{count}")
        return count
    except Exception as e:
        logger.error(f"->wscn 失败：{str(e)}")
        return -1
