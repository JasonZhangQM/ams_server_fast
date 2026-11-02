"""汇总账单及账户/标的汇总业务函数（从 bills/service.py 拆分，并合并 account_summary）。"""

import logging

import numpy as np
import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import (
    filter_in_cols,
    filter_dtypes,
    df_init_model,
    upsert_df_to_db,
    get_sql_to_df,
    act_sql_engine,
)
from server_fast.app.bills.config import Config as BlsCfg
from server_fast.app.bills.models import Bill, Group, GroupAcc, GroupSymbol

logger = logging.getLogger("uvicorn.error")

# bills_group_symbol 的派生列：由 bills_group 聚合算出、每次重算都要覆盖。
# 不在此列的：underlying_symbol（人工归属列）；risk_exposure（人工值设在 Group，
# 由 Group 编辑联动刷新到本表，见 router._cascade_risk_exposure）。
# upsert_df_to_db 用 update_columns 显式限定，从机制上防覆盖。
# ⚠️ 重算只更新白名单里的派生列。
# **underlying_symbol / risk_exposure 永远不要加进这个列表**——一旦加进来，下一次同步
# 就会把人工值整体冲掉（risk_exposure 还会被 fillna(0) 变成 0），且不报错。
_DERIVED_COLS = [
    'count', 'p_total', 'cost_total', 'price', 'multiplier', 'value_total',
    'pf_total', 'pl_all', 'pfl_all', 'diff_br', 'diff_dw', 'diff_dwt',
]


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


