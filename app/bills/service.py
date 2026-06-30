# -*- coding: utf-8 -*-
"""bills 应用业务函数（从 server_dj/apps/bills/api.py 迁移）。

迁移要点：
- 移除所有 Django 专用 import（django.conf.settings / django.db.models / django.db.transaction）
- settings 来源改为 server_fast.config.settings
- 模型来源改为 server_fast.app.bills.models
- Config 来源改为 server_fast.app.bills.config
- 通用工具改为 server_fast.common.utils
- Django ORM 的 _meta.db_table -> SQLAlchemy __table__.name
- Django ORM 的 _meta.fields -> SQLAlchemy __table__.columns
- Django ORM 的 objects.filter/aggregate/values 查询 -> SQLAlchemy session 查询
- pandas + settings.DB_ENGINE 逻辑（get_sql_to_df / upsert_df_to_db / df.to_sql）原样保留
"""
from datetime import date, datetime, timedelta
import logging
import re
import pandas as pd
import numpy as np
from gm.api import *
from sqlalchemy import text, select, func
from decimal import Decimal

from server_fast.config import settings
from server_fast.common.utils import (
    filter_in_cols, map_value, filter_dtypes, df_init_model,
    act_sql_engine, upsert_df_to_db, get_sql_to_df,
)
from server_fast.common.db import SessionLocal
from server_fast.app.bills.config import Config as BlsCfg
from server_fast.app.bills.models import (
    Bill, Profit, Group, GroupAcc, GroupSymbol, DailyValue, DailyAcc,
)

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 的 logger，输出到 stderr 不被缓冲

pd.set_option('future.no_silent_downcasting', True)

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


## 导入出入金
# 读取数据库中指定账户中某一交易类型最近的交易时间
def get_field_max_account_category_sql(account, category='出入金', field='trade_time', mdl=Bill):
    _engine = settings.DB_ENGINE
    sql = f'''
        SELECT MAX({field}) as max_field
        FROM {mdl.__table__.name}
        WHERE account='{account}'
        AND category='{category}'
    '''
    with _engine.connect() as conn:
        sql = text(sql)  # 用 text() 包装 SQL 字符串
        result = conn.execute(sql)  # 执行查询
        return result.fetchone()[0]  # 获取查询结果的第一行

# 根据文件名处理df列(出入金)
def handle_cols_io_from_file_name(df: pd.DataFrame, file_name: str) -> pd.DataFrame:
    if '出入金' in file_name:
        df['证券代码'] = '--'
        df['证券名称'] = '--'
        df['amount'] = 0.0
        if ('股票' in file_name):
            df['成交数量'] = 0.0
            df['成交均价'] = 0
            df['成交日期'] = pd.to_datetime(df['成交日期'].astype(str), format='%Y%m%d')
            return df
        elif ('期权' in file_name) and ('东财' in file_name):
            df['price'] = 0.0
            df['vol'] = 0
            df['币种'] = '人民币'  # 期权、期货、信用
            df['清算日期'] = pd.to_datetime(df['清算日期'].astype(str) + ' ' + df['发生时间'].astype(str))  # 股票
            return df
        elif ('期货' in file_name) and ('弘业' in file_name):
            df['price'] = 0.0
            df['vol'] = 0
            df['币种'] = '人民币'  # 期权、期货、信用
            df = df.rename(columns={'发生日期': '清算日期'})  # 期货
            df['amount_act'] = df['入金'] - df['出金']
            df.loc[df['amount_act'] > 0, '出入金类型'] = '入金'
            df.loc[df['amount_act'] < 0, '出入金类型'] = '出金'
            df.loc[df['说明'].str.contains('申报费'), '出入金类型'] = '申报费'
            return df
        else:
            logger.warning(f"文件名 {file_name} 无法识别账单类型")
            raise ValueError

# 规整和处理字段(出入金)
def handle_df_fields_io(df: pd.DataFrame) -> pd.DataFrame:
    _category_dict, _category1_dict = BlsCfg.to_exec_type()
    df['exec_type'] = df['exec_type'].str.replace(r'\s+', '', regex=True)  # 去除空格
    # 交易类别映射
    df['category'] = df['exec_type'].map(_category_dict)  # 交易类别映射
    df['category1'] = df['exec_type'].map(_category1_dict)  # 交易类别映射
    # 根据交易类别赋予symbol编号
    df.loc[df['category'] == '收益费用', 'symbol'] = 'SYFY'
    df.loc[df['category'] == '出入金', 'symbol'] = 'CRJ'
    return df

# 导入出入金数据
def insert_io_all_excel_sql():
    _engine = settings.DB_ENGINE
    _folder_io = BlsCfg.FOLDER_IO
    _mdl = Bill
    file_names = [
        f.name for f in _folder_io.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
        and (not f.name.startswith('~'))
    ]
    # 循环导入出入金文件
    for file_name in file_names:
        result = file_name.split('-')
        df = pd.read_excel(_folder_io / file_name)
        account = result[0]
        df['account'] = account
        max_trade_time = get_field_max_account_category_sql(account)
        logger.info(f"正在导入 {file_name}:{str(max_trade_time)}")
        # 根据文件名处理df列
        df = handle_cols_io_from_file_name(df, file_name)
        df = df.rename(columns=_mdl.map_fields())  # 外部列名映射数据库字段
        df['trade_time'] = pd.to_datetime(df['trade_time'])  # 转换时间格式
        df = df[filter_in_cols(df.columns, _mdl.db_fields())]  # 过滤字段
        # 规整和处理字段
        df = handle_df_fields_io(df)
        # 数据库中的最新时间(不同账户)
        if max_trade_time:  # 数据库中有存量数据
            df = df[df['trade_time'] > max_trade_time]  # 过滤已导入的账单
        if not df.empty:
            _table_name = _mdl.__table__.name
            df.to_sql(_table_name, _engine, if_exists='append', index=False)  # 插入
            logger.info(f"->导入成功：{len(df)}")
        else:
            logger.info("->无需导入")


