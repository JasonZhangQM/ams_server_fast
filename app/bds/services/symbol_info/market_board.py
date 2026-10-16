# -*- coding: utf-8 -*-
"""市场 / 板块推导：symbol 前缀 → (市场, 板块)。

本模块是 bds_symbol_info.market / board 两列的【唯一事实源】。
两列由 symbol 前缀唯一决定（纯函数，不随时间变化、不依赖任何外部列），
em（东财 Excel）与 gm（get_instruments）两条通道都在落库前调用本函数；
因结果与通道无关，两通道写出的值必然相同 ⇒ 无需 keep_columns 护栏。

实测校验（2026-10-16）：
- gm 剔 B 股后的 5,564 行：本函数结果与 gm 原生 board 字段
  （0=B股 / 1=主板 / 3=创业板 / 4=科创板）【零不一致】、推导为 NULL 0 行；
- em 导出 5,581 行：推导分布（沪主板 1,702 / 沪科创 618 / 深主板 1,496 /
  深创业 1,412 / 北交所 353）与「改动前库内市股口径」逐项吻合。
"""
from typing import Optional

# (交易所, 代码前 3 位) -> (市场, 板块)
#
# 采用【白名单】而非「按交易所推市场」：上证指数 SHSE.000001 形如 SHSE.xxxxxx，
# 但 ("SHSE", "000") 不在表内 ⇒ 返回 (None, None)；宽松写法会把它误标成「上交所/主板」。
BOARD_PREFIX_MAP: dict[tuple[str, str], tuple[str, str]] = {
    # 上交所：主板 600/601/603/605，科创板 688/689
    ("SHSE", "600"): ("上交所", "主板"),
    ("SHSE", "601"): ("上交所", "主板"),
    ("SHSE", "603"): ("上交所", "主板"),
    ("SHSE", "605"): ("上交所", "主板"),
    ("SHSE", "688"): ("上交所", "科创板"),
    ("SHSE", "689"): ("上交所", "科创板"),
    # 深交所：主板 000/001/002/003，创业板 300/301/302
    ("SZSE", "000"): ("深交所", "主板"),
    ("SZSE", "001"): ("深交所", "主板"),
    ("SZSE", "002"): ("深交所", "主板"),
    ("SZSE", "003"): ("深交所", "主板"),
    ("SZSE", "300"): ("深交所", "创业板"),
    ("SZSE", "301"): ("深交所", "创业板"),
    ("SZSE", "302"): ("深交所", "创业板"),
}

# 北交所：不设主板/创业板/科创板，市场与板块同填「北交所」（用户指定）
BJSE_MARKET_BOARD = ("北交所", "北交所")


def derive_market_board(symbol: str) -> tuple[Optional[str], Optional[str]]:
    """symbol → (市场, 板块)；非 A 股（指数/外盘/期货/未知前缀）返回 (None, None)。

    例：'SHSE.600001'→('上交所','主板')；'SZSE.300750'→('深交所','创业板')；
        'SHSE.688981'→('上交所','科创板')；'BJSE.920001'→('北交所','北交所')；
        'SHSE.000001'(上证指数) / 'SZSE.399006'(创业板指) / 'SPX' / 'GC00Y'→(None, None)。
    """
    if not isinstance(symbol, str) or "." not in symbol:
        return (None, None)
    exchange, code = symbol.split(".", 1)
    if exchange == "BJSE":
        return BJSE_MARKET_BOARD
    return BOARD_PREFIX_MAP.get((exchange, code[:3]), (None, None))
