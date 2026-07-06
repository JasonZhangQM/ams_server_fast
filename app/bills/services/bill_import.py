# -*- coding: utf-8 -*-
"""导入账单业务函数（从 bills/service.py 拆分）。"""
import logging
import re
import pandas as pd
from sqlalchemy import text

from server_fast.config import settings
from server_fast.common.utils import filter_in_cols
from server_fast.app.bills.config import Config as BlsCfg
from server_fast.app.bills.models import Bill

logger = logging.getLogger("uvicorn.error")

## 导入账单
# 读取数据库中指定账户中最近的交易时间
def get_field_max_account_sql(account, field='trade_time', mdl=Bill):
    _engine = settings.DB_ENGINE
    sql = f'''
        SELECT MAX({field}) as max_field
        FROM {mdl.__table__.name}
        WHERE account='{account}'
    '''
    with _engine.connect() as conn:
        sql = text(sql)  # 用 text() 包装 SQL 字符串
        result = conn.execute(sql)  # 执行查询
        return result.fetchone()[0]  # 获取查询结果的第一行

# 根据文件名处理df列(账单)
def handle_cols_from_file_name(df: pd.DataFrame, file_name: str) -> pd.DataFrame:
    if '出入金' in file_name:
        pass
    else:
        # 根据文件名处理df列
        if '期权' in file_name:
            df['类别'] = df['类别'].fillna('')
            df['备兑'] = df['备兑'].fillna('')
            df['交易类别'] = df['类别'] + df['备兑']  # 期权
            df['币种'] = '人民币'  # 期权、期货、信用
            return df
        elif '东财' in file_name and '股票' in file_name:
            df['交易时间'] = pd.to_datetime(df['交收日期'].astype(str) + ' ' + df['发生时间'].astype(str),
                format='mixed',  # 自动推断每个元素的格式
                dayfirst=False,  # 日期中日是否在前（根据你的数据调整）
                )  # 股票
            return df
        elif '东财' in file_name and '信用' in file_name:
            df['交易时间'] = pd.to_datetime(df['交收日期'].astype(str) + ' ' + df['发生时间'].astype(str),
                format='mixed',  # 自动推断每个元素的格式
                dayfirst=False,  # 日期中日是否在前（根据你的数据调整）
                )  # 股票
            df['币种'] = '人民币'  # 期权、期货、信用
            return df
        elif '广发' in file_name and '股票' in file_name:
            df['成交日期'] = pd.to_datetime(df['成交日期'].astype(str),
                format='mixed',  # 自动推断每个元素的格式
                dayfirst=False,  # 日期中日是否在前（根据你的数据调整）
                )
            df['币种'] = '人民币'  # 期权、期货、信用
            df['交易市场'] = df['股东帐户'].astype(str).str[0].map(BlsCfg.MAP_ACOUNT_MARKET)  # 交易市场映射
            return df
        elif '广发' in file_name and '信用' in file_name:
            df['成交日期'] = pd.to_datetime(df['成交日期'].astype(str),
                format='mixed',  # 自动推断每个元素的格式
                dayfirst=False,  # 日期中日是否在前（根据你的数据调整）
                )
            df['币种'] = '人民币'  # 期权、期货、信用
            df['交易市场'] = df['股东帐户'].astype(str).str[0].map(BlsCfg.MAP_ACOUNT_MARKET)  # 交易市场映射
            return df
        elif '国金' in file_name and '股票' in file_name:
            df['成交日期'] = pd.to_datetime(df['成交日期'].astype(str) + ' ' + df['成交时间'].astype(str),
                format='mixed',  # 自动推断每个元素的格式
                dayfirst=False,  # 日期中日是否在前（根据你的数据调整）
                )  # 股票
            df['币种'] = '人民币'  # 期权、期货、信用
            return df
        elif '期货' in file_name:
            df['币种'] = '人民币'  # 期权、期货、信用
            return df
        elif '虚拟币' in file_name:
            return df
        else:
            logger.warning(f"文件名 {file_name} 无法识别账单类型")
            raise ValueError

# 汇总税费
def handle_sum_fees(df: pd.DataFrame) -> pd.DataFrame:
    _mdl = Bill
    sum_cols = []
    for col in df.columns:
        if col in _mdl.fields_fee:
            sum_cols.append(col)
    df['fee_tax'] = df[sum_cols].sum(axis=1)
    return df

# 规整和处理字段
def handle_df_fields(df: pd.DataFrame) -> pd.DataFrame:
    _map_market = BlsCfg.MAP_MARKET
    _category_dict, _category1_dict = BlsCfg.to_exec_type()
    df['exec_type'] = df['exec_type'].str.replace(r'\s+', '', regex=True)  # 去除空格
    # 交易类别映射
    df['category'] = df['exec_type'].map(_category_dict)  # 交易类别映射
    df['category1'] = df['exec_type'].map(_category1_dict)  # 交易类别映射
    df['symbol'] = df['market'].map(  # 交易类别映射
        _map_market).astype(str) + '.' + df['symbol'].astype(str)
    # 移除所有以nan.开头的字符（仅保留后续内容）
    df['symbol'] = df['symbol'].str.replace(r'^nan\.', '', regex=True)
    # 处理symbol编号
    # 根据交易类型赋予symbol编号
    df.loc[df['exec_type'].str.contains('OTC', na=False), 'symbol'] = 'OTC'
    df.loc[df['exec_type'].str.contains('天天宝', na=False), 'symbol'] = 'TTB'
    # 根据交易类别赋予symbol编号
    df.loc[df['category'] == '融资融券', 'symbol'] = 'RZRQ'
    df.loc[df['category'] == '收益费用', 'symbol'] = 'SYFY'
    df.loc[df['category'] == '出入金', 'symbol'] = 'CRJ'
    return df