## 汇总账单
# 更新账单中的代码
def update_symbol_bill_sql():
    _engine = settings.DB_ENGINE
    _update_dict = BlsCfg.MAP_SYMBOL  # 更新账单中的代码
    _mdl = Bill
    sql = f'''
        UPDATE {_mdl.__table__.name}
        SET symbol=:new_symbol
        WHERE symbol=:old_symbol
        '''
    params = [
        {'old_symbol': k, 'new_symbol': v} for k, v in _update_dict.items()
    ]
    result = act_sql_engine(_engine, sql, params)
    logger.info(f'更新代码:{result}')

# 删除汇总表中的旧代码
def del_old_symbol_group_sql():
    _engine = settings.DB_ENGINE
    _mdl = Group
    _update_dict = BlsCfg.MAP_SYMBOL  # 删除汇总表中的旧代码
    _keys_str = [f"'{item}'" for item in _update_dict.keys()]
    sql = f'''
        DELETE FROM {_mdl.__table__.name}
        WHERE symbol IN ({', '.join(_keys_str)})
        '''
    result = act_sql_engine(_engine, sql)
    logger.info(f'删除代码:{result}')

# 导出账单全部数据
def export_all_data_bill():
    _engine = settings.DB_ENGINE
    _mdl = Bill
    sql = f'''
        SELECT account,category,symbol,trade_time
        FROM {_mdl.__table__.name}
        '''
    df = get_sql_to_df(sql, _engine)
    df = df[~df['category'].str.contains('-')]  # 去掉包含-的数据
    return df

# 汇总资金余额
def upsert_group_cash_sql():
    _engine = settings.DB_ENGINE
    _mdl_group = Group
    _mdl_bill = Bill
    df = export_all_data_bill()
    # 按账户汇总数据(汇总资金余额用)
    group_df = df.groupby(
        by=['account']).agg(
        start_time=("trade_time", "min"),
        end_time=("trade_time", "max"),
        count=("trade_time", "count")
    )
    group_df['category'] = 'cash'
    group_df['symbol'] = 'cash'
    group_df.reset_index(inplace=True)  # 重置索引(索引变为列)
    group_df = group_df[filter_in_cols(group_df.columns, _mdl_group.db_fields())]
    _unique_keys = _mdl_group.unique_keys  # 唯一索引字段
    _table_name = _mdl_group.__table__.name
    result = upsert_df_to_db(group_df, _table_name, _engine, _unique_keys)
    logger.info(f'汇总资金:{result}')

# 汇总损益
def upsert_group_profit_sql():
    _engine = settings.DB_ENGINE
    _mdl_group = Group
    _mdl_bill = Bill
    df = export_all_data_bill()
    # 按账户、分类和标的汇总数据(汇总成本收益用)
    group_df = df.groupby(
        by=['account', 'category', 'symbol']).agg(
        start_time=("trade_time", "min"),
        end_time=("trade_time", "max"),
        count=("trade_time", "count")
    )
    group_df.reset_index(inplace=True)  # 重置索引(索引变为列)
    group_df = group_df[filter_in_cols(group_df.columns, _mdl_group.db_fields())]
    if not group_df.empty:  # 更新交易账单表
        _unique_keys = _mdl_group.unique_keys  # 唯一索引字段
        _table_name = _mdl_group.__table__.name
        result = upsert_df_to_db(group_df, _table_name, _engine, _unique_keys)
        logger.info(f'汇总收益:{result}')
    else:
        logger.info("->无需更新")


## 收益试算
# 买入开多
def util_buy_open_long(org: dict, dfd: dict) -> dict:
    org['p_long'] += abs(dfd['vol'])  # 多头持仓
    org['cost_t_long'] += abs(dfd['amount_act'])  # 多头总成本
    org['cost_u_long'] = round(org['cost_t_long'] / org['p_long'], 4)  # 单位成本
    org['pl_long'] = 0
    org['pl_short'] = 0
    return org

# 卖出开空
def util_sell_open_short(org: dict, dfd: dict) -> dict:
    org['p_short'] += abs(dfd['vol'])  # 空头持仓
    org['cost_t_short'] += abs(dfd['amount_act'])  # 多头总成本
    org['cost_u_short'] = round(org['cost_t_short'] / org['p_short'], 4)  # 单位成本
    org['pl_long'] = 0
    org['pl_short'] = 0
    return org

# 卖出平多
def util_sell_close_long(org: dict, dfd: dict) -> dict:
    org['p_long'] = round(org['p_long'] - abs(dfd['vol']), 0)  # 调整持仓
    if org['p_long'] > 0:  # 有持仓，当次收益为发生额减去单位成本与交易量折算的当成交易成本
        _cost_single = round(org['cost_u_long'] * abs(dfd['vol']), 2)  # 当次交易成本
    else:  # 无持仓，当次收益为0
        _cost_single = org['cost_t_long']  # 当次交易成本为剩余总成本
        org['cost_u_long'] = 0.0000  # 单位成本设置为0
    org['cost_t_long'] = round(org['cost_t_long'] - _cost_single, 2)  # 剩余总成本
    org['pl_long'] = round(abs(dfd['amount_act']) - _cost_single, 2)  # 平仓盈亏
    org['pl_short'] = 0
    return org

