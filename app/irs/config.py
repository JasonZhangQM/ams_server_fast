# -*- coding: utf-8 -*-
"""irs 应用配置。

- SYMBOL_CON_LIST：贴水连续合约代码列表
- SYMBOL_CON_ZL：主力连续合约代码前缀
- MAP_OPTIONS_UD_MARKET + map_ud_market()：期权标的市场映射
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

    OPTIONS_MARCH = (
        {'underlying_symbol':'SHSE.000300','option_type':'股指期权','option_name':'沪深300股指期权','multiplier':'100','rule_exercise_date':'R1'},
        {'underlying_symbol':'SHSE.510500','option_type':'ETF期权','option_name':'南方中证500ETF期权','multiplier':'10000','rule_exercise_date':'R2'},
        )

    # 到期日规则映射：rule_exercise_date -> (第几个星期, 星期几)
    # 星期几：0=周一 ... 4=周五
    # R1: 股指期权，合约月第三个周五；R2: ETF期权，合约月第四个周三
    # 遇节假日顺延至下一交易日（由 service 层查 TradeDate 日历处理）
    RULE_EXERCISE_DATE = {
        'R1': (3, 4),  # 合约月第三个周五
        'R2': (4, 2),  # 合约月第四个周三
    }


    # 期权标的市场映射
    MAP_OPTIONS_UD_MARKET = {
        'SHSE': [
            '000016', '000300', '000852', '510050',
            '510300', '510500', '588000', '588080',
        ],
        'SZSE': ['159901', '159919', '159922', '159915'],
    }

    @classmethod
    def map_ud_market(cls):
        '''
        'SZSE':['159901','159919','159922','159915']
        ---------->
        {'159901': 'SZSE.159901',}
        '''
        ud_market_dict = {}
        for k, v in cls.MAP_OPTIONS_UD_MARKET.items():
            for symbol in v:
                ud_market_dict[symbol] = f'{k}.{symbol}'
        return ud_market_dict
