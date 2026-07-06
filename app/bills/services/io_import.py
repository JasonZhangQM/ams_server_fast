# -*- coding: utf-8 -*-
"""导入出入金业务函数（从 bills/service.py 拆分）。"""
import logging
import pandas as pd
from sqlalchemy import text
from server_fast.config import settings
from server_fast.common.utils import filter_in_cols
from server_fast.app.bills.config import Config as BlsCfg
from server_fast.app.bills.models import Bill

logger = logging.getLogger("uvicorn.error")


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
