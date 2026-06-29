# -*- coding: utf-8 -*-
"""irs 应用配置（从 server_dj/apps/irs/config.py 迁移）。

保留所有原 Django Config 类的属性与方法：
- FOLDER_*：各类 Excel 文件所在文件夹
- MAP_MARKET_UNDERLYING：代码前缀 -> 交易所映射
- SYMBOL_CON_LX / SYMBOL_CON_ZL：连续合约代码列表
- MAP_OPTIONS_UD_MARKET + map_ud_market()：期权标的市场映射
"""
from pathlib import Path


class Config:
    """irs 应用配置类，与原 Django 版本完全一致。"""

    def __init__(self):
        pass

    # 各类 Excel 文件所在文件夹
    FOLDER_SYMBOL_VALUE = Path('C:\BaiduSyncdisk\账单\估值分析')
    FOLDER_OPTION = Path('C:\BaiduSyncdisk\账单\期权标的')
    FOLDER_SYMBOL_CON = Path('C:\BaiduSyncdisk\账单\连续合约')
    FOLDER_OPTION_PRICE = Path('C:\BaiduSyncdisk\账单\期权行情')

    # 代码市场映射
    MAP_MARKET_UNDERLYING = {
        '0': 'SHSE', '5': 'SHSE',
        '1': 'SZSE',
    }

    # 连续合约列表（连续主力）
    SYMBOL_CON_LX = [
        'CFFEX.IC00', 'CFFEX.IC01', 'CFFEX.IC02', 'CFFEX.IC03',
        'CFFEX.IF00', 'CFFEX.IF01', 'CFFEX.IF02', 'CFFEX.IF03',
        'CFFEX.IM00', 'CFFEX.IM01', 'CFFEX.IM02', 'CFFEX.IM03',
    ]
    # 主力连续合约代码前缀
    SYMBOL_CON_ZL = [
        'CFFEX.IC', 'CFFEX.IF', 'CFFEX.IH', 'CFFEX.IM',
    ]

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
