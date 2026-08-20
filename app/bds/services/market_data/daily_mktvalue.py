# -*- coding: utf-8 -*-
"""每日市值同步：gm 接口 stk_get_daily_mktvalue。

复用 daily_valuation 的"按 trade_date 增量 + 直接覆盖"模式。
与每日估值的差异：fields 集合不同（总市值/A股市值/B股市值/企业价值等），
但 API 签名一致（fields 为第二位置参数，无 rpt_type/data_type）。
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func
from gm.api import stk_get_daily_mktvalue

from server_fast.common.db import SessionLocal
from server_fast.app.bds.models import DailyMktvalue
# 复用 fund_data 的标量清洗工具，避免重复实现
from server_fast.app.bds.services.fund_data.fund_reports import _clean_scalar, _to_date
from server_fast.common.utils import call_with_timeout

logger = logging.getLogger("uvicorn.error")

# 市值指标字段（gm API fields 参数，10个，单批即可获取）
# 字段清单以 gm 接口实际返回为准（2026-08 经 SHSE.600900 探针校验）
DAILY_MKTVALUE_FIELDS = "tot_mv,tot_mv_csrc,a_mv,a_mv_ex_ltd,b_mv,b_mv_ex_ltd,ev,ev_ex_curr,ev_ebitda,equity_value"


def _fetch_daily_mktvalue_batched(symbol, start_date, end_date):
    """分批获取市值指标数据（gm API 限制 fields 不超过 20 个）。

    注意：stk_get_daily_mktvalue 的 API 签名与每日估值一致，
    fields 为第二个位置参数，无 rpt_type/data_type。
    将字段按 20 个一批分别请求，再以元数据列为 key 合并，
    返回包含全部字段的完整 DataFrame。
    """
    all_fields = DAILY_MKTVALUE_FIELDS.split(",")
    batch_size = 20
    dfs = []
    for i in range(0, len(all_fields), batch_size):
        batch_fields = ",".join(all_fields[i:i + batch_size])
        # fields 为第二位置参数，无 rpt_type/data_type
        df = call_with_timeout(stk_get_daily_mktvalue, timeout=30)(
            symbol,
            batch_fields,
            start_date=start_date,
            end_date=end_date,
            df=True,
        )
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]
    # 多批数据按元数据列合并（市值指标元数据仅 symbol + trade_date）
    merge_cols = ["symbol", "trade_date"]
    result = dfs[0]
    for df in dfs[1:]:
        result = result.merge(df, on=merge_cols, how="outer")
    return result


def upsert_daily_mktvalue_sql(symbols):
    """循环获取指定股票列表的市值指标数据并 upsert 入库。

    增量更新策略（基于 trade_date，非 rpt_date）：
    - 数据库已有该 symbol 数据：从最新 trade_date + 1 天开始获取
    - 数据库无该 symbol 数据：从 2010-01-01 全量获取

    去重规则（按 symbol + trade_date 直接覆盖）：
    - 每日市值无修正概念，无需 pub_date 最新保留逻辑
    - 已有记录：直接更新所有字段
    - 无已有记录：插入新记录

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的保存条数。
    """
    _mdl = DailyMktvalue
    _field_list = DAILY_MKTVALUE_FIELDS.split(",")
    steps = {}
    logger.info("市值指标数据获取并导入")
    end_date = datetime.now().strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            # 查询数据库中该 symbol 的最大 trade_date，用于增量更新
            with SessionLocal() as db:
                max_trade_date = (
                    db.query(func.max(_mdl.trade_date))
                    .filter(_mdl.symbol == symbol)
                    .scalar()
                )
            # 增量起点：有数据则从最新 trade_date + 1 天，否则从 2010-01-01 全量获取
            if max_trade_date is not None:
                start_date = (max_trade_date + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = "2010-01-01"
            # 调用 gm 接口获取市值指标（分批获取，带超时保护）
            df = _fetch_daily_mktvalue_batched(symbol, start_date, end_date)
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            saved_count = 0
            with SessionLocal() as db:
                for _, row in df.iterrows():
                    trade_date = _to_date(row.get("trade_date"))
                    if trade_date is None:
                        continue
                    # 查询是否已有该 (symbol, trade_date) 记录
                    existing = (
                        db.query(_mdl)
                        .filter(_mdl.symbol == symbol, _mdl.trade_date == trade_date)
                        .first()
                    )
                    if existing is not None:
                        # 已有记录：直接覆盖更新所有字段（每日市值无修正概念）
                        for f in _field_list:
                            setattr(existing, f, _clean_scalar(row.get(f)))
                    else:
                        # 无已有记录：插入新记录
                        obj = _mdl(symbol=symbol, trade_date=trade_date)
                        for f in _field_list:
                            setattr(obj, f, _clean_scalar(row.get(f)))
                        db.add(obj)
                    saved_count += 1
                    # 每 100 条 commit 一次
                    if saved_count % 100 == 0:
                        db.commit()
                db.commit()  # 提交剩余记录
            logger.info(f"->{symbol} 成功：{saved_count}")
            steps[symbol] = saved_count
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps
