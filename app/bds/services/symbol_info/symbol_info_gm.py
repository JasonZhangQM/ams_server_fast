# -*- coding: utf-8 -*-
"""证券信息同步（gm 通道）：get_instruments 全量 → 剔 B 股 → upsert 到 bds_symbol_info。

与 symbol_info.py（东财 Excel 通道）共用同一张表与 market/board 推导函数，
两者由 sync_symbol_info_all() 串行编排（前端一个「同步证券信息」按钮）。

⚠️ 五个必须记住的点（实测，2026-10-16）：
1. 在市股的 delisted_date = 2038-01-01 是【占位值】（合约到期日语义），落库必须转 NULL；
2. delisted_date 可能是【未来日期】（已公告未退市，如信达证券 601059 = 2026-10-19），
   故「已退市」判据是 delisted_date <= CURDATE()，不是 IS NOT NULL；
3. 本表只落静态身份列；pre_close / 涨跌停 / adj_factor / price / rate 是易变行情值，不落本表；
4. name / online_date 是【回填现有列】，必须走 keep_columns（只补空），
   否则会与东财 Excel 通道互相覆盖、名称随执行顺序漂移；
5. market / board 由 symbol 推导（两条通道都写）；gm 原生 board 是数字（0/1/3/4），
   必须在裁列前用推导值覆盖掉，否则数字会写进 board 列。
"""
import logging
from datetime import date

import pandas as pd
from gm.api import get_instruments

from server_fast.config import settings
from server_fast.common.utils import filter_in_cols, upsert_df_to_db
from server_fast.app.bds.models import SymbolInfo
from server_fast.app.bds.services.symbol_info.market_board import derive_market_board

logger = logging.getLogger("uvicorn.error")

# gm 对「在市股」返回的 delisted_date 占位值（语义 = 合约到期日）
GM_LIVE_PLACEHOLDER = date(2038, 1, 1)
# B 股前缀：沪市 900xxx / 深市 200xxx（实测共 115 只；与 gm 原生 board==0 完全重合）
B_SHARE_PREFIXES = ("SHSE.9", "SZSE.2")
# 落库列：静态身份列 + 推导列
GM_COLS = ["symbol", "name", "sec_abbr", "market", "board", "online_date", "delisted_date"]
# 回填列：只补空、不覆盖（已有值优先）。market/board 不在其中 —— 它们值确定，可无条件写。
GM_KEEP_COLUMNS = ["name", "online_date"]
# gm 原生 board 数字编码 -> 中文（仅用于交叉校验，不落库）
GM_BOARD_CN = {0: "B股", 1: "主板", 3: "创业板", 4: "科创板"}


def upsert_symbol_info_gm_sql() -> int:
    """gm 全量证券信息 → bds_symbol_info（一次调用覆盖全市场，含退市股）。

    返回值：受影响行数（act_sql_engine 的 rowcount），供前端同步反馈展示。
    """
    logger.info("证券信息导入(gm)")
    _mdl = SymbolInfo
    raw = get_instruments(
        exchanges=["SHSE", "SZSE", "BJSE"], sec_types=[1],
        skip_suspended=False, skip_st=False, df=True,
    )
    # 1) 剔 B 股（沪 900xxx + 深 200xxx；实测 115 只，与原生 board==0 完全重合）
    df = raw[~raw["symbol"].str.startswith(B_SHARE_PREFIXES)].copy()
    # 2) 列名映射：本通道专用一份
    df = df.rename(columns=_mdl.map_fields(_mdl.cols_map_fields_gm))
    # 3) market / board 推导 —— 必须在裁列前覆盖，避免 gm 的数字 board 混入
    derived = df["symbol"].map(derive_market_board)
    df["market"] = [d[0] for d in derived]
    df["board"] = [d[1] for d in derived]
    # 3b) 交叉校验（仅告警，不阻断）
    _check_board_consistency(df, raw)
    # 4) 裁列
    df = df[filter_in_cols(df.columns, GM_COLS)].copy()
    # 5) 日期：gm 返回 tz-aware Timestamp，而 to_dtype() 不处理 Date 列 ⇒ 显式转 date；
    #    同时把在市股的占位值 2038-01-01 转 NULL（否则「已退市」判据失效）
    for col in ("online_date", "delisted_date"):
        ser = pd.to_datetime(df[col], errors="coerce").dt.date
        df[col] = ser.where(ser.notna(), None)
    df["delisted_date"] = df["delisted_date"].where(
        df["delisted_date"] != GM_LIVE_PLACEHOLDER, None
    )
    # 6) upsert：唯一键 symbol；name / online_date 只补空，其余列无条件写
    result = upsert_df_to_db(
        df, _mdl.__table__.name, settings.DB_ENGINE, _mdl.unique_keys,
        keep_columns=GM_KEEP_COLUMNS,
    )
    logger.info(f"->成功：{result}（行数 {len(df)}）")
    return result


def _check_board_consistency(df: pd.DataFrame, raw: pd.DataFrame) -> None:
    """推导 board 与 gm 原生 board 交叉校验；不一致只告警、不阻断（实测 0 行）。"""
    gm_board = raw.loc[df.index, "board"].map(GM_BOARD_CN)
    mismatch = df[(df["board"] != gm_board) & gm_board.notna()]
    if len(mismatch):
        logger.warning(
            f"⚠️ board 推导与 gm 原生不一致 {len(mismatch)} 行："
            f"{mismatch[['symbol', 'board']].head(5).to_dict('records')}"
        )
