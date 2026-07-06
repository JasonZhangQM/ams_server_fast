"""收益试算业务函数（从 bills/service.py 拆分）。"""

import logging

import pandas as pd

from server_fast.config import settings
from server_fast.common.utils import (
    filter_in_cols,
    filter_dtypes,
    df_init_model,
    upsert_df_to_db,
    get_sql_to_df,
)
from server_fast.app.bills.models import Bill, Group, Profit

logger = logging.getLogger("uvicorn.error")

pd.set_option('future.no_silent_downcasting', True)


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
