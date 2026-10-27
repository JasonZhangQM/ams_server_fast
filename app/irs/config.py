# -*- coding: utf-8 -*-
"""irs 应用配置。

- SYMBOL_CON_LIST：贴水连续合约代码列表
- SYMBOL_CON_ZL：主力连续合约代码前缀

⚠️ 期权品种台账（品种属性 / 到期规则）**正源在 `bds.config.Config.OPTION_VARIETIES`**，
本类不再保留任何副本或别名（2026-10-27 合并 `OPTIONS_MARCH` + `MAP_OPTION_LEDGER` 后，
别名已无意义）。调用方一律 `from ...bds.config import Config as BdsCfg` 再用
`BdsCfg.option_varieties()` / `option_meta()`。依赖方向固定为 irs → bds。
"""


class Config:
    """irs 应用配置类。"""

    def __init__(self):
        pass

    # 贴水配置：连续合约代码列表
    SYMBOL_CON_LIST = {
        'CFFEX.IC00':{'symbol_type':'中证500', 'con_name':'当月'}, 
        'CFFEX.IC01':{'symbol_type':'中证500', 'con_name':'次月'}, 
        'CFFEX.IC02':{'symbol_type':'中证500', 'con_name':'当季'}, 
        'CFFEX.IC03':{'symbol_type':'中证500', 'con_name':'隔季'}, 
        'CFFEX.IF00':{'symbol_type':'沪深300', 'con_name':'当月'}, 
        'CFFEX.IF01':{'symbol_type':'沪深300', 'con_name':'次月'}, 
        'CFFEX.IF02':{'symbol_type':'沪深300', 'con_name':'当季'}, 
        'CFFEX.IF03':{'symbol_type':'沪深300', 'con_name':'隔季'}, 
        'CFFEX.IH':{'symbol_type':'上证50', 'con_name':'主连'}, 
        'CFFEX.IM':{'symbol_type':'中证1000', 'con_name':'主连'}, 
    }
        
    # 主力连续合约代码前缀
    SYMBOL_CON_ZL = [
        'CFFEX.IC', 'CFFEX.IF', 'CFFEX.IH', 'CFFEX.IM',
    ]
