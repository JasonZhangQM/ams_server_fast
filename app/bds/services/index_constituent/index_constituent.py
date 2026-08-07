# -*- coding: utf-8 -*-
"""指数成分股同步：gm 接口获取指数成分股并入库（含变更检测）。"""
import logging
from datetime import date

import pandas as pd
from gm.api import stk_get_index_constituents

from server_fast.config import settings
from server_fast.common.utils import call_with_timeout
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import IndexConstituent

logger = logging.getLogger("uvicorn.error")


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
    steps = {}
    logger.info("指数成分股获取并导入")

    def _normalize_weight(w):
        """权重归一化：NaN/None 统一转 0.0，其余保留 4 位小数。"""
        if w is None or pd.isna(w):
            return 0.0
        return round(float(w), 4)

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
            if df is None or df.empty:
                logger.info(f"->{index_code} API 返回空数据，跳过")
                steps[index_code] = 0
                continue

            df = df[['index', 'symbol', 'weight', 'trade_date']]
            current_set = set(zip(
                df['symbol'],
                df['weight'].apply(_normalize_weight),
            ))
            if current_set == last_saved_set:
                logger.info(f"->{index_code} 成分股未变化，跳过")
                steps[index_code] = 0
                continue

            df = df.rename(columns={'index': 'index_code'})
            df['trade_date'] = pd.to_datetime(
                df['trade_date'], format='%Y-%m-%d'
            ).dt.date
            df['weight'] = df['weight'].apply(_normalize_weight)
            df.to_sql(_mdl.__table__.name, _engine, if_exists='append', index=False)

            logger.info(f"->{index_code} 成功保存")
            steps[index_code] = 1
        except Exception as e:
            logger.error(f"->{index_code} 失败：{str(e)}")
            steps[index_code] = -1
    return steps