# 买入平空
def util_buy_close_short(org: dict, dfd: dict) -> dict:
    org['p_short'] = round(org['p_short'] - abs(dfd['vol']), 0)  # 调整持仓
    if org['p_short'] > 0:  # 有持仓，当次收益为发生额减去单位成本与交易量折算的当成交易成本
        _cost_single = round(org['cost_u_short'] * abs(dfd['vol']), 2)  # 当次交易成本
    else:  # 无持仓，当次收益为0
        _cost_single = org['cost_t_short']  # 当次交易成本为剩余总成本
        org['cost_u_short'] = 0.0000  # 单位成本设置为0
    org['cost_t_short'] = round(org['cost_t_short'] - _cost_single, 2)  # 剩余总成本
    org['pl_long'] = 0
    org['pl_short'] = round(_cost_single - abs(dfd['amount_act']), 2)  # 平仓盈亏
    return org

# 处理证券交易
def handle_securities_exec(org: dict, dfd: dict) -> dict:
    org['pl_ft'] = -abs(dfd['fee_tax'])  # 手续费及税费
    if dfd['category1'] == '买入':  # 调整持仓、总成本、单位成本，不确认损益。
        org = util_buy_open_long(org, dfd)
    elif dfd['category1'] == '卖出':  # 调整持仓、总成本，确认损益及总收益，不调整单位成本。
        org = util_sell_close_long(org, dfd)
    elif dfd['category1'] == '转入':
        org['p_long'] += abs(dfd['vol'])  # 持仓量
        org['diff_dwt'] += abs(dfd['amount'])  # 划转净额(***需要调整账单)
        org['cost_t_long'] += abs(dfd['amount'])  # 总成本(***需要调整账单)
        org['cost_u_long'] = round(org['cost_t_long'] / org['p_long'], 4)  # 单位成本
        org['pl_long'] = 0.00
    elif dfd['category1'] == '转出':
        org['p_long'] -= abs(dfd['vol'])
        org['diff_dwt'] -= abs(dfd['amount'])  # 划转净额(***需要调整账单)
        org['cost_t_long'] = round(org['cost_t_long'] - abs(dfd['amount']), 2)  # 剩余总成本
        org['pl_long'] = 0.00
    elif dfd['category1'] == '红利':
        if org['p_long'] > 0:  # 有持仓，调整持仓总成本、单位成本，不确认收益
            org['cost_t_long'] -= dfd['amount_act']  # 总成本(红利入账+,扣税-)
            org['cost_u_long'] = round(org['cost_t_long'] / org['p_long'], 4)  # 单位成本
            org['pl_long'] = 0
        else:  # 无持仓,确认收益及累计收益
            org['pl_long'] = dfd['amount_act']  # 当次收益
    elif dfd['category1'] == '红股':
        org['p_long'] += abs(dfd['vol'])  # 持仓量
        if org['p_long'] > 0:  # 有持仓,调整持仓单位成本
            org['cost_u_long'] = round(org['cost_t_long'] / org['p_long'], 4)  # 单位成本
            org['pl_long'] = 0.00  # 当次收益
        else:  # 无持仓,总成本与单位成本均设置为0
            org['pl_long'] = 0.00  # 当次收益
            org['cost_t_long'] = 0.00  # 单位成本
            org['cost_u_long'] = 0.0000  # 单位成本
    else:
        logger.warning("未知类型")
        raise ValueError
    return org.copy()

# 处理期货交易
def handle_futures_exec(org: dict, dfd: dict) -> dict:
    org['pl_ft'] = -abs(dfd['fee_tax'])  # 手续费及税费
    if '买' in dfd['b_s']:  # 买入
        if '开' in dfd['o_c']:  # 开多头
            org = util_buy_open_long(org, dfd)
        elif '平' in dfd['o_c']:  # 平空头
            org = util_buy_close_short(org, dfd)
    elif '卖' in dfd['b_s']:  # 卖出
        if '开' in dfd['o_c']:  # 开仓
            org = util_sell_open_short(org, dfd)
        elif '平' in dfd['o_c']:  # 平仓多头
            org = util_sell_close_long(org, dfd)
    else:
        logger.warning("未知类型")
        raise ValueError
    return org.copy()

# 买入开空
def util_buy_open_short(org: dict, dfd: dict) -> dict:
    org = util_sell_open_short(org, dfd)
    return org

# 卖出平空
def util_sell_close_short(org: dict, dfd: dict) -> dict:
    org['p_short'] = round(org['p_short'] - abs(dfd['vol']), 0)  # 调整持仓
    if org['p_short'] > 0:  # 有持仓，当次收益为发生额减去单位成本与交易量折算的当成交易成本
        _cost_single = round(org['cost_u_short'] * abs(dfd['vol']), 2)  # 当次交易成本
    else:  # 无持仓
        _cost_single = org['cost_t_short']  # 当次交易成本为剩余总成本
        org['cost_u_short'] = 0.0000  # 单位成本设置为0
    org['cost_t_short'] = round(org['cost_t_short'] - _cost_single, 2)  # 剩余总成本
    org['pl_short'] = round(abs(dfd['amount_act']) - _cost_single, 2)  # 平仓盈亏
    org['pl_long'] = 0
    return org