## 账户与标的汇总
# 持仓归零清空风险敞口：人工设置的 risk_exposure 只对"还持有"的行有意义，
# 平仓离场（p_total=0）后残留的敞口值会污染 GroupSymbol / GroupAcc 汇总。
# 幂等（WHERE risk_exposure IS NOT NULL），清空后同口径联动刷新两张汇总表
# （与 router._cascade_risk_exposure 一致：按标的、按账户、合计行）。
def clear_zero_position_risk_sql():
    _engine = settings.DB_ENGINE
    # 1) 找出将被清空的行（先取键，清空后要按这些键联动刷新汇总表）
    sql_keys = '''
        SELECT account, category, symbol
        FROM bills_group
        WHERE p_total = 0 AND risk_exposure IS NOT NULL
        '''
    df_hit = get_sql_to_df(sql_keys, _engine)
    if df_hit.empty:
        logger.info('持仓归零清空风险敞口:无')
        return 0
    # 2) 清空（NULL=未设置，与人工清空同语义）
    sql_clear = '''
        UPDATE bills_group
        SET risk_exposure = NULL
        WHERE p_total = 0 AND risk_exposure IS NOT NULL
        '''
    cleared = act_sql_engine(_engine, sql_clear)
    logger.info(f'持仓归零清空风险敞口:{cleared}')
    # 3) 联动刷新受影响的 GroupSymbol（按 category+symbol 求和）
    pairs = list({(c, s) for c, s in zip(df_hit['category'], df_hit['symbol'])})
    pairs_str = ', '.join(f"('{c}','{s}')" for c, s in pairs)
    sql_gs = f'''
        UPDATE bills_group_symbol gs
        JOIN (SELECT category, symbol, SUM(risk_exposure) AS s
              FROM bills_group
              WHERE (category, symbol) IN ({pairs_str})
              GROUP BY category, symbol) t
          ON gs.category = t.category AND gs.symbol = t.symbol
        SET gs.risk_exposure = t.s
        '''
    act_sql_engine(_engine, sql_gs)
    # 4) 联动刷新受影响的 GroupAcc（按 account 求和）与合计行（全账户求和）
    accounts = list(df_hit['account'].unique())
    acc_str = ', '.join(f"'{a}'" for a in accounts)
    sql_ga = f'''
        UPDATE bills_group_acc ga
        JOIN (SELECT account, SUM(risk_exposure) AS s
              FROM bills_group
              WHERE account IN ({acc_str})
              GROUP BY account) t
          ON ga.account = t.account
        SET ga.risk_exposure = t.s
        '''
    act_sql_engine(_engine, sql_ga)
    sql_total = '''
        UPDATE bills_group_acc ga
        JOIN (SELECT SUM(risk_exposure) AS s FROM bills_group) t
        SET ga.risk_exposure = t.s
        WHERE ga.account = '合计'
        '''
    act_sql_engine(_engine, sql_total)
    return cleared

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
    # 获取Group数据，按account汇总
    df_g = get_group_sql_df()  # 获取数据
    df_acc = df_g.groupby('account').sum(numeric_only=True)
    df_acc['status'] = (  # 验证（融资借入亦为资金来源，需计入 diff_br）
        df_acc['cost_total'] - df_acc['pl_all'] -
        df_acc['diff_dw'] - df_acc['diff_dwt'] - df_acc['diff_br']
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
    df_acc['acc_aset'] = df_acc['value_total']  # 账户价值（未扣融资负债）
    df_acc['value_total'] = (  # 证券市值剔除现金、理财
        df_acc['value_total'] - df_acc['cash_acc'] - df_acc['fm_acc'])

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
    # 获取Group数据
    sql = f'''
        SELECT {','.join([col.name for col in _mdl.__table__.columns])}
        FROM bills_group
        ORDER BY account;
    '''
    df_g = get_sql_to_df(sql, _engine)
    # ⚠️ 丢掉 Group 的 risk_exposure（人工列）：GroupSymbol 同名列由 Group 编辑联动
    # 维护（router._cascade_risk_exposure）。留在 df 里会被 fillna(0) 把「未设置」
    # 变成 0，且新行 INSERT 时写入的 0 与联动的 NULL 语义冲突
    df_g = df_g.drop(columns=['risk_exposure'], errors='ignore')
    df_g = df_g.fillna(0)
    df_g = df_g.astype(filter_dtypes(df_g.columns, _mdl.to_dtype()))
    df_g['pl_all'] = (  # 平仓盈亏
        df_g['pl_total'] + df_g['pl_t_other'] + df_g['pl_t_br'])
    df_g['pfl_all'] = (  # 盈亏合计
        df_g['pl_all'] + df_g['pf_total'])

    # 平仓行（p_total=0）的 price 已被 value_calc 冻结在最后一次重算时点
    # （行筛选 end_time<>value_time OR p_total>0 OR value_time IS NULL：平仓后
    # 不再进清单），聚合前必须排除，否则历史高价会被 MAX 钉进 GroupSymbol.price
    # （实测 2026-11-02：513100 钉在 2.4000 vs 在仓 2.2390、300750 钉在 298.38
    # vs 232.92；600036 为反向案例——现价高于冻结价时恰好显示"对"，纯运气，
    # 价格回落即钉在高位）。mask 后 groupby.max 跳过 NaN：无在仓行的标的
    # 聚合得 NaN → upsert_df_to_db 写 NULL（前端显示 '-'），已清仓的标的
    # 本就不该显示一个陈旧的"最新价"。
    df_g.loc[df_g['p_total'] == 0, 'price'] = np.nan
    df_symbol = df_g.groupby(['category', 'symbol']).sum(numeric_only=True)
    # price/multiplier 同一标的应一致，取 max 覆盖 sum 累加值（剔除 fillna(0) 空值）。
    # price 只聚合在仓行（平仓行已置 NaN）；multiplier 不 mask：乘数不随行情
    # 变化，平仓行的值同样有效
    df_symbol['price'] = df_g.groupby(['category', 'symbol'])['price'].max()
    df_symbol['multiplier'] = df_g.groupby(['category', 'symbol'])['multiplier'].max()

    df_symbol.reset_index(inplace=True)
    result = 0
    if not df_symbol.empty:
        df_in = df_init_model(df_symbol, _mdl_symbol)
        _table = _mdl_symbol.__table__.name
        _unique_keys = _mdl_symbol.unique_keys
        # 显式白名单：只更新派生列，绝不碰人工/联动列。
        # ⚠️ 白名单外的是 underlying_symbol（人工归属）与 risk_exposure（人工值设在
        # Group、编辑联动刷新）——默认写法「更新除唯一键外所有列」在 df 多带一列时
        # 会把它们静默冲掉，而 underlying_symbol 被清空后不会有任何报错，
        # 只会在标的页悄悄少掉几十笔成交。
        result = upsert_df_to_db(
            df_in, _table, _engine, _unique_keys, update_columns=_DERIVED_COLS)
    return result
