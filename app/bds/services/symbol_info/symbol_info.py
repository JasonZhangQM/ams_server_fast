# -*- coding: utf-8 -*-
"""证券信息同步（em 通道）：代码信息文件夹内的 Excel 导入证券基本信息。

与 symbol_info_gm.py（gm 通道）共用同一张表与 market/board 推导函数，
两者由 sync_symbol_info_all() 串行编排（前端一个「同步证券信息」按钮）。
"""
import logging

import numpy as np
import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import *
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import SymbolInfo
from server_fast.app.bds.services.symbol_info.market_board import derive_market_board
from server_fast.app.bds.services.symbol_info.symbol_info_gm import upsert_symbol_info_gm_sql

logger = logging.getLogger("uvicorn.error")


def _normalize_symbol(code: str, map_market_code: dict) -> str:
    """代码归一化：已带市场前缀（含 '.'）或前两位不在映射表者原样返回，否则补前缀。

    例：'601398' → 'SHSE.601398'；'000001' → 'SZSE.000001'；'688981' → 'SHSE.688981'；
        '920001' → 'BJSE.920001'；
        'SHSE.000001' / 'SPX' / 'NDX100' / 'GC00Y' → 原样保留
        （「手动添加」表的代码列本身就是完整 symbol）。

    ⚠️ 按【前两位】匹配，不是首字符：科创板 688xxx 与沪市主板 600xxx 首字符同为 6、
       深市 00xxxx 与沪市 60xxxx 只有前两位能区分，北交所 920xxx 同理。
    ⚠️ 映射表刻意不收沪市 B 股 900xxx；若日后东财导出含 B 股，需在此显式分流（方案 §4-5）。
    """
    code = str(code).strip()
    if "." in code:
        return code
    prefix = map_market_code.get(code[:2])
    if prefix is None:
        return code
    return f"{prefix}.{code}"


def upsert_symbol_info_excel_sql() -> int:
    """导入证券基本信息（代码信息文件夹内全部 Excel，全量 upsert）。

    返回值：累计受影响行数（act_sql_engine 的 rowcount 之和），供前端同步反馈展示。
    """
    _engine = settings.DB_ENGINE
    _mdl = SymbolInfo
    _folder = dbsCfg.FOLDER_SYMBOL
    _map_market_code = dbsCfg.MAP_MARKET_CODE
    _fields_replace = _mdl.fields_replace
    # 本通道专用列名映射（与 gm 通道各持一份，见 models.SymbolInfo）
    _cols_map = _mdl.map_fields(_mdl.cols_map_fields_em)

    logger.info("证券基本信息导入")
    file_names = [
        f.name for f in _folder.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
        and (not f.name.startswith('~'))
    ]
    total = 0
    for file_name in file_names:
        df = pd.read_excel(_folder / file_name, dtype=str)
        df = df.map(lambda x: x.strip() if isinstance(x, str) else x)
        df.columns = df.columns.str.strip()
        df = df.rename(columns=_cols_map)
        df = df[filter_in_cols(df.columns, _mdl.db_fields())]
        df['symbol'] = df['symbol'].map(lambda x: _normalize_symbol(x, _map_market_code))
        # '—' 替换必须按【列交集】进行：filter_in_cols 返回交集，而 fields_replace 是固定
        # 字段清单 —— 直接切片会让列不全的 Excel（如「手动添加」仅 5 列）抛 KeyError（方案 §4-8）。
        _replace_cols = filter_in_cols(df.columns, _fields_replace)
        df[_replace_cols] = df[_replace_cols].replace('—', np.nan)
        df = df.astype(filter_dtypes(df.columns, _mdl.to_dtype()))
        # 市场 / 板块：与 gm 通道共用同一纯函数，值确定 ⇒ 两通道谁先谁后都一样。
        # 放在 astype 之后：避免 astype(str) 把 None 变成字符串 'nan'。
        derived = df['symbol'].map(derive_market_board)
        df['market'] = [d[0] for d in derived]
        df['board'] = [d[1] for d in derived]
        if not df.empty:
            df = df.replace({np.nan: None})
            result = upsert_df_to_db(
                df, _mdl.__table__.name, _engine, _mdl.unique_keys
            )
            total += result or 0
            logger.info(f"->成功：{result}（{file_name}，行数 {len(df)}）")
        else:
            logger.info(f"->无需导入：{file_name}")
    return total


def sync_symbol_info_all() -> dict:
    """证券信息全量同步：先东财 Excel（建骨架）后 gm（补退市行 + 空值回填）。

    两条通道在【新列上不重叠】、在 name/online_date 上【gm 只补空】、
    在 market/board 上【值必然相同】⇒ 顺序在逻辑上无关，先 em 只是让日志更易读。
    任一通道失败即抛错（已成功通道的写入仍生效，端点幂等、可直接重跑）。

    返回 steps 数组，前端 executeSync 会拼成
    「同步成功：东财Excel:更新N条; gm证券信息:更新M条」。
    """
    steps = []
    try:
        steps.append({"step": "东财Excel", "result": upsert_symbol_info_excel_sql()})
    except Exception as e:
        raise RuntimeError(f"东财 Excel 导入失败：{e}") from e
    try:
        steps.append({"step": "gm证券信息", "result": upsert_symbol_info_gm_sql()})
    except Exception as e:
        raise RuntimeError(f"gm 导入失败（东财 Excel 已成功）：{e}") from e
    return {"status": "success", "message": "证券信息同步完成", "steps": steps}