# 卖出开空(期权)
def util_sell_open_short_opt(org: dict, dfd: dict) -> dict:
    org['p_short'] += abs(dfd['vol'])  # 空头持仓
    org['cost_t_short'] -= abs(dfd['amount_act'])  # 多头总成本
    org['cost_u_short'] = round(org['cost_t_short'] / org['p_short'], 4)  # 单位成本
    org['pl_long'] = 0
    org['pl_short'] = 0
    return org

# 买入平空(期权)
def util_buy_close_short_opt(org: dict, dfd: dict) -> dict:
    org['p_short'] = round(org['p_short'] - abs(dfd['vol']), 0)  # 调整持仓
    if org['p_short'] > 0:  # 有持仓，当次收益为发生额减去单位成本与交易量折算的当成交易成本
        _cost_single = round(org['cost_u_short'] * abs(dfd['vol']), 2)  # 当次交易成本
    else:  # 无持仓，当次收益为0
        _cost_single = org['cost_t_short']  # 当次交易成本为剩余总成本
        org['cost_u_short'] = 0.0000  # 单位成本设置为0
    org['cost_t_short'] = round(org['cost_t_short'] - _cost_single, 2)  # 剩余总成本
    org['pl_long'] = 0
    org['pl_short'] = -round(_cost_single + abs(dfd['amount_act']), 2)  # 平仓盈亏
    return org

# 卖出开多(期权)
def util_sell_open_long_opt(org: dict, dfd: dict) -> dict:
    org['p_long'] += abs(dfd['vol'])  # 多头持仓
    org['cost_t_long'] -= abs(dfd['amount_act'])  # 多头总成本
    org['cost_u_long'] = round(org['cost_t_long'] / org['p_long'], 4)  # 单位成本
    org['pl_long'] = 0
    org['pl_short'] = 0
    return org

# 买入平多(期权)
def util_buy_close_long_opt(org: dict, dfd: dict) -> dict:
    org['p_long'] = round(org['p_long'] - abs(dfd['vol']), 0)  # 调整持仓
    if org['p_long'] > 0:  # 有持仓，当次收益为发生额减去单位成本与交易量折算的当成交易成本
        _cost_single = round(org['cost_u_long'] * abs(dfd['vol']), 2)  # 当次交易成本
    else:  # 无持仓，当次收益为0
        _cost_single = org['cost_t_long']  # 当次交易成本为剩余总成本
        org['cost_u_long'] = 0.0000  # 单位成本设置为0
    org['cost_t_long'] = round(org['cost_t_long'] - _cost_single, 2)  # 剩余总成本
    org['pl_long'] = -round(abs(dfd['amount_act']) + _cost_single, 2)  # 平仓盈亏
    org['pl_short'] = 0
    return org

# 处理期权交易
def handle_option_exec(org: dict, dfd: dict) -> dict:
    org['pl_ft'] = -abs(dfd['fee_tax'])  # 手续费及税费
    if ('C' in dfd['c_p']) or ('购' in dfd['c_p']):  # 认购
        if '买' in dfd['b_s']:  # 买入
            if '开' in dfd['o_c']:  # 开多
                org = util_buy_open_long(org, dfd)  # ok1
            elif '平' in dfd['o_c']:  # 平空
                org = util_buy_close_short_opt(org, dfd)  # ok2
        elif '卖' in dfd['b_s']:  # 卖出
            if '开' in dfd['o_c']:  # 开空
                org = util_sell_open_short_opt(org, dfd)  # ok2
            elif '平' in dfd['o_c']:  # 平多
                org = util_sell_close_long(org, dfd)  # ok1
    elif ('P' in dfd['c_p']) or ('沽' in dfd['c_p']):  # 认沽
        if '买' in dfd['b_s']:  # 买入
            if '开' in dfd['o_c']:  # 开空
                org = util_buy_open_short(org, dfd)  # ok3
            elif '平' in dfd['o_c']:  # 平多
                org = util_buy_close_long_opt(org, dfd)  # ok4
        elif '卖' in dfd['b_s']:  # 卖出
            if '开' in dfd['o_c']:  # 开多
                org = util_sell_open_long_opt(org, dfd)  # ok4
            elif '平' in dfd['o_c']:  # 平空
                org = util_sell_close_short(org, dfd)  # ok3
    else:
        logger.warning("未知类型")
        raise ValueError
    return org.copy()

# 处理理财
def handle_personal_finances(org: dict, dfd: dict) -> dict:
    '''
    赎回优先抵扣本金，多于计入收益。
    '''
    org['pl_ft'] = -abs(dfd['fee_tax'])  # 手续费及税费
    if dfd['category1'] in ['理财申购', '买入币']:
        org['cost_t_long'] += abs(dfd['amount_act'])  # 总成本
        org['cost_u_long'] = 0
        org['pl_long'] = 0
    elif dfd['category1'] in ['理财赎回', '卖出币']:
        org['cost_t_long'] -= abs(dfd['amount_act'])  # 总成本
        if org['cost_t_long'] < 0:  # 优先回收成本
            org['pl_long'] = abs(org['cost_t_long'])  # 当期收益为超出成本部分
            org['cost_t_long'] = 0  # 总成本设置为0
        else:  # 还未收回成本
            org['pl_long'] = 0
    else:
        logger.warning("未知类型")
        raise ValueError
    return org.copy()

# 处理出入金
def handle_deposit_withdrawal(org: dict, dfd: dict) -> dict:
    # 转入转出净额（负数表示出金总额超过入金总额）
    org['pl_ft'] = -abs(dfd['fee_tax'])  # 手续费及税费
    amount_act = abs(dfd['amount_act'])
    if dfd['category1'] == '入金':
        org['diff_dw'] += amount_act  # 转入转出净额
    elif dfd['category1'] == '出金':
        org['diff_dw'] -= amount_act  # 转入转出净额
    else:
        logger.warning("未知类型")
        raise ValueError
    return org.copy()

