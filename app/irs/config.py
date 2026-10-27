# -*- coding: utf-8 -*-
"""irs 应用配置。

- SYMBOL_CON_LIST：贴水连续合约代码列表
- SYMBOL_CON_ZL：主力连续合约代码前缀

⚠️ 期权品种基础数据（品种表 / 到期规则 / 标的市场映射）**已迁至 `bds.config.Config`**，
本类仅保留别名引用（见 `Config` 尾部注释）。依赖方向固定为 irs → bds。
"""
from server_fast.app.bds.config import Config as BdsCfg


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

    # ==========================================================================
    # 期权品种基础数据：2026-10-27 起**正源归 bds**，本类只保留引用
    # ==========================================================================
    # 这三项（品种表 / 到期规则 / 标的市场映射）描述的都是市场事实，属基础数据层。
    # 此前 bds 复制了一份并逐步切换读副本，方向是「bds 依赖 irs」，违反「依赖只能向下」。
    # 现已反转：**实现与正源都在 `bds.config.Config`**，本类改为引用同一对象 ——
    # 既有调用点（`IrsCfg.OPTIONS_MARCH` 等）照旧可用，同时结构上不可能再存在
    # 「两份可各自被改」的副本。
    #
    # 🔒 新增/修改品种只改 `bds/config.py` 一处（及其上方「加品种三步」）。
    OPTIONS_MARCH = BdsCfg.OPTIONS_MARCH
    RULE_EXERCISE_DATE = BdsCfg.RULE_EXERCISE_DATE
    MAP_OPTIONS_UD_MARKET = BdsCfg.MAP_OPTIONS_UD_MARKET

    @classmethod
    def map_ud_market(cls):
        """转发 `bds.Config.map_ud_market()`（全仓零消费者，仅保留 API 兼容）。"""
        return BdsCfg.map_ud_market()