# 定义函数：从第一个数字开始截取后面内容，判断是否包含 P / C
def get_cp(s):
    if not isinstance(s, str):
        return None

    # 找到第一个数字位置
    match = re.search(r'\d', s)
    if not match:
        return None

    # 从第一个数字开始截取后面所有
    sub_str = s[match.start():]

    if 'P' in sub_str:
        return 'P'
    elif 'C' in sub_str:
        return 'C'
    else:
        return None

# 计算期货amount_act,cash字段
def handle_amount_act_from_file_name(df: pd.DataFrame, file_name: str) -> pd.DataFrame:
    if ('期货' in file_name):
        cdt = (df['exec_type'] == '入金')
        df.loc[cdt, 'amount_act'] = abs(df.loc[cdt, 'amount'])
        cdt = (df['exec_type'] == '出金')
        df.loc[cdt, 'amount_act'] = -abs(df.loc[cdt, 'amount'])
        cdt = (df['exec_type'] == '申报费')
        df.loc[cdt, 'amount_act'] = -abs(df.loc[cdt, 'amount'])
        df['b_s'] = df['b_s'].astype(str)
        df['o_c'] = df['o_c'].astype(str)
        cdt = (df['b_s'].str.contains('买', na=False) & df['o_c'].str.contains('开', na=False))
        df.loc[cdt, 'amount_act'] = -(abs(df.loc[cdt, 'amount']) + df.loc[cdt, 'fee_tax'])
        cdt = (df['b_s'].str.contains('卖', na=False) & df['o_c'].str.contains('开', na=False))
        df.loc[cdt, 'amount_act'] = (abs(df.loc[cdt, 'amount']) - df.loc[cdt, 'fee_tax'])
        cdt = (df['b_s'].str.contains('买', na=False) & df['o_c'].str.contains('平', na=False))
        df.loc[cdt, 'amount_act'] = -(abs(df.loc[cdt, 'amount']) + df.loc[cdt, 'fee_tax'])
        cdt = (df['b_s'].str.contains('卖', na=False) & df['o_c'].str.contains('平', na=False))
        df.loc[cdt, 'amount_act'] = (abs(df.loc[cdt, 'amount']) - df.loc[cdt, 'fee_tax'])
        df['cash'] = df['amount_act']
        cdt = df['name'].str.contains('期权', na=False)  # 处理期权的c_p字段
        df.loc[cdt, 'c_p'] = df.loc[cdt, 'symbol'].apply(get_cp)
        df.loc[cdt, 'category1'] = '期权交易'
        df.loc[cdt, 'category'] = '期权交易'
    elif '期权' in file_name:
        df['cash'] = df['amount_act']
    # 处理股票划转
    cdt = (df['category1'] == '转入')
    df['amount_act'] = df['amount_act'].astype(float)
    df.loc[cdt, 'amount_act'] = abs(df.loc[cdt, 'amount']) - df.loc[cdt, 'fee_tax']
    cdt = (df['category1'] == '转出')
    df.loc[cdt, 'amount_act'] = -(abs(df.loc[cdt, 'amount']) + df.loc[cdt, 'fee_tax'])
    return df

# 导入账单数据
def insert_bill_all_excel_sql():
    _engine = settings.DB_ENGINE
    _folder_bills = BlsCfg.FOLDER_BILLS
    _mdl = Bill
    file_names = [
        f.name for f in _folder_bills.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
        and (not f.name.startswith('~'))
    ]
    # 循环导入账单文件
    for file_name in file_names:
        result = file_name.split('-')
        df = pd.read_excel(
            _folder_bills / file_name,
            dtype={'证券代码': str, '交易编码': str, '合约编码': str})
        account = result[0]
        df['account'] = account
        max_trade_time = get_field_max_account_sql(account)
        logger.info(f"正在导入 {file_name}:{str(max_trade_time)}")
        # 根据文件名处理df列
        df = handle_cols_from_file_name(df, file_name)
        # df列名映射数据库字段名
        df = df.rename(columns=_mdl.map_fields())  # 外部列名映射数据库字段
        # 过滤df列与数据库字段交集(剔除不需要存入数据库的列)
        df = df[filter_in_cols(df.columns, _mdl.db_fields())]  # 过滤字段
        # 汇总税费
        df = handle_sum_fees(df)
        # 规整和处理字段
        df = handle_df_fields(df)
        # 计算期货amount_act字段
        df = handle_amount_act_from_file_name(df, file_name)
        df['trade_time'] = pd.to_datetime(df['trade_time'])  # 转换日期格式
        # 数据库中的最新时间(不同账户)
        if max_trade_time:  # 数据库中有存量数据
            df = df[df['trade_time'] > max_trade_time]  # 过滤已导入的账单
        # 插入或更新数据库
        if not df.empty:
            _table_name = _mdl.__table__.name
            df.to_sql(_table_name, _engine, if_exists='append', index=False)  # 插入
            logger.info(f"->导入成功：{len(df)}")
        else:
            logger.info("->无需导入")