# 处理融资融券
def handle_securitie_margin(org: dict, dfd: dict) -> dict:
    '''
    借入总额，还款总额，剩余额度（不可为负），融资成本
    'diff_br':0,'pl_br':0, #借入总额，还款总额，余额，利息总额
    '''
    org['pl_ft'] = -abs(dfd['fee_tax'])  # 手续费及税费
    amount_act = abs(dfd['amount_act'])
    if dfd['category1'] == '融资借入':
        org['pl_br'] = 0.00  # 融资利息
        org['diff_br'] += amount_act  # 余额
    elif dfd['category1'] == '融资还款':
        org['pl_br'] = 0.00  # 融资利息
        org['diff_br'] -= amount_act  # 余额
    elif dfd['category1'] == '融资利息':
        org['pl_br'] = -amount_act  # 融资利息
    else:
        logger.warning("未知类型")
        raise ValueError
    return org.copy()

# 处理收益与费用
def handle_revenue_expenses(org: dict, dfd: dict) -> dict:
    # 收益费用累计额（负数表示累计其他费用大于累计其他收益）
    org['pl_ft'] = -abs(dfd['fee_tax'])  # 手续费及税费
    amount_act = abs(dfd['amount_act'])
    if dfd['category1'] == '其他收益':
        org['pl_other'] = amount_act  # 收益费用
    elif dfd['category1'] == '其他费用':
        org['pl_other'] = -amount_act  # 收益费用
    else:
        logger.warning("未知类型")
        raise ValueError
    return org.copy()

# 处理交易账单(全部,有问题)
def handle_trades_all(group_dict, df_bill, handle_dict, ll=[]):
    for i, bill in df_bill.iterrows():  # 逐行处理交易账单
        bill = bill.to_dict()
        handle_dict['bill_id'] = bill['id']
        # 处理交易类型
        try:
            if bill['category'] == '证券交易':
                handle_dict = handle_securities_exec(handle_dict, bill)
            elif bill['category'] == '期货交易':
                handle_dict = handle_futures_exec(handle_dict, bill)
            elif bill['category'] == '期权交易':
                handle_dict = handle_futures_exec(handle_dict, bill)
            elif bill['category'] == '理财':
                handle_dict = handle_personal_finances(handle_dict, bill)
            elif bill['category'] == '出入金':
                handle_dict = handle_deposit_withdrawal(handle_dict, bill)
            elif bill['category'] == '收益费用':
                handle_dict = handle_revenue_expenses(handle_dict, bill)
            elif bill['category'] == '融资融券':
                handle_dict = handle_securitie_margin(handle_dict, bill)
            elif bill['category'] == '-':
                logger.warning(f"忽略类型:{handle_dict['category']}")
                continue
            else:
                logger.warning(f"未知类型:{handle_dict['category']}")
                raise ValueError
        except Exception as e:
            logger.error(f"处理失败:{group_dict['account']}-{group_dict['symbol']}-{e}")
            raise e
        ll.append(handle_dict.copy())
        return ll

# 获取df最后一行组成新的df
def last_row_group_profit(df: pd.DataFrame, group_dict: dict, df_empty: pd.DataFrame) -> pd.DataFrame:
    last_row = df.iloc[-1]
    last_row_add = pd.Series(
        [group_dict['id'], group_dict['account'], group_dict['category'],
         group_dict['symbol'], group_dict['start_time'],
         group_dict['end_time'], group_dict['count'],
         group_dict['end_time']
         ],  ###
        index=[
            'id', 'account', 'category', 'symbol', 'start_time', 'end_time',
            'count', 'profit_time'
        ])
    last_row = pd.concat([last_row, last_row_add], ignore_index=False, axis=0)
    df_empty = pd.concat([df_empty, last_row.to_frame().T], ignore_index=True)
    df_empty = df_empty.fillna(0)
    df_empty['p_total'] = df_empty['p_long'] + df_empty['p_short']
    df_empty['cost_t_long'] = pd.to_numeric(df_empty['cost_t_long'], errors='coerce').fillna(0)
    df_empty['cost_t_short'] = pd.to_numeric(df_empty['cost_t_short'], errors='coerce').fillna(0)
    df_empty['cost_total'] = (df_empty['cost_t_long'] + df_empty['cost_t_short']).round(2)
    df_empty['pl_t_long'] = pd.to_numeric(df_empty['pl_t_long'], errors='coerce').fillna(0)
    df_empty['pl_t_short'] = pd.to_numeric(df_empty['pl_t_short'], errors='coerce').fillna(0)
    df_empty['pl_total'] = (df_empty['pl_t_long'] + df_empty['pl_t_short']).round(2)
    return df_empty

