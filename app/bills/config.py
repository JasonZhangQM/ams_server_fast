# -*- coding: utf-8 -*-
"""bills 应用配置（从 server_dj/apps/bills/config.py 迁移）。

Config 类为纯 Python 配置类，不依赖任何框架，原样保留所有类属性与方法。
路径使用 raw string 以避免 Windows 反斜杠转义问题，实际路径值不变。
"""
from pathlib import Path


# apps配置
class Config:
    def __init__(self):
        pass

    FOLDER_BILLS = Path(r'C:\BaiduSyncdisk\账单')
    FOLDER_IO = Path(r'C:\BaiduSyncdisk\账单\出入金')
    FOLDER_QH = Path(r'C:\BaiduSyncdisk\账单\期货结单')

    ACCOUNT_INFO = {  # 账户信息
        'B01': {'acc_owner': '张三爸爸', 'acc_type': '股票账户', 'acc_inst': '东方财富'},
        'B02': {'acc_owner': '张三爸爸', 'acc_type': '信用账户', 'acc_inst': '东方财富'},
        'B03': {'acc_owner': '张三爸爸', 'acc_type': '期权账户', 'acc_inst': '东方财富'},
        'B04': {'acc_owner': '张三爸爸', 'acc_inst': '期货账户', 'acc_type': '弘业期货'},
        'B05': {'acc_owner': '张三爸爸', 'acc_inst': '虚拟币', 'acc_type': '币安'},
        'M01': {'acc_owner': '张三妈咪', 'acc_type': '股票账户', 'acc_inst': '东方财富'},
        'M02': {'acc_owner': '张三妈咪', 'acc_type': '股票账户', 'acc_inst': '广发证券'},
        'M03': {'acc_owner': '张三妈咪', 'acc_type': '信用账户', 'acc_inst': '广发证券'},
        'M04': {'acc_owner': '张三妈咪', 'acc_type': '期权账户', 'acc_inst': '广发证券'},
        'M05': {'acc_owner': '张三妈咪', 'acc_type': '期货账户', 'acc_inst': '广发期货'},
        'N01': {'acc_owner': '张三奶奶', 'acc_type': '股票账户', 'acc_inst': '国金证券'},
    }

    MAP_MARKET = {  # 市场映射
        '上海Ａ股': 'SHSE',
        '深圳Ａ股': 'SZSE',
        '沪市A股': 'SHSE',
        '深市A股': 'SZSE',
        '京市A股': 'BJSE',
        '沪港通': '',
        '上海股票期权': '',
        '上海': '',
        '深圳股票期权': '',
        '深圳': '',
        '中金所': 'CFFEX',
        '大商所': 'DEX',
        '上期所': 'SHEX',
        '郑商所': 'CZCE',
        '广期所': 'GFEX',
        '上海能源': 'INE',
        '开放式基金': '',
    }
    MAP_ACOUNT_MARKET = {  # 市场映射
        'A': '沪市A股',
        'E': '沪市A股',
        '0': '深市A股',
    }
    # 单点价值映射
    MAP_MULTIPLIER = {
        'CFFEX.IF': 300,
        'CFFEX.IC': 200,
        'CFFEX.IM': 200,
        'CFFEX.T': 10000,
        'CFFEX.TF': 10000,
        'SHSE.': 1,
        'SZSE.': 1,
        'BJSE.': 1,
    }

    MAP_CATEGORY = {  # 交易类型映射
        '证券交易': {
            '买入': ['担保品买入', '证券买入', '港股通买入', '融资买入', '新股申购', '配股缴款', '配售缴款', '开放基金申购'],
            '卖出': ['担保品卖出', '证券卖出', '港股通卖出', '卖券还款', '还款卖出', '申购还款', '配股退款退息'],
            '转入': ['担保物转入', '担保品划入', '股份转入'],
            '转出': ['担保物转出', '担保品划出', '股份转出'],
            '红利': ['红利入账', '股息入账', '港股通红利发放', '股息红利差异扣税', '股息红利税补'],
            '红股': ['红股入账'],
        },
        '期货交易': {
            '期货交易': ['交易', '投机'],
        },
        '期权交易': {
            '期权交易': ['期权交易', '认沽非备兑', '认购非备兑'],
        },
        '虚拟币': {
            '买入币': ['买入币'],
            '卖出币': ['卖出币'],
        },
        '理财': {
            '理财申购': ['报价融券回购', '融券回购', 'OTC资金划出', '质押回购拆出', '基金申购拨出', '天天宝申购'],
            '理财赎回': ['报价融券购回', '融券购回', 'OTC资金划入', 'OTC现金宝调账入', '拆出质押购回', '基金赎回拨入', '基金红利拨入', '天天宝赎回', '天天宝快速取现'],
        },
        '融资融券': {
            '融资借入': ['融资借入'],  # 无代码
            '融资还款': ['偿还融资负债本金', '直接偿还融资负债', '直接偿还融资费用'],
            '融资利息': ['偿还融资利息', '偿还融资逾期利息', '直接偿还融资利息'],
        },
        '收益费用': {
            '其他费用': ['港股通组合费', '申报费', '费用调整'],  # 无代码
            '其他收益': ['利息归本', '三方存管现金蓝补'],  # 无代码
        },
        '出入金': {
            '入金': ['入金', '银行转证券', '银行转存', '互通转存', '券商发起,银转衍'],  # 无代码
            '出金': ['出金', '证券转银行', '银行转取', '互通转取', '券商发起,衍转银'],
        },
        '-': {
            '-': ['配股权证', '申购配号', '配股入帐', '新股入帐', '指定交易'],
        },
    }

    MAP_SYMBOL = {  # 代码映射
        'SHSE.783838': 'SHSE.113055',  # 成银发债
        'SHSE.783963': 'SHSE.113056',  # 重银发债
        'SHSE.783881': 'SHSE.113057',  # 中银发债
        'SHSE.754806': 'SHSE.113661',  # 福22发债
        'SHSE.783665': 'SHSE.113065',  # 齐鲁发债
        'SZSE.370973': 'SZSE.123179',  # 立高发债
        'SZSE.072237': 'SZSE.127086',  # 恒邦转债
        'SHSE.713177': 'SHSE.111015',  # 东亚发债
        'SHSE.754305': 'SHSE.113685',  # 升24发债
        'SHSE.754596': 'SHSE.113696',  # 伯25发债
        'SHSE.754067': 'SHSE.113687',  # 振华发债
        'SZSE.370059': 'SZSE.123111',  # 东财发债
        'SZSE.380059': 'SZSE.123111',  # 东财配债
        'SHSE.783012': 'SHSE.113053',  # 隆基发债
        'SHSE.783878': 'SHSE.113060',  # 浙22发债
        'SHSE.718599': 'SHSE.118031',  # 天合发债
        'SHSE.787602': 'SHSE.688602',  # 康鹏申购
        'SZSE.072459': 'SZSE.127089',  # 晶澳发债
        'SZSE.072761': 'SZSE.127102',  # 浙建发债
        'SHSE.707368': 'SHSE.605368',  # 蓝天申购
        'SHSE.783229': 'SHSE.113042',  # 上银发债
        'SHSE.733926': 'SHSE.110079',  # 杭银发债
        'SHSE.783016': 'SHSE.113051',  # 节能发债
        'SZSE.082714': 'SZSE.127045',  # 牧原配债
        'SZSE.371216': 'SZSE.123247',  # 万凯发债
    }

    SYMBOL_CON_ZL = [  # 主力连续
        'CFFEX.IH', 'CFFEX.IF', 'CFFEX.IC', 'CFFEX.IM',
    ]
    SYMBOL_CON_IF = [  # IC连续合约
        'CFFEX.IF00', 'CFFEX.IF01', 'CFFEX.IF02', 'CFFEX.IF03',
    ]
    SYMBOL_CON_IC = [  # IC连续合约
        'CFFEX.IC00', 'CFFEX.IC01', 'CFFEX.IC02', 'CFFEX.IC03',
    ]
    SYMBOL_CON_IM = [  # IM连续合约
        'CFFEX.IM00', 'CFFEX.IM01', 'CFFEX.IM02', 'CFFEX.IM03',
    ]

    # 交易类型映射转换
    @classmethod
    def to_exec_type(cls):
        '''
        {'证券交易':{'买入': ['担保品买入','证券买入']}},
        ---->
        category_dict:{'担保品买入': '证券交易', '证券买入': '证券交易'}
        category1_dict:{'担保品买入': '买入', '证券买入': '买入'}
        '''
        category_dict = {}
        category1_dict = {}
        for i, j in cls.MAP_CATEGORY.items():
            for k, v in j.items():
                for l in v:
                    category_dict[l] = i
                    category1_dict[l] = k
        return category_dict, category1_dict
