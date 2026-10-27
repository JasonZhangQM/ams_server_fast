# -*- coding: utf-8 -*-
from pathlib import Path
from typing import Optional

# apps配置
class Config:
    def __init__(self):
        pass

    # 数据首次同步的统一起点（仅对"人为截断"类表生效）。
    # 1901-01-01 为名义下限，各数据源会自动截断到自身最早：
    #   gm   -> 个股/指数首日（含上市前披露的财务）
    #   FRED -> 序列起始（如 DGS10 从 1962-01-02）
    #   IMF  -> 数据集起始
    # 注意：仅首次同步（库中无该标的/序列数据）时生效；已有数据的标的走 MAX+1 增量，
    #      不会因改此值而回补历史，需另行删除旧行重跑。
    # 调整起点只改这一处。例外：wscn 日历源起点仍为 2015-01-01（见 economic_indicator.py）
    EARLIEST_START_DATE = "1901-01-01"

    FOLDER_SYMBOL = Path(r'C:\quant_man\statement\代码信息')

    # 代码【前两位】-> 交易所：用于把纯 6 位代码补成完整 symbol（如 601398 → SHSE.601398）。
    # ⚠️ 键是两位（不是首字符）：科创板 68xxxx 与深市 00xxxx 首字符分别为 6 / 0，
    #    若按首字符匹配就会把科创板误判成沪主板；北交所 920xxx 更需要前两位区分。
    # 与 services/symbol_info/market_board.py 的板块映射一一对应（60/68→沪、00/30→深、43/83/87/92→北交所）。
    MAP_MARKET_CODE = { #代码前两位 -> 交易所
        '60':'SHSE',  # 沪市主板
        '68':'SHSE',  # 科创板
        '00':'SZSE',  # 深市主板
        '30':'SZSE',  # 创业板
        '43':'BJSE',  # 北交所（老三板遗留代码段）
        '83':'BJSE',  # 北交所
        '87':'BJSE',  # 北交所
        '92':'BJSE'   # 北交所（新代码段，em 当前 353 只全在此段）
    }

    # 交易所代码 -> AkShare 期货行情接口（futures_zh_spot）的 market 参数。
    # 只有期货类交易所有该参数：CFFEX 走金融期货口径（FF），商品四所走商品口径（CF）。
    # 股票/期权交易所（SHSE/SZSE/BJSE/SHQQ/SZQQ/HGT）不在此表内 —— 它们不查期货行情。
    # 消费者：bills 的实时市值（value_calc.fetch_futures_realtime_prices）。
    MAP_AKSHARE_MARKET = {
        'CFFEX': 'FF',  # 金融期货
        'SHFE': 'CF',   # 商品期货
        'DEX': 'CF',    # 商品期货
        'CZCE': 'CF',   # 商品期货
        'GFEX': 'CF',   # 商品期货
        'INE': 'CF',    # 商品期货
    }

    # 合约乘数：symbol 前缀 -> 乘数。市值 = 价格 × 持仓 × 乘数。
    # ⚠️ 查找方式（common.utils.map_value）：按字典顺序逐个 startswith，**先命中先返回**，
    #    不是最长前缀优先 ⇒ 具体键（'SHFE.au'）必须排在它所归属的通配键之前，否则被通配键先吃掉。
    #    当前表里没有 'SHFE.' 通配键，顺序暂无冲突；将来若要加，'SHFE.au' 必须留在它前面。
    # ⚠️ 前缀口径：2026-10-27 起全库统一为 `SHFE.`（旧数据曾是券商写法 `SHEX.`，已清洗干净）。
    # ⚠️ 键末尾带点（'SHSE.'）表示「该交易所全部标的」。新增品种时优先加更具体的键，
    #    不要改成按首字母或按品种名匹配 —— 期权代码（CFFEX.IO2611-P-4000）与
    #    期货代码（CFFEX.IC2612）只有前缀可区分。
    # 消费者：bills 的实时市值（value_calc.value_float）。
    #
    # 🔒 期权四类（CFFEX.IO/HO、SHQQ、SZQQ）**故意保持注释**，不是遗漏：
    #    value_float 里义务仓市值取的是正数（value_long = price × 持仓 × 乘数），
    #    正确应为负数。乘数=1 时错误恰好被压小（假盈利≈权利金）；一旦填入真乘数，
    #    假盈利会等比放大——实测 au2612P840 从 +5,870 变 +18,038（真实 −6,322）、
    #    IO2611-P-4000 从 +8,783 变 +17,693（真实 −307）。
    #    ⇒ 必须等 value_long 符号缺陷修好之后再解注释，且要同时接期权行情。
    MAP_MULTIPLIER = {
        'CFFEX.IF': 300,   # 沪深300股指期货 300元/点
        'CFFEX.IC': 200,   # 中证500股指期货 200元/点
        'CFFEX.IM': 200,   # 中证1000股指期货 200元/点
        'CFFEX.T': 10000,  # 十年期国债期货 10000元/点
        'CFFEX.TF': 10000, # 五年期国债期货 10000元/点
        # 'CFFEX.IO': 100, # 沪深300股指期权 100元/点 —— 见上方🔒，暂不启用
        # 'CFFEX.HO': 100, # 上证50股指期权 100元/点 —— 见上方🔒，暂不启用
        # 'SHQQ.': 10000,  # 沪市ETF期权 10000份/张 —— 见上方🔒，暂不启用
        # 'SZQQ.': 10000,  # 深市ETF期权 10000份/张 —— 见上方🔒，暂不启用
        'SHFE.au': 1000,   # 黄金期货/期权 1000克/手
        'SHSE.': 1,        # A股/场内基金 1份
        'SZSE.': 1,
        'BJSE.': 1,
    }

    # ============ 期权品种台账（2026-10-27 合并，**唯一正源**）============
    # 合并缘由：此前「我要同步哪些品种」(`OPTIONS_MARCH`) 与「我们的品种名对应 openctp
    # 哪个品种」(`MAP_OPTION_LEDGER`) 是两张表，键都是同一个 option_name，字段互补不重叠，
    # 加一个品种要改两处、漏一处就出「行情能存但查不到中文名」或「info 有行但拉不到行情」。
    # 现在合成一张 `OPTION_VARIETIES`：**键 = option_name（唯一身份）**，值为该品种的全部属性。
    #
    # 依赖方向固定为 **irs → bds**：`irs/config.py` 已不再持有任何副本，只能通过下面
    # 五个访问器读，后续本表结构再变也不会波及 irs。
    #
    # 🔒 加/改品种只改本表一处（见下方「加品种三步」）。
    #
    # 与 MAP_MULTIPLIER 的关系：本表每行的 multiplier 是**品种默认乘数**（ETF 拼标准码的
    # 分母），不是逐合约乘数（后者在 `bds_option_info.multiplier`，A 类为 10180）。
    # 10-27 起两处都用 int（历史上本表是字符串 '100'，已统一）。
    OPTION_VARIETIES = {
        # —— 已启用（实测在 sync 链路中跑通）——
        '沪深300股指期权': dict(
            option_type='股指期权', und_key='csi300',
            product_id='IO', ctp_underlying=None,
            underlying_symbol='SHSE.000300', multiplier=100, rule_exercise_date='R1'),
        '南方中证500ETF期权': dict(
            option_type='ETF期权', und_key='csi500etf',
            product_id='ETF_O', ctp_underlying='510500',
            underlying_symbol='SHSE.510500', multiplier=10000, rule_exercise_date='R2'),
        '黄金期权': dict(
            option_type='商品期权', und_key='au',
            product_id='au_o', ctp_underlying=None,
            underlying_symbol='SHFE.au', multiplier=1000, rule_exercise_date='R3'),
        # —— 字典里有、暂未启用（[待核]：akshare 入参中文名需逐个验证，见方案 §六）——
        # '上证50股指期权': dict(option_type='股指期权', und_key='csi50',
        #     product_id='HO', ctp_underlying=None,
        #     underlying_symbol='SHSE.000016', multiplier=100, rule_exercise_date='R1'),
        # '中证1000股指期权': dict(option_type='股指期权', und_key='csi1000',
        #     product_id='MO', ctp_underlying=None,
        #     underlying_symbol='SHSE.000852', multiplier=100, rule_exercise_date='R1'),
        # '华泰柏瑞沪深300ETF期权': dict(option_type='ETF期权', und_key='csi300etf',
        #     product_id='ETF_O', ctp_underlying='510300',
        #     underlying_symbol='SHSE.510300', multiplier=10000, rule_exercise_date='R2'),
        # '华夏上证50ETF期权': dict(option_type='ETF期权', und_key='csi50etf',
        #     product_id='ETF_O', ctp_underlying='510050',
        #     underlying_symbol='SHSE.510050', multiplier=10000, rule_exercise_date='R2'),
        # '易方达创业板ETF期权': dict(option_type='ETF期权', und_key='chinextetf',
        #     product_id='ETF_O', ctp_underlying='159915',
        #     underlying_symbol='SZSE.159915', multiplier=10000, rule_exercise_date='R2'),
        # '嘉实沪深300ETF期权': dict(option_type='ETF期权', und_key='csi300etf',
        #     product_id='ETF_O', ctp_underlying='159919',
        #     underlying_symbol='SZSE.159919', multiplier=10000, rule_exercise_date='R2'),
        # '嘉实中证500ETF期权': dict(option_type='ETF期权', und_key='csi500etf',
        #     product_id='ETF_O', ctp_underlying='159922',
        #     underlying_symbol='SZSE.159922', multiplier=10000, rule_exercise_date='R2'),
        # '易方达深证100ETF期权': dict(option_type='ETF期权', und_key='sz100etf',
        #     product_id='ETF_O', ctp_underlying='159901',
        #     underlying_symbol='SZSE.159901', multiplier=10000, rule_exercise_date='R2'),
        # '华夏上证科创板50ETF期权': dict(option_type='ETF期权', und_key='kc50etf',
        #     product_id='ETF_O', ctp_underlying='588000',
        #     underlying_symbol='SHSE.588000', multiplier=10000, rule_exercise_date='R2'),
        # '易方达上证科创板50ETF期权': dict(option_type='ETF期权', und_key='kc50etf',
        #     product_id='ETF_O', ctp_underlying='588080',
        #     underlying_symbol='SHSE.588080', multiplier=10000, rule_exercise_date='R2'),
    }
    #
    # 字段口径：
    #   option_type        股指期权/ETF期权/商品期权 ⇒ 决定走「商品 .dat」还是「akshare 板」，
    #                      并映射保证金公式族（见 option_market.MARGIN_TYPE_BY_OPTION_TYPE）
    #   und_key            情景分组键（前端「品种」分组标签、`_sina_code` 分支）
    #   product_id         openctp 的品种ID。92 个品种只有下面几个有映射，其余照存不映射
    #   ctp_underlying     标的 6 位码。⚠️ `ETF_O` 一个品种ID覆盖 9 个标的，只按 product_id
    #                      匹配会把 510050 的合约全部串成 510500 ⇒ ETF 必须靠这一列细分；
    #                      IO/HO/MO/au_o 的标的由品种唯一确定，填 None
    #   underlying_symbol  完整标的代码，拼 gm/新浪行情用（`SHFE.au` + 月份 = `SHFE.au2612`）
    #   multiplier         品种默认乘数（ETF 拼标准码的分母，A 类别用 10180）
    #   rule_exercise_date 指向下方 RULE_EXERCISE_DATE 的规则码（仅交叉校验用）
    #
    # 加一个品种的三步（顺序不能变）：
    #   1. 先验证行情能拉到：`option_finance_board`／上期所 `.dat` 能返回该品种
    #      （入参是中文名，很容易写错，写错了只会在同步时才暴露成「接口返回空」）
    #   2. 在本表加一行（乘数 + 到期规则 + 标的 + 品种ID 一次给全）
    #   3. 跑一次批量同步的 `option_info` 段（`POST /bds/sync/macro` +
    #      `{"stages":["option_info"]}`）让字典认到该标的的合约
    # 只做 1、2 ⇒ 行情能存但字典里查不到中文名；只做 2 不做 1 ⇒ info 有行但拉不到行情。

    # 到期日规则：rule -> (第几个星期 1-based, 星期几 0=周一 ... 4=周五)
    # R1 股指期权（IO/HO/MO）＝合约月第三个周五
    # R2 ETF 期权（510300/510500/159919）＝合约月第四个周三
    # R3 商品期权（沪金等）＝标的交割月前一月倒数第 5 个交易日 ⇒ **无元组**，
    #    这里只放 None 占位，真实逻辑在服务层按日历倒数，不能按 (week_n, weekday) 套用。
    # 遇节假日顺延由服务层查 bds_trade_date 处理（这也是 irs 无法自给、必须依赖 bds 的原因）。
    RULE_EXERCISE_DATE = {
        'R1': (3, 4),
        'R2': (4, 2),
        'R3': None,
    }

    # ---- 品种台账访问器 ----
    # irs 只准用这五个函数，不准直读 OPTION_VARIETIES（结构再变不影响调用方）。

    @classmethod
    def option_varieties(cls) -> list[dict]:
        """全部品种，按定义顺序，每项**注入 `option_name`**。

        返回的是**副本**：调用方改了不会污染配置（历史上确有调用点想往 meta 里塞临时字段）。
        """
        return [dict(option_name=name, **meta)
                for name, meta in cls.OPTION_VARIETIES.items()]

    @classmethod
    def option_names(cls) -> list[str]:
        """全部品种名（下拉框顺序 = 定义顺序，不是字典序）。"""
        return list(cls.OPTION_VARIETIES.keys())

    @classmethod
    def option_meta(cls, option_name: str) -> Optional[dict]:
        """品种名 → 完整属性（含 `option_name`）。未登记返回 None，**不抛错**。

        不抛错是刻意的：92 个 openctp 品种里只有少量有中文名映射，反查（`option_name_of`）
        会大量撞到未登记品种，那里抛错等于让字典同步整体失败。
        """
        meta = cls.OPTION_VARIETIES.get(option_name)
        return dict(option_name=option_name, **meta) if meta else None

    @classmethod
    def und_key_of(cls, option_name: str) -> Optional[str]:
        """品种名 → 情景分组键。未登记返回 None。"""
        meta = cls.OPTION_VARIETIES.get(option_name)
        return meta["und_key"] if meta else None

    @classmethod
    def option_name_of_und(cls, underlying_symbol: str) -> Optional[str]:
        """标的完整代码 → 品种名（`SHSE.510500` → `南方中证500ETF期权`）。

        取代此前 router 里手抄的 `_OPT_UND_KEY` 反向硬编码表。
        """
        for name, meta in cls.OPTION_VARIETIES.items():
            if meta["underlying_symbol"] == underlying_symbol:
                return name
        return None

    INDEX_CODE = { #指数代码映射
        # constituent_count：**成分股只数上限**，供成分同步做伪快照拦截（见 index_constituent._judge_change）。
        # 宽基指数成分数恒定，超过它必然是新旧名单并存；缺 2 只以上必然是 gm 返回缺股。
        # 不填（None）⇒ 该项不做只数校验（上证指数取全部沪市股票，无固定只数）。
        'SHSE.000001':{'sec_name':'上证指数','market_code':'SHSE','listed_date':'1991-07-15'},
        'SHSE.000300':{'sec_name':'沪深300','market_code':'SHSE','listed_date':'2005-04-08','constituent_count':300},
        # 'SHSE.000510':{'sec_name':'中证A500','market_code':'SHSE','listed_date':'2024-09-23'},
        'SHSE.000905':{'sec_name':'中证500','market_code':'SHSE','listed_date':'2007-01-15','constituent_count':500},
        'SZSE.399006':{'sec_name':'创业板指','market_code':'SZSE','listed_date':'2010-06-01','constituent_count':100},
        'SHSE.000852':{'sec_name':'中证1000','market_code':'SHSE','listed_date':'2014-10-17','constituent_count':1000},
        'SHSE.000688':{'sec_name':'科创50','market_code':'SHSE','listed_date':'2020-07-23','constituent_count':50},
        # 美股指数：gm 不支持，通过 yfinance 拉取
        # 'SP500':{'sec_name':'S&P 500','market_code':'US','listed_date':'1957-01-01','data_source':'yfinance','yf_ticker':'^GSPC'},
    }

    # 收益率指标已拆分至 DAILY_INDICATORS 字典，通过 FRED API 同步到 bds_daily_indicator 表
    # fred_units: lin=原始值 pch=环比百分比变化 pc1=同比百分比变化 pca=复合年化变化率
    DAILY_INDICATORS = {  # 美债收益率指标配置（代码 -> 元信息），独立于 ECONOMIC_INDICATORS，仅通过 FRED API 同步
        'YIELD_2Y':           {'name': '2年期美债收益率',  'short_name': '二年美债收益率',  'country': '美国', 'category': '收益率', 'unit': '%', 'frequency': 'daily', 'fred_series_id': 'DGS2',   'fred_units': 'lin'},
        'YIELD_10Y':          {'name': '10年期美债收益率', 'short_name': '十年美债收益率', 'country': '美国', 'category': '收益率', 'unit': '%', 'frequency': 'daily', 'fred_series_id': 'DGS10',  'fred_units': 'lin'},
        'YIELD_SPREAD_10Y2Y': {'name': '10Y-2Y利差',       'short_name': '10Y-2Y利差',     'country': '美国', 'category': '收益率', 'unit': '%', 'frequency': 'daily', 'fred_series_id': 'T10Y2Y', 'fred_units': 'lin'},
        'YIELD_TIPS_10Y':     {'name': '10年期TIPS收益率', 'short_name': '十年TIPS收益率', 'country': '美国', 'category': '收益率', 'unit': '%', 'frequency': 'daily', 'fred_series_id': 'DFII10', 'fred_units': 'lin'},
        # 信用利差（ICE BofA 穆迪评级利差）：投资级/高收益债相对美债的收益率溢价
        'CREDIT_SPREAD_IG':   {'name': '投资级信用利差',     'short_name': '投资级信用利差',     'country': '美国', 'category': '信用利差', 'unit': '%', 'frequency': 'daily', 'fred_series_id': 'BAMLC0A4CBBB', 'fred_units': 'lin'},
        'CREDIT_SPREAD_HY':   {'name': '高收益债信用利差',   'short_name': '高收益信用利差',   'country': '美国', 'category': '信用利差', 'unit': '%', 'frequency': 'daily', 'fred_series_id': 'BAMLH0A0HYM2', 'fred_units': 'lin'},
    }

    ECONOMIC_INDICATORS = {  # 各国核心宏观经济指标配置（代码 -> 元信息）
        # 主数据源：wscn 日历 API（覆盖美国/中国/加拿大宏观指标）
        # 收益率指标已拆分至 DAILY_INDICATORS 字典，通过 FRED API 同步到 bds_daily_indicator 表
        # fred_units: lin=原始值 pch=环比百分比变化 pc1=同比百分比变化 pca=复合年化变化率
        # 美国宏观指标：按 category 分组整合排序（共 35 个）
        # 非收益率指标仅通过 wscn 日历数据源同步
        # short_name: 5 字左右简称，供前端筛选框/表格首列使用
        # ---- 利率与财政 ----
        'FED_FUNDS_RATE':        {'name': 'FOMC利率决策(下限)', 'short_name': '联邦利率下限', 'country': '美国', 'category': '利率与财政', 'unit': '%',    'frequency': 'per_fomc'},
        'FED_FUNDS_RATE_UPPER':  {'name': 'FOMC利率决策(上限)', 'short_name': '联邦利率上限', 'country': '美国', 'category': '利率与财政', 'unit': '%',    'frequency': 'per_fomc'},
        'GOVERNMENT_BUDGET':     {'name': '政府预算',           'short_name': '政府预算',     'country': '美国', 'category': '利率与财政', 'unit': '亿美元', 'frequency': 'monthly'},
        # ---- 通胀 ----
        'CPI_YOY':                           {'name': 'CPI同比',              'short_name': 'CPI同比',              'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        # 'CPI_MOM':                           {'name': 'CPI月率',              'short_name': 'CPI月率',              'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        'CORE_CPI_YOY':                      {'name': '核心CPI同比',          'short_name': '核心CPI同比',          'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        # 'CORE_CPI_MOM':                      {'name': '核心CPI月率',          'short_name': '核心CPI月率',          'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        'PCE_YOY':                           {'name': 'PCE物价指数同比',      'short_name': 'PCE同比',              'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        # 'PCE_MOM':                           {'name': 'PCE物价指数环比',      'short_name': 'PCE环比',              'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        'CORE_PCE_YOY':                      {'name': '核心PCE年率',          'short_name': '核心PCE年率',          'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        # 'CORE_PCE_MOM':                      {'name': '核心PCE物价指数环比',  'short_name': '核心PCE环比',          'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        'PPI_YOY':                           {'name': 'PPI同比',              'short_name': 'PPI同比',              'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        'MICHIGAN_5Y_INFLATION_EXPECTATION': {'name': '密歇根大学5年通胀预期', 'short_name': '密大5年通胀预期',      'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        'MICHIGAN_1Y_INFLATION_EXPECTATION': {'name': '密歇根大学1年通胀预期', 'short_name': '密大1年通胀预期',      'country': '美国', 'category': '通胀', 'unit': '%', 'frequency': 'monthly'},
        # ---- 就业 ----
        'NONFARM_PAYROLL':        {'name': '非农就业新增',         'short_name': '非农就业新增',         'country': '美国', 'category': '就业', 'unit': '万人', 'frequency': 'monthly'},
        'UNEMPLOYMENT_RATE':      {'name': '失业率',               'short_name': '失业率',               'country': '美国', 'category': '就业', 'unit': '%',   'frequency': 'monthly'},
        'INITIAL_JOBLESS_CLAIMS': {'name': '首次申请失业救济人数', 'short_name': '初请失业救济人数',     'country': '美国', 'category': '就业', 'unit': '万人', 'frequency': 'weekly'},
        'ADP_EMPLOYMENT_CHANGE':  {'name': 'ADP就业人数变动',      'short_name': 'ADP就业人数变动',      'country': '美国', 'category': '就业', 'unit': '万人', 'frequency': 'monthly'},
        'JOLTS_JOB_OPENINGS':     {'name': 'JOLTS职位空缺',        'short_name': 'JOLTS职位空缺',        'country': '美国', 'category': '就业', 'unit': '万人', 'frequency': 'monthly'},
        # ---- 经济增长 ----
        'GDP_QOQ':                {'name': 'GDP季环比', 'short_name': 'GDP季环比', 'country': '美国', 'category': '经济增长', 'unit': '%', 'frequency': 'quarterly'},
        # ---- 景气调查 ----
        'ISM_MFG_PMI':           {'name': 'ISM制造业PMI',      'short_name': 'ISM制造业PMI',      'country': '美国', 'category': '景气调查', 'unit': '', 'frequency': 'monthly'},
        'ISM_NON_MFG_PMI':       {'name': 'ISM非制造业指数',   'short_name': 'ISM非制造业PMI',   'country': '美国', 'category': '景气调查', 'unit': '', 'frequency': 'monthly'},
        'SP_GLOBAL_MFG_PMI':     {'name': '标普全球制造业PMI', 'short_name': '标普全球制造业PMI', 'country': '美国', 'category': '景气调查', 'unit': '', 'frequency': 'monthly'},
        'SP_GLOBAL_SVC_PMI':     {'name': '标普全球服务业PMI', 'short_name': '标普全球服务业PMI', 'country': '美国', 'category': '景气调查', 'unit': '', 'frequency': 'monthly'},
        'NY_FED_MFG_INDEX':      {'name': '纽约联储制造业指数', 'short_name': '纽联储制造业指数', 'country': '美国', 'category': '景气调查', 'unit': '', 'frequency': 'monthly'},
        'RICHMOND_FED_MFG_INDEX':{'name': '里士满联储制造业指数','short_name': '里士满制造业指数',   'country': '美国', 'category': '景气调查', 'unit': '', 'frequency': 'monthly'},
        # ---- 消费与投资 ----
        'RETAIL_SALES_MOM':         {'name': '零售销售月率',    'short_name': '零售销售月率',    'country': '美国', 'category': '消费与投资', 'unit': '%',   'frequency': 'monthly'},
        'DURABLE_GOODS_ORDERS_MOM': {'name': '耐用品订单环比',  'short_name': '耐用品订单环比',  'country': '美国', 'category': '消费与投资', 'unit': '%',   'frequency': 'monthly'},
        'EXISTING_HOME_SALES':      {'name': '成屋销售总数年化','short_name': '成屋销售总数年化','country': '美国', 'category': '消费与投资', 'unit': '万户', 'frequency': 'monthly'},
        'CONSUMER_CONFIDENCE':      {'name': '消费者信心指数', 'short_name': '消费者信心指数',  'country': '美国', 'category': '消费与投资', 'unit': '', 'frequency': 'monthly'},
        # ---- 能源 ----
        'EIA_GASOLINE_INVENTORY_CHANGE':  {'name': 'EIA汽油库存变动', 'short_name': 'EIA汽油库存变动', 'country': '美国', 'category': '能源', 'unit': '万桶', 'frequency': 'weekly'},
        'EIA_CRUDE_OIL_INVENTORY_CHANGE': {'name': 'EIA原油库存变动', 'short_name': 'EIA原油库存变动', 'country': '美国', 'category': '能源', 'unit': '万桶', 'frequency': 'weekly'},
        # 中国宏观指标：FRED 不提供中国数据，仅通过 wscn 日历数据源同步
        # 分类参照 wscn_indicators_updated.xlsx：盈利基本面/流动性与信用/外贸与外部/消费与投资/景气预期
        # ---- 盈利基本面 ----
        'CN_GDP_YOY':                       {'name': 'GDP同比',                    'short_name': 'GDP同比',                    'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'quarterly'},
        'CN_GDP_CUM_YOY':                   {'name': 'GDP累计同比',                'short_name': 'GDP累计同比',                'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'quarterly'},
        'CN_CPI_YOY':                       {'name': 'CPI同比',                    'short_name': 'CPI同比',                    'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'monthly'},
        'CN_PPI_YOY':                       {'name': 'PPI同比',                    'short_name': 'PPI同比',                    'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'monthly'},
        'CN_URBAN_UNEMPLOYMENT':            {'name': '城镇调查失业率',             'short_name': '城镇失业率',                 'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'monthly'},
        'CN_INDUSTRIAL_PROFIT_YOY':         {'name': '规模以上工业企业利润同比',   'short_name': '工业利润同比',               'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'monthly'},
        'CN_INDUSTRIAL_VALUE_ADDED_YOY':    {'name': '规模以上工业增加值同比',     'short_name': '工业增加值同比',             'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'monthly'},
        'CN_INDUSTRIAL_VALUE_ADDED_CUM_YOY':{'name': '规模以上工业增加值累计同比', 'short_name': '工业增加值累计同比',         'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'monthly'},
        'CN_INDUSTRIAL_PROFIT_CUM_YOY':     {'name': '规模以上工业企业利润累计同比','short_name': '工业利润累计同比',           'country': '中国', 'category': '盈利基本面',   'unit': '%',    'frequency': 'monthly'},
        # ---- 流动性与信用 ----
        # 'CN_M0_YOY':           {'name': 'M0货币供应同比',          'country': '中国', 'category': '流动性与信用', 'unit': '%',    'frequency': 'monthly'},
        'CN_M1_YOY':           {'name': 'M1货币供应同比',          'short_name': 'M1同比',          'country': '中国', 'category': '流动性与信用', 'unit': '%',    'frequency': 'monthly'},
        'CN_M2_YOY':           {'name': 'M2货币供应同比',          'short_name': 'M2同比',          'country': '中国', 'category': '流动性与信用', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_NEW_RMB_LOANS':    {'name': '新增人民币贷款',          'country': '中国', 'category': '流动性与信用', 'unit': '亿元', 'frequency': 'monthly'},
        'CN_NEW_RMB_LOANS_CUM':{'name': '新增人民币贷款累计',      'short_name': '新增贷款累计',    'country': '中国', 'category': '流动性与信用', 'unit': '亿元', 'frequency': 'monthly'},
        # 'CN_SOCIAL_FINANCING': {'name': '社会融资规模增量',        'country': '中国', 'category': '流动性与信用', 'unit': '亿元', 'frequency': 'monthly'},
        'CN_SOCIAL_FINANCING_CUM': {'name': '社会融资规模增量累计','short_name': '社融增量累计',    'country': '中国', 'category': '流动性与信用', 'unit': '亿元', 'frequency': 'monthly'},
        'CN_LPR_1Y':           {'name': '一年期贷款市场报价利率',  'short_name': '一年期LPR',       'country': '中国', 'category': '流动性与信用', 'unit': '%',    'frequency': 'monthly'},
        'CN_LPR_5Y':           {'name': '五年期贷款市场报价利率',  'short_name': '五年期LPR',       'country': '中国', 'category': '流动性与信用', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_MLF_RATE_1Y':      {'name': '一年期MLF中标利率',       'country': '中国', 'category': '流动性与信用', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_MLF_VOLUME_1Y':    {'name': '一年期MLF操作规模',       'country': '中国', 'category': '流动性与信用', 'unit': '亿元', 'frequency': 'monthly'},
        # ---- 外贸与外部 ----
        'CN_EXPORT_YOY_USD':       {'name': '出口同比(按美元计)',     'short_name': '出口同比(美元)',     'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        'CN_IMPORT_YOY_USD':       {'name': '进口同比(按美元计)',     'short_name': '进口同比(美元)',     'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_EXPORT_YOY_CNY':       {'name': '出口同比(按人民币计)',   'short_name': '出口同比(人民币)',   'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_IMPORT_YOY_CNY':       {'name': '进口同比(按人民币计)',   'short_name': '进口同比(人民币)',   'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_EXPORT_CUM_YOY_USD':   {'name': '出口累计同比(按美元计)', 'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_IMPORT_CUM_YOY_USD':   {'name': '进口累计同比(按美元计)', 'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_EXPORT_CUM_YOY_CNY':   {'name': '出口累计同比(按人民币计)', 'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        # 'CN_IMPORT_CUM_YOY_CNY':   {'name': '进口累计同比(按人民币计)', 'country': '中国', 'category': '外贸与外部', 'unit': '%',    'frequency': 'monthly'},
        'CN_TRADE_BALANCE_USD':    {'name': '贸易帐(按美元计)',       'short_name': '贸易顺差(美元)',         'country': '中国', 'category': '外贸与外部', 'unit': '亿美元',   'frequency': 'monthly'},
        # 'CN_TRADE_BALANCE_CNY':    {'name': '贸易帐(按人民币计)',     'short_name': '贸易顺差(人民币)',       'country': '中国', 'category': '外贸与外部', 'unit': '亿人民币', 'frequency': 'monthly'},
        'CN_FX_RESERVES':          {'name': '外汇储备',               'short_name': '外汇储备',               'country': '中国', 'category': '外贸与外部', 'unit': '亿美元', 'frequency': 'monthly'},
        'CN_SWIFT_CNY_SHARE':      {'name': 'Swift人民币在全球支付中占比', 'short_name': 'Swift人民币占比',  'country': '中国', 'category': '外贸与外部', 'unit': '%', 'frequency': 'monthly'},
        # ---- 消费与投资 ----
        'CN_URBAN_FIXED_ASSET_INVEST_YOY': {'name': '城镇固定资产投资同比',     'short_name': '城镇固投同比',         'country': '中国', 'category': '消费与投资', 'unit': '%', 'frequency': 'monthly'},
        'CN_REAL_ESTATE_INVEST':           {'name': '房地产开发投资同比',       'short_name': '房地产投资同比',       'country': '中国', 'category': '消费与投资', 'unit': '%', 'frequency': 'monthly'},
        'CN_RETAIL_SALES_YOY':             {'name': '社会消费品零售总额同比',   'short_name': '社销零同比',           'country': '中国', 'category': '消费与投资', 'unit': '%', 'frequency': 'monthly'},
        'CN_RETAIL_SALES_CUM_YOY':         {'name': '社会消费品零售总额累计同比','short_name': '社销零累计同比',       'country': '中国', 'category': '消费与投资', 'unit': '%', 'frequency': 'monthly'},
        # ---- 景气预期 ----
        'CN_OFFICIAL_MFG_PMI':     {'name': '官方制造业PMI',     'short_name': '官方制造业PMI',     'country': '中国', 'category': '景气预期', 'unit': '', 'frequency': 'monthly'},
        'CN_OFFICIAL_NON_MFG_PMI': {'name': '官方非制造业PMI',   'short_name': '官方非制造业PMI',   'country': '中国', 'category': '景气预期', 'unit': '', 'frequency': 'monthly'},
        'CN_RATINGDOG_MFG_PMI':    {'name': 'RatingDog制造业PMI', 'short_name': '财新制造业PMI',    'country': '中国', 'category': '景气预期', 'unit': '', 'frequency': 'monthly'},
        'CN_RATINGDOG_SVC_PMI':    {'name': 'RatingDog服务业PMI', 'short_name': '财新服务业PMI',    'country': '中国', 'category': '景气预期', 'unit': '', 'frequency': 'monthly'},
        # 加拿大宏观指标：FRED 不提供加拿大数据，仅通过 wscn 日历数据源同步
        'CA_POLICY_RATE':      {'name': '加拿大央行政策利率', 'short_name': '加央行利率', 'country': '加拿大', 'category': '利率', 'unit': '%', 'frequency': 'per_boc'},
    }

    # 华尔街见闻日历数据源：wscn_ticker -> indicator_code 映射
    # 数据源接口：GET https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas?start={ts}&end={ts}
    # 覆盖 32 个美国指标（3 个收益率指标 wscn 无对应，仍由 FRED 提供）+ 38 个中国指标 + 1 个加拿大指标 = 71 个映射
    # importance/revised/forecast/public_date 由 wscn 接口返回，补充 FRED 缺失字段
    WSCN_INDICATOR_MAP = {
        # 美国 - 利率与财政
        'US191228': 'FED_FUNDS_RATE',      # FOMC利率决策(下限)
        'US191229': 'FED_FUNDS_RATE_UPPER', # FOMC利率决策(上限)
        'US181222': 'GOVERNMENT_BUDGET',   # 政府预算
        # 美国 - 通胀
        'US111017': 'CPI_YOY',             # CPI同比
        'US111018': 'CORE_CPI_YOY',        # 核心CPI同比
        'US111044': 'CPI_MOM',             # CPI环比
        'US111045': 'CORE_CPI_MOM',        # 核心CPI环比
        'US111033': 'PCE_YOY',             # PCE物价指数同比
        'US111046': 'PCE_MOM',             # PCE物价指数环比
        'US111034': 'CORE_PCE_YOY',        # 核心PCE物价指数同比
        'US111047': 'CORE_PCE_MOM',        # 核心PCE物价指数环比
        'US111042': 'PPI_YOY',             # PPI同比
        'US111025': 'MICHIGAN_5Y_INFLATION_EXPECTATION', # 密歇根大学5年通胀预期
        'US111024': 'MICHIGAN_1Y_INFLATION_EXPECTATION', # 密歇根大学1年通胀预期
        # 美国 - 就业
        'US121058': 'NONFARM_PAYROLL',     # 非农就业人口变动
        'US121050': 'UNEMPLOYMENT_RATE',   # 失业率
        'US121055': 'INITIAL_JOBLESS_CLAIMS', # 首次申请失业救济人数
        'US121065': 'ADP_EMPLOYMENT_CHANGE',  # ADP就业人数变动
        'US121074': 'JOLTS_JOB_OPENINGS',     # JOLTS职位空缺
        # 美国 - 景气调查
        'US151132': 'ISM_MFG_PMI',         # ISM制造业指数
        'US151138': 'ISM_NON_MFG_PMI',     # ISM非制造业指数
        'US151144': 'SP_GLOBAL_MFG_PMI',   # 标普全球制造业PMI
        'US151145': 'SP_GLOBAL_SVC_PMI',   # 标普全球服务业PMI
        'US151154': 'NY_FED_MFG_INDEX',    # 纽约联储制造业指数
        'US151158': 'RICHMOND_FED_MFG_INDEX', # 里士满联储制造业指数
        'US151146': 'CONSUMER_CONFIDENCE', # 谘商会消费者信心指数
        # 美国 - 经济增长
        'US101000': 'GDP_QOQ',             # 实际GDP年化季环比
        # 美国 - 消费与投资
        'US171206': 'RETAIL_SALES_MOM',    # 零售销售环比
        'US141104': 'DURABLE_GOODS_ORDERS_MOM', # 耐用品订单环比
        'US161171': 'EXISTING_HOME_SALES',     # 成屋销售总数年化
        # 美国 - 能源
        'US141124': 'EIA_GASOLINE_INVENTORY_CHANGE', # EIA汽油库存变动
        'US141126': 'EIA_CRUDE_OIL_INVENTORY_CHANGE', # EIA原油库存变动
        # 中国 - 盈利基本面
        'CN101256': 'CN_GDP_YOY',                      # 二季度GDP同比
        'CN101273': 'CN_GDP_CUM_YOY',                   # 一至一季度GDP同比
        'CN111275': 'CN_CPI_YOY',                       # CPI同比
        'CN111291': 'CN_PPI_YOY',                       # PPI同比
        'CN121479': 'CN_URBAN_UNEMPLOYMENT',            # 城镇调查失业率
        'CN141328': 'CN_INDUSTRIAL_PROFIT_YOY',         # 规模以上工业企业利润同比
        'CN141330': 'CN_INDUSTRIAL_VALUE_ADDED_YOY',    # 规模以上工业增加值同比
        'CN141334': 'CN_INDUSTRIAL_VALUE_ADDED_CUM_YOY',# 规模以上工业增加值累计同比
        'CN191484': 'CN_INDUSTRIAL_PROFIT_CUM_YOY',     # 规模以上工业企业利润累计同比
        # 中国 - 流动性与信用
        # 'CN191440': 'CN_M0_YOY',            # M0货币供应同比
        'CN191441': 'CN_M1_YOY',            # M1货币供应同比
        'CN191442': 'CN_M2_YOY',            # M2货币供应同比
        'CN131335': 'CN_NEW_RMB_LOANS_CUM', # 新增人民币贷款累计
        # 'CN191445': 'CN_NEW_RMB_LOANS',     # 新增人民币贷款
        'CN131334': 'CN_SOCIAL_FINANCING_CUM', # 社会融资规模增量累计
        # 'CN191479': 'CN_SOCIAL_FINANCING',     # 社会融资规模增量
        'CN191480': 'CN_LPR_1Y',            # 一年期贷款市场报价利率
        'CN191481': 'CN_LPR_5Y',            # 五年期贷款市场报价利率
        # 'CN191482': 'CN_MLF_RATE_1Y',       # 一年期MLF中标利率
        # 'CN191483': 'CN_MLF_VOLUME_1Y',     # 一年期MLF操作规模
        # 中国 - 外贸与外部
        'CN131320': 'CN_EXPORT_YOY_USD',     # 出口同比(按美元计)
        'CN131321': 'CN_IMPORT_YOY_USD',     # 进口同比(按美元计)
        # 'CN131326': 'CN_EXPORT_YOY_CNY',     # 出口同比(按人民币计)
        # 'CN131327': 'CN_IMPORT_YOY_CNY',     # 进口同比(按人民币计)
        # 'CN131329': 'CN_EXPORT_CUM_YOY_USD', # 出口累计同比(按美元计)
        # 'CN131330': 'CN_IMPORT_CUM_YOY_USD', # 进口累计同比(按美元计)
        # 'CN131332': 'CN_EXPORT_CUM_YOY_CNY', # 出口累计同比(按人民币计)
        # 'CN131333': 'CN_IMPORT_CUM_YOY_CNY', # 进口累计同比(按人民币计)
        'CN191467': 'CN_FX_RESERVES',        # 外汇储备
        'CN131317': 'CN_TRADE_BALANCE_USD',  # 贸易帐(按美元计)
        # 'CN131323': 'CN_TRADE_BALANCE_CNY',  # 贸易帐(按人民币计)
        'CN191478': 'CN_SWIFT_CNY_SHARE',    # Swift人民币在全球支付中占比
        # 中国 - 消费与投资
        'CN141339': 'CN_URBAN_FIXED_ASSET_INVEST_YOY', # 城镇固定资产投资同比
        'CN161376': 'CN_REAL_ESTATE_INVEST',           # 全国房地产开发投资
        'CN171402': 'CN_RETAIL_SALES_CUM_YOY',         # 社会消费品零售总额累计同比
        'CN171405': 'CN_RETAIL_SALES_YOY',             # 社会消费品零售总额同比
        # 中国 - 景气预期
        'CN151359': 'CN_OFFICIAL_MFG_PMI',     # 官方制造业PMI
        'CN151360': 'CN_OFFICIAL_NON_MFG_PMI', # 官方非制造业PMI
        'CN151371': 'CN_RATINGDOG_MFG_PMI',    # RatingDog制造业PMI
        'CN151372': 'CN_RATINGDOG_SVC_PMI',    # RatingDog服务业PMI
        # 加拿大
        'CA193210': 'CA_POLICY_RATE',      # 加拿大央行政策利率
    }

    # 央行黄金储备主要持有国（IMF SDMX IFS 数据集，RAXG_USD 指标）
    # 国家代码 -> {country_name(中文), imf_code(ISO2 用于 IMF API)}
    GOLD_RESERVE_COUNTRIES = {
        'US': {'country_name': '美国',     'imf_code': 'US'},
        'DE': {'country_name': '德国',     'imf_code': 'DE'},
        'IT': {'country_name': '意大利',   'imf_code': 'IT'},
        'FR': {'country_name': '法国',     'imf_code': 'FR'},
        'RU': {'country_name': '俄罗斯',   'imf_code': 'RU'},
        'CN': {'country_name': '中国',     'imf_code': 'CN'},
        'CH': {'country_name': '瑞士',     'imf_code': 'CH'},
        'JP': {'country_name': '日本',     'imf_code': 'JP'},
        'IN': {'country_name': '印度',     'imf_code': 'IN'},
        'TR': {'country_name': '土耳其',   'imf_code': 'TR'},
        'TW': {'country_name': '中国台湾', 'imf_code': 'TW'},
        'PT': {'country_name': '葡萄牙',   'imf_code': 'PT'},
        'PL': {'country_name': '波兰',     'imf_code': 'PL'},
        'KZ': {'country_name': '哈萨克斯坦','imf_code': 'KZ'},
        'GB': {'country_name': '英国',     'imf_code': 'GB'},
        'ES': {'country_name': '西班牙',   'imf_code': 'ES'},
        'AT': {'country_name': '奥地利',   'imf_code': 'AT'},
        'BE': {'country_name': '比利时',   'imf_code': 'BE'},
        'TH': {'country_name': '泰国',     'imf_code': 'TH'},
        'SG': {'country_name': '新加坡',   'imf_code': 'SG'},
    }