# 收益试算
def upsert_profit_group_sql():
    _engine = settings.DB_ENGINE
    _mdl_group = Group
    _mdl_bill = Bill
    _mdl_profit = Profit
    sql = f'''
        SELECT {','.join(_mdl_group.fields_pl)} FROM {_mdl_group.__table__.name}
        WHERE category<>'cash'
            AND (end_time<>profit_time OR profit_time IS NULL);
        '''
    group_list = (get_sql_to_df(sql, _engine)).to_dict('records')
    df_group = pd.DataFrame()  # 初始化df
    for group_dict in group_list:  # 逐组(代码)处理交易账单
        logger.info(f"{group_dict['account']}-{group_dict['category']}-{group_dict['symbol']}")
        sql = f'''
            SELECT {','.join(_mdl_bill.fields_pl)} FROM {_mdl_bill.__table__.name}
            WHERE account="{group_dict['account']}"
            AND category="{group_dict['category']}"
            AND symbol="{group_dict['symbol']}"
            '''
        df_bill = get_sql_to_df(sql, _engine)  # 按照账户类别和代码，查询交易账单

        df_bill = df_bill.fillna(0)
        df_bill = df_bill.astype(
            filter_dtypes(list(df_bill.columns), _mdl_bill.to_dtype()))
        handle_dict = {}
        for field in _mdl_profit.fields_pl_update:  # 将需要更新的字段初始化为0
            handle_dict[field] = 0.0
        ll = []
        for i, bill in df_bill.iterrows():  # 逐行处理交易账单
            bill = bill.to_dict()
            handle_dict['bill_id'] = bill['id']
            try:  # 处理交易类型
                if bill['category'] == '证券交易':
                    handle_dict = handle_securities_exec(handle_dict, bill)
                elif bill['category'] == '期货交易':
                    handle_dict = handle_futures_exec(handle_dict, bill)
                elif bill['category'] == '期权交易':
                    handle_dict = handle_option_exec(handle_dict, bill)
                elif bill['category'] == '理财':
                    handle_dict = handle_personal_finances(handle_dict, bill)
                elif bill['category'] == '虚拟币':
                    handle_dict = handle_personal_finances(handle_dict, bill)
                elif bill['category'] == '出入金':
                    handle_dict = handle_deposit_withdrawal(handle_dict, bill)
                elif bill['category'] == '收益费用':
                    handle_dict = handle_revenue_expenses(handle_dict, bill)
                elif bill['category'] == '融资融券':
                    handle_dict = handle_securitie_margin(handle_dict, bill)
                elif bill['category'] == '-':
                    logger.warning(f"忽略类型:{handle_dict['category']}")
                    continue
                else:
                    logger.warning(f"未知类型:{handle_dict['category']}")
                    raise ValueError
            except Exception as e:
                logger.error(f"处理失败:{group_dict['account']}-{group_dict['symbol']}-{e}")
                raise e
            ll.append(handle_dict.copy())
        df = pd.DataFrame(ll)  # 更新后的交易账单
        df['pl_t_long'] = df['pl_long'].cumsum()  # 累计多头盈亏
        df['pl_t_short'] = df['pl_short'].cumsum()  # 累计空头盈亏
        df['pl_t_other'] = df['pl_other'].cumsum()  # 累计其他损益
        df['pl_t_ft'] = df['pl_ft'].cumsum()  # 累计手续费及税费
        df['pl_t_br'] = df['pl_br'].cumsum()  # 融资利息
        if not df.empty:  # 更新交易账单表
            df_profit = df_init_model(df, _mdl_profit)
            _table = _mdl_profit.__table__.name
            _unique_keys = ['bill_id']  # 可以直接匹配id吧？？？
            result = upsert_df_to_db(
                df_profit, _table, _engine, _unique_keys)
            logger.info(f'->更新成功:{result}')
        df_group = last_row_group_profit(df, group_dict, df_group)
    logger.info("收益试算更新汇总表")
    if not df_group.empty:  # 更新bills_group表
        df_group = df_init_model(df_group, _mdl_group)
        _table = _mdl_group.__table__.name
        _unique_keys = _mdl_group.unique_keys
        _fields_update = _mdl_group.fields_pl_update
        result = upsert_df_to_db(
            df_group, _table, _engine, _unique_keys, _fields_update)
        logger.info(f'->更新成功:{result}')
    else:
        logger.info("->无需更新")

## 资金试算
# 获取df最后一行并添加到汇总表中
def last_row_group_cash(df: pd.DataFrame, group_dict: dict, df_empty: pd.DataFrame) -> pd.DataFrame:
    last_row = df.iloc[-1]
    last_row_add = pd.Series(
        [group_dict['id'], group_dict['account'], group_dict['category'],
         group_dict['symbol'], group_dict['start_time'],
         group_dict['end_time'], group_dict['count'],
         group_dict['end_time']
         ],  ###
        index=[
            'id', 'account', 'category', 'symbol', 'start_time', 'end_time',
            'count', 'profit_time'
        ])
    last_row = pd.concat([last_row, last_row_add], ignore_index=False, axis=0)
    df_empty = pd.concat([df_empty, last_row.to_frame().T], ignore_index=True)
    df_empty = df_empty.fillna(0)
    df_empty['cost_total'] = df_empty['cost_t_long']
    return df_empty

# 资金日结
def cash_acc_daily_group_sql(account):
    _mdl = Bill
    _engine = settings.DB_ENGINE
    sql = f'''
        SELECT trade_time, amount_act FROM {_mdl.__table__.name}
        WHERE account='{account}'
            AND category<>'-'
            AND (category1<>'转入' AND category1<>'转出')
        '''
    df = get_sql_to_df(sql, _engine)
    df = df.sort_values(by='trade_time')  # 按交易时间排序
    df['trade_time'] = df['trade_time'].dt.floor('D')  # 取交易日
    df = df.fillna(0)
    # 转换数据类型
    df = df.astype(
        filter_dtypes(list(df.columns), _mdl.to_dtype()))
    df.set_index('trade_time', inplace=True)
    daily_df = df.groupby(df.index).agg(
        cost_t_long=("amount_act", "sum")
    ).round(2)
    daily_df['cost_t_long'] = daily_df['cost_t_long'].cumsum().round(2)  # 累计和
    return daily_df

# 资金试算
def cash_update_group_sql():
    _engine = settings.DB_ENGINE
    _mdl_g = Group
    sql = f'''
        SELECT {','.join(_mdl_g.fields_pl)} FROM {_mdl_g.__table__.name}
        WHERE category='cash'
            AND (end_time<>profit_time OR profit_time IS NULL);
        '''
    group_list = (get_sql_to_df(sql, _engine)).to_dict('records')
    df_group = pd.DataFrame()  # 初始化df
    for group_dict in group_list:
        _account = group_dict['account']
        daily_df = cash_acc_daily_group_sql(_account)  # 获取账单数据
        df_group = last_row_group_cash(daily_df, group_dict, df_group)
    logger.info("资金试算更新汇总表")
    if not df_group.empty:  # 更新bills_group表
        df_group = df_init_model(df_group, _mdl_g, is_id=True)
        _table = _mdl_g.__table__.name
        _unique_keys = _mdl_g.unique_keys
        _fields_update = _mdl_g.fields_pl_update
        result = upsert_df_to_db(
            df_group, _table, _engine, _unique_keys, _fields_update)
        logger.info(f'->更新成功:{result}')
    else:
        logger.info("->无需更新")

# 实时市值
# 根据实时数据，计算持有市值与浮动盈亏
def value_float(df, current_data, multiplier):
    symbol_dict = {item['symbol']: item['price'] for item in current_data}
    df['price'] = df['symbol'].map(symbol_dict)
    df['multiplier'] = df['symbol'].apply(
        map_value, multiplier=multiplier)
    cdt = (df['p_long'] > 0 & df['price'].notna())  # 多头持仓市值=多头持仓数量*最新价格*乘数
    df.loc[cdt, 'value_long'] = (
        df.loc[cdt, 'price'] * df.loc[cdt, 'p_long'] * df.loc[cdt, 'multiplier']
    ).round(2)
    cdt = (df['p_short'] > 0 & df['price'].notna())  # 空头持仓市值=空头持仓数量*最新价格*乘数
    df.loc[cdt, 'value_short'] = (
        df.loc[cdt, 'price'] * df.loc[cdt, 'p_short'] * df.loc[cdt, 'multiplier']
    ).round(2)
    cdt = df['price'].isna()  # 没有价格信息，则市值=成本
    df.loc[cdt, 'value_long'] = df.loc[cdt, 'cost_t_long']
    df.loc[cdt, 'value_short'] = df.loc[cdt, 'cost_t_short']
    # 如果没有持仓，则市值=成本
    cdt = ((df['p_long'] <= 0) & (df['p_short'] <= 0))
    df.loc[cdt, 'value_long'] = df.loc[cdt, 'cost_t_long']
    df.loc[cdt, 'value_short'] = df.loc[cdt, 'cost_t_short']
    # 计算浮动盈亏
    df = df.fillna(0)
    df['value_total'] = (
        df['value_long'] + df['value_short']).round(2)
    df['pf_long'] = (
        df['value_long'] - df['cost_t_long']).round(2)
    df['pf_short'] = (
        df['cost_t_short'] - df['value_short']).round(2)
    df['pf_total'] = (
        df['pf_long'] + df['pf_short']).round(2)
    # 市值更新时间
    df['value_time'] = df['end_time']
    return df

# 更新市值和浮动盈亏
def value_float_em_sql():
    _engine = settings.DB_ENGINE
    _multiplier = BlsCfg.MAP_MULTIPLIER
    _mdl = Group
    sql = f'''
        SELECT {','.join(_mdl.fields_f)}
        FROM {_mdl.__table__.name}
        WHERE end_time<>value_time
            OR cost_total>0
            OR p_total>0
            OR value_time IS NULL;
        '''
    df = get_sql_to_df(sql, _engine)
    df = df.fillna(0)
    df = df.astype(filter_dtypes(list(df.columns), _mdl.to_dtype()))
    # 获取实时数据
    try:
        current_data = current(list(df['symbol']), fields=['symbol', 'price'])
    except Exception as e:
        logger.error(f'*****获取实时数据失败:{e}')
        raise e
    df = value_float(df, current_data, _multiplier)  # 计算市值与浮动盈亏
    # 更新bills_group表
    result = 0
    if not df.empty:
        _table = _mdl.__table__.name
        _unique_keys = ['id']
        _update_columns = _mdl.fields_f_update
        df = df_init_model(df, _mdl)
        result = upsert_df_to_db(
            df, _table, _engine, _unique_keys, _update_columns)
    else:
        logger.info("->无需更新")
    return result


# 账户汇总
# 取出汇总所有数据，汇总平仓盈亏，总盈亏
def get_group_sql_df():
    _engine = settings.DB_ENGINE
    _mdl = Group
    sql = f'''
        SELECT {','.join(_mdl.fields_api_details)}
        FROM bills_group
        ORDER BY account;
    '''
    df = get_sql_to_df(sql, _engine)
    df = df.fillna(0)
    df = df.astype(filter_dtypes(df.columns, _mdl.to_dtype()))
    df['pl_all'] = (
        df['pl_total'] + df['pl_t_other'] + df['pl_t_br'])
    df['pfl_all'] = (
        df['pl_all'] + df['pf_total'])
    return df

# 账户汇总
def upsert_group_acc_sql():
    _engine = settings.DB_ENGINE
    _mdl = GroupAcc
    _mdl_acc = DailyAcc
    # 获取Group数据，按account汇总
    df_g = get_group_sql_df()  # 获取数据
    df_acc = df_g.groupby('account').sum(numeric_only=True)
    df_acc['status'] = (  # 验证
        df_acc['cost_total'] - df_acc['pl_all'] -
        df_acc['diff_dw'] - df_acc['diff_dwt']
    ).round(0)
    # 提出现金、理财与市值合并(区分现金、理财和证券市值)
    df_cash_acc = df_g[df_g['category'] == 'cash']
    df_cash_acc = df_cash_acc.fillna(0)
    df_cash_acc = df_cash_acc.groupby('account').sum(numeric_only=True)
    df_acc['cash_acc'] = df_cash_acc['cost_total']
    df_fm_acc = df_g[df_g['category'] == '理财']
    df_fm_acc = df_fm_acc.fillna(0)
    df_fm_acc = df_fm_acc.groupby('account').sum(numeric_only=True)
    df_acc['fm_acc'] = df_fm_acc['cost_total']
    df_acc = df_acc.fillna(0)
    df_acc['cost_total'] = (  # 证券成本剔除现金、理财
        df_acc['cost_total'] - df_acc['cash_acc'] - df_acc['fm_acc'])
    df_acc['acc_aset'] = df_acc['value_total']  # 账户净值
    df_acc['value_total'] = (  # 证券市值剔除现金
        df_acc['value_total'] - df_acc['cash_acc'] - df_acc['fm_acc'])

    # 计算当日盈亏（Django ORM 查询已转为 SQLAlchemy session 查询）
    with SessionLocal() as session:
        # DailyAcc 最新交易日（原 .filter(daily_type='day').aggregate(Max('trade_date'))）
        max_trade_date = session.query(
            func.max(_mdl_acc.trade_date)
        ).filter(_mdl_acc.daily_type == 'day').scalar()
        # 取最新交易日各账户数据，排除 ALL（原 .filter().exclude(account='ALL').values(*fields)）
        stmt = select(*[getattr(_mdl_acc, f) for f in _mdl_acc.fields_group_acc]).where(
            _mdl_acc.daily_type == 'day',
            _mdl_acc.trade_date == max_trade_date,
            _mdl_acc.account != 'ALL',
        )
        result = session.execute(stmt)
        df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    df.set_index('account', inplace=True)
    df = df.astype(filter_dtypes(df.columns, _mdl_acc.to_dtype()))  # 转换数据类型
    df_acc = pd.concat([df_acc, df], axis=1)
    df_acc['pfl_day'] = (  # 当日盈亏=当日净值-上日净值
        df_acc['acc_aset'] - df_acc['daily_value']).round(0)

    # 求合计数
    new_row_df = df_acc.sum(
        numeric_only=True
    ).to_frame().T.set_index([['合计']])
    df_acc = pd.concat([df_acc, new_row_df])
    df_acc.index.names = ['account']  # 索引重命名

    df_acc.reset_index(inplace=True)
    result = 0
    if not df_acc.empty:
        df_in = df_init_model(df_acc, _mdl)
        _table = _mdl.__table__.name
        _unique_keys = ['account']
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys)
    return result

# 标的汇总
def upsert_group_symbol_sql():
    _engine = settings.DB_ENGINE
    _mdl = Group
    _mdl_symbol = GroupSymbol
    _mdl_daily_value = DailyValue
    # 获取Group数据（原 Django _meta.fields 改为 __table__.columns）
    sql = f'''
        SELECT {','.join([col.name for col in _mdl.__table__.columns])}
        FROM bills_group
        ORDER BY account;
    '''
    df_g = get_sql_to_df(sql, _engine)
    df_g = df_g.fillna(0)
    df_g = df_g.astype(filter_dtypes(df_g.columns, _mdl.to_dtype()))
    df_g['pl_all'] = (  # 平仓盈亏
        df_g['pl_total'] + df_g['pl_t_other'] + df_g['pl_t_br'])
    df_g['pfl_all'] = (  # 盈亏合计
        df_g['pl_all'] + df_g['pf_total'])

    # 计算当日盈亏（Django ORM 查询已转为 SQLAlchemy session 查询）
    with SessionLocal() as session:
        # 最新交易日（原 .aggregate(Max('trade_date'))）
        max_trade_date = session.query(
            func.max(_mdl_daily_value.trade_date)
        ).scalar()
        # 取最新交易日数据（原 .filter(trade_date=...).values(*fields)）
        stmt = select(*[getattr(_mdl_daily_value, f) for f in _mdl_daily_value.fields_group_symbol]).where(
            _mdl_daily_value.trade_date == max_trade_date,
        )
        result = session.execute(stmt)
        df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    df = df.astype(filter_dtypes(df.columns, _mdl_daily_value.to_dtype()))
    df = df.fillna(0)
    df['value_d_total'] = df['value_d_long'] + df['value_d_short']
    df_g_daily = df.groupby(['category', 'symbol']).sum(numeric_only=True)

    df_g_symbol = df_g.groupby(['category', 'symbol']).sum(numeric_only=True)
    df_symbol = pd.concat([df_g_daily, df_g_symbol], axis=1)
    df_symbol['pf_d_total'] = df_symbol['value_total'] - df_symbol['value_d_total']

    df_symbol.reset_index(inplace=True)
    result = 0
    if not df_symbol.empty:
        df_in = df_init_model(df_symbol, _mdl_symbol)
        _table = _mdl_symbol.__table__.name
        _unique_keys = _mdl_symbol.unique_keys
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys)
    return result
