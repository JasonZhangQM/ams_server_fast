# -*- coding: utf-8 -*-
"""bds 应用业务函数（从 server_dj/apps/bds/api.py 迁移）。

迁移要点：
- 移除 Django 专用 import（django.conf、django.db）。
- settings 改从 server_fast.config 导入；工具函数与模型改用 server_fast 路径。
- Django 的 _mdl._meta.db_table 改为 SQLAlchemy 的 _mdl.__table__.name。
- 保留 pandas + settings.DB_ENGINE 逻辑（不改为 session）。
"""
import pandas as pd
import numpy as np
import logging
from sqlalchemy import func
from gm.api import *
from datetime import datetime, date, timedelta
from server_fast.config import settings
from server_fast.common.utils import *
from server_fast.common.db import SessionLocal
from server_fast.app.bds.config import Config as dbsCfg
from server_fast.app.bds.models import *

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 的 logger，输出到 stderr 不被缓冲


# 获取交易日历并存入数据库
def insert_trade_date_em_sql():
    '''
    获取交易日历并存入数据库
    field = 'trade_date'
    mdl = TradeDate

    返回值：新增的交易日数量（int），0 表示无需更新
    '''
    _engine = settings.DB_ENGINE
    _field = 'trade_date'
    _mdl = TradeDate
    logger.info("交易日历获取并导入")
    max_date = get_field_max_sql(_field,_mdl,_engine)
    today_year = datetime.today().year
    if max_date is None:
        max_year = 1991
    else:
        max_year = max_date.year
    if max_year >= today_year:
        logger.info("->已经有最新数据，无需调取接口")
        return 0
    df = get_trading_dates_by_year( #调取em接口获取交易日历
        'SHSE', max_year, datetime.today().year)
    df = df[df['trade_date']!='']
    if max_date:
        df = df[df['trade_date']>str(max_date)]
    if not df.empty: # 交易日数据不为空
        df = df_init_model(df,_mdl) # 过滤字段
        _table = _mdl.__table__.name  # SQLAlchemy 表名（原 Django: _meta.db_table）
        df.to_sql(_table,_engine,if_exists='append', index=False)
        logger.info(f"->成功 {len(df)}")
        return len(df)
    else:
        logger.info("->无需导入")
        return 0

# 导入证券基本信息(东财)
def upsert_symbol_info_excel_sql():
    _engine = settings.DB_ENGINE
    _mdl = SymbolInfo
    _folder = dbsCfg.FOLDER_SYMBOL
    _map_market_code = dbsCfg.MAP_MARKET_CODE
    _fields_replace = _mdl.fields_replace

    logger.info("证券基本信息导入")
    file_names = [
        f.name for f in _folder.iterdir()
        if f.is_file() and f.suffix in ['.xlsx']
            and (not f.name.startswith('~'))
            ]
    for file_name in file_names:
        df = pd.read_excel(_folder/file_name,dtype=str)
        df = df.map( # 去除空格
            lambda x: x.strip() if isinstance(x, str) else x)
        df.columns = df.columns.str.strip()  # 去除列名首尾空格（如 ' 所属行业' → '所属行业'）
        df = df.rename(columns=_mdl.map_fields()) # 外部列名映射数据库字段
        df = df[filter_in_cols(df.columns,_mdl.db_fields())] # 过滤字段
        df['symbol'] = df['symbol'].map( # 代码市场映射
            lambda x: _map_market_code[x[0]])+'.'+df['symbol']
        df[_fields_replace] = df[_fields_replace].replace('—', np.nan)
        df = df.astype(filter_dtypes(df.columns,_mdl.to_dtype())) # 转换数据类型
        if not df.empty:
            df = df.replace({np.nan: None})
            _table = _mdl.__table__.name  # SQLAlchemy 表名（原 Django: _meta.db_table）
            _unique_keys = _mdl.unique_keys
            result = upsert_df_to_db(df, _table, _engine, _unique_keys)
            logger.info(f"->成功：{result}")
        else:
            logger.info(f"->无需导入：{file_name}")


# 循环获取 INDEX_CODE 中所有指数的历史行情并 upsert 入库
def upsert_index_history_sql():
    """循环获取 INDEX_CODE 中所有指数的历史行情并 upsert 入库。

    增量更新策略：
    - 数据库已有该 symbol 数据：从最新日期 + 1 天开始获取
    - 数据库无该 symbol 数据：从 listed_date 开始全量获取

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的获取条数。
    """
    _engine = settings.DB_ENGINE
    _mdl = IndexHistory
    _index_code = dbsCfg.INDEX_CODE
    steps = {}  # 记录每个 symbol 的获取条数
    logger.info("指数历史行情获取并导入")
    for symbol, info in _index_code.items():
        try:
            # 查询数据库中该 symbol 的最新 trade_date
            with SessionLocal() as session:
                row = (
                    session.query(_mdl.trade_date)
                    .filter(_mdl.symbol == symbol)
                    .order_by(_mdl.trade_date.desc())
                    .first()
                )
            max_date = row[0] if row else None
            # 增量更新起点：有数据则从最新日期 + 1 天，否则从 listed_date 全量获取
            if max_date is not None:
                start_time = max_date + timedelta(days=1)
            else:
                start_time = info['listed_date']
            end_time = date.today()
            # 调用 gm 接口获取历史行情（带超时保护，防止 gm 终端未启动时阻塞）
            df = call_with_timeout(history, timeout=30)(
                symbol=symbol,
                frequency='1d',
                start_time=start_time,
                end_time=end_time,
                fields='eob,symbol,open,high,low,close',
                adjust=ADJUST_NONE,
                df=True,
            )
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            # eob（datetime）转为 date 并重命名为 trade_date
            df['eob'] = pd.to_datetime(df['eob']).dt.date
            df = df.rename(columns={'eob': 'trade_date'})
            df = df_init_model(df, _mdl)  # 清洗、列名映射、字段过滤、类型转换
            if not df.empty:
                df = df.replace({np.nan: None})
                _table = _mdl.__table__.name
                _unique_keys = _mdl.unique_keys
                result = upsert_df_to_db(df, _table, _engine, _unique_keys)
                logger.info(f"->{symbol} 成功：{result}")
                steps[symbol] = len(df)
            else:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps


# 循环获取 INDEX_CODE 中所有指数指定日期的成分股并追加入库
def upsert_index_constituent_sql(trade_date=None):
    """循环获取 INDEX_CODE 中所有指数指定日期的成分股并追加入库。

    同步策略：
    - trade_date 为 None：获取每个指数最新交易日的成分股
    - trade_date 指定：获取每个指数该日期的成分股

    成分股变更检测：若当前获取的成分股集合与数据库中该 index_code 最新已保存
    交易日的成分股集合一致，则跳过不保存，避免存储无变化的冗余快照。

    单个 index_code 失败不中断后续步骤，返回 steps 字典记录每个 index_code 的结果
    （1=已保存，0=未变化或空数据跳过，-1=失败）。
    """
    _engine = settings.DB_ENGINE
    _mdl = IndexConstituent
    _index_code = dbsCfg.INDEX_CODE
    steps = {}  # 记录每个 index_code 的保存结果
    logger.info("指数成分股获取并导入")

    def _normalize_weight(w):
        """权重归一化：NaN/None 统一转 0.0，其余保留 4 位小数。"""
        if w is None or pd.isna(w):
            return 0.0
        return round(float(w), 4)

    # trade_date 转字符串供 API 使用，None 表示获取最新交易日数据
    trade_date_str = trade_date.strftime('%Y-%m-%d') if trade_date else None

    for index_code, info in _index_code.items():
        try:
            # ---- 查询数据库中该 index_code 最新已保存的 trade_date 及其成分股集合 ----
            with SessionLocal() as session:
                row = (
                    session.query(_mdl.trade_date)
                    .filter(_mdl.index_code == index_code)
                    .order_by(_mdl.trade_date.desc())
                    .first()
                )
                max_date = row[0] if row else None

                if max_date is not None:
                    last_rows = (
                        session.query(_mdl.symbol, _mdl.weight)
                        .filter(
                            _mdl.index_code == index_code,
                            _mdl.trade_date == max_date,
                        )
                        .all()
                    )
                    last_saved_set = {
                        (r.symbol, _normalize_weight(r.weight)) for r in last_rows
                    }
                else:
                    last_saved_set = set()

            # ---- 调用 gm 接口获取成分股（带超时保护，防止 gm 终端未启动时阻塞） ----
            df = call_with_timeout(stk_get_index_constituents, timeout=30)(
                index=index_code,
                trade_date=trade_date_str,
            )
            # API 返回空 DataFrame 时跳过（非交易日或数据尚未更新）
            if df is None or df.empty:
                logger.info(f"->{index_code} API 返回空数据，跳过")
                steps[index_code] = 0
                continue

            # 仅取需要的四列
            df = df[['index', 'symbol', 'weight', 'trade_date']]
            # 构建当前成分股集合用于变更检测
            current_set = set(zip(
                df['symbol'],
                df['weight'].apply(_normalize_weight),
            ))
            # 成分股集合未变化则跳过不保存
            if current_set == last_saved_set:
                logger.info(f"->{index_code} 成分股未变化，跳过")
                steps[index_code] = 0
                continue

            # 重命名列 index → index_code（index 为 SQL 保留字）
            df = df.rename(columns={'index': 'index_code'})
            # 转换 trade_date 为 date 类型（API 返回字符串格式 %Y-%m-%d）
            df['trade_date'] = pd.to_datetime(
                df['trade_date'], format='%Y-%m-%d'
            ).dt.date
            # weight 转 float，NaN 统一转 0
            df['weight'] = df['weight'].apply(_normalize_weight)
            # 追加写入数据库
            df.to_sql(_mdl.__table__.name, _engine, if_exists='append', index=False)

            logger.info(f"->{index_code} 成功保存")
            steps[index_code] = 1
        except Exception as e:
            # 单步失败不中断后续 index_code，记录错误并继续
            logger.error(f"->{index_code} 失败：{str(e)}")
            steps[index_code] = -1
    return steps


# 资产负债表 20 个财务字段（英文逗号分隔，供 gm 接口 fields 参数使用）
FUND_BALANCE_FIELDS = (
    "mny_cptl,acct_rcv,invt,ttl_cur_ast,fix_ast,lt_eqy_inv,intg_ast,gw,"
    "ttl_ncur_ast,ttl_ast,sht_ln,acct_pay,ttl_cur_liab,lt_ln,ttl_ncur_liab,"
    "ttl_liab,cptl_rsv,ret_prof,ttl_eqy_pcom,ttl_eqy"
)


def _clean_scalar(v):
    """将 pandas NaN 统一转为 None，适配数据库空值存储。"""
    if v is None or pd.isna(v):
        return None
    return v


def _to_date(v):
    """将字符串/datetime 统一转为 date 类型，空值返回 None。"""
    if v is None or pd.isna(v):
        return None
    if isinstance(v, date):
        return v
    return pd.to_datetime(v).date()


def _fetch_fund_balance_batched(symbol, start_date, end_date):
    """分批获取资产负债表数据（gm API 限制 fields 不超过 20 个）。

    将 23 个字段按 20 个一批分别请求，再以元数据列为 key 合并，
    返回包含全部字段的完整 DataFrame。
    """
    all_fields = FUND_BALANCE_FIELDS.split(",")
    batch_size = 20
    dfs = []
    for i in range(0, len(all_fields), batch_size):
        batch_fields = ",".join(all_fields[i:i + batch_size])
        df = call_with_timeout(stk_get_fundamentals_balance, timeout=30)(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            fields=batch_fields,
            df=True,
        )
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]
    # 多批数据按元数据列合并（同一 symbol+日期范围返回的行一致）
    merge_cols = ["symbol", "pub_date", "rpt_date", "rpt_type", "data_type"]
    result = dfs[0]
    for df in dfs[1:]:
        result = result.merge(df, on=merge_cols, how="outer")
    return result


# 循环获取指定股票列表的资产负债表数据并 upsert 入库
def upsert_fund_balance_sql(symbols):
    """循环获取指定股票列表的资产负债表数据并 upsert 入库。

    增量更新策略：
    - 数据库已有该 symbol 数据：从最新 rpt_date + 1 天开始获取
    - 数据库无该 symbol 数据：全量获取（start_date=None）

    去重规则（按 symbol + rpt_date）：
    - 已有记录且新数据 pub_date >= 已有 pub_date：更新所有字段
    - 已有记录但新数据 pub_date < 已有 pub_date：跳过该行
    - 无已有记录：插入新记录

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的保存条数。
    """
    _mdl = FundBalance
    _field_list = FUND_BALANCE_FIELDS.split(",")  # 23 个财务字段名列表
    steps = {}  # 记录每个 symbol 的保存条数
    logger.info("资产负债表数据获取并导入")
    end_date = datetime.now().strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            # 查询数据库中该 symbol 的最大 rpt_date，用于增量更新
            with SessionLocal() as db:
                max_rpt_date = (
                    db.query(func.max(_mdl.rpt_date))
                    .filter(_mdl.symbol == symbol)
                    .scalar()
                )
            # 增量起点：有数据则从最新 rpt_date + 1 天，否则从 2010-01-01 全量获取
            if max_rpt_date is not None:
                start_date = (max_rpt_date + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = "2010-01-01"
            # 调用 gm 接口获取资产负债表（分批获取，带超时保护）
            df = _fetch_fund_balance_batched(symbol, start_date, end_date)
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            saved_count = 0
            with SessionLocal() as db:
                for _, row in df.iterrows():
                    rpt_date = _to_date(row.get("rpt_date"))
                    if rpt_date is None:
                        continue
                    pub_date = _to_date(row.get("pub_date"))
                    # 查询是否已有该 (symbol, rpt_date) 记录
                    existing = (
                        db.query(_mdl)
                        .filter(_mdl.symbol == symbol, _mdl.rpt_date == rpt_date)
                        .first()
                    )
                    if existing is not None:
                        # 已有记录：新数据 pub_date 更旧则跳过，否则更新所有字段
                        if (pub_date is not None and existing.pub_date is not None
                                and pub_date < existing.pub_date):
                            continue
                        existing.pub_date = pub_date
                        existing.rpt_type = _clean_scalar(row.get("rpt_type"))
                        existing.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(existing, f, _clean_scalar(row.get(f)))
                    else:
                        # 无已有记录：插入新记录
                        obj = _mdl(symbol=symbol, rpt_date=rpt_date)
                        obj.pub_date = pub_date
                        obj.rpt_type = _clean_scalar(row.get("rpt_type"))
                        obj.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(obj, f, _clean_scalar(row.get(f)))
                        db.add(obj)
                    saved_count += 1
                    # 每 100 条 commit 一次
                    if saved_count % 100 == 0:
                        db.commit()
                db.commit()  # 提交剩余记录
            logger.info(f"->{symbol} 成功：{saved_count}")
            steps[symbol] = saved_count
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps


# 利润表 20 个财务字段（英文逗号分隔，供 gm 接口 fields 参数使用）
FUND_INCOME_FIELDS = (
    "ttl_inc_oper,inc_oper,ttl_cost_oper,cost_oper,exp_sell,exp_adm,exp_rd,exp_fin,"
    "inc_inv,inc_fv_chg,oper_prof,ttl_prof,inc_tax,net_prof,net_prof_pcom,"
    "eps_base,eps_dil,inc_noper,exp_noper,ttl_comp_inc"
)


def _fetch_fund_income_batched(symbol, start_date, end_date):
    """分批获取利润表数据（gm API 限制 fields 不超过 20 个）。

    将 20 个字段按 20 个一批分别请求，再以元数据列为 key 合并，
    返回包含全部字段的完整 DataFrame。
    """
    all_fields = FUND_INCOME_FIELDS.split(",")
    batch_size = 20
    dfs = []
    for i in range(0, len(all_fields), batch_size):
        batch_fields = ",".join(all_fields[i:i + batch_size])
        df = call_with_timeout(stk_get_fundamentals_income, timeout=30)(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            fields=batch_fields,
            df=True,
        )
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]
    # 多批数据按元数据列合并（同一 symbol+日期范围返回的行一致）
    merge_cols = ["symbol", "pub_date", "rpt_date", "rpt_type", "data_type"]
    result = dfs[0]
    for df in dfs[1:]:
        result = result.merge(df, on=merge_cols, how="outer")
    return result


# 循环获取指定股票列表的利润表数据并 upsert 入库
def upsert_fund_income_sql(symbols):
    """循环获取指定股票列表的利润表数据并 upsert 入库。

    增量更新策略：
    - 数据库已有该 symbol 数据：从最新 rpt_date + 1 天开始获取
    - 数据库无该 symbol 数据：全量获取（start_date=None）

    去重规则（按 symbol + rpt_date）：
    - 已有记录且新数据 pub_date >= 已有 pub_date：更新所有字段
    - 已有记录但新数据 pub_date < 已有 pub_date：跳过该行
    - 无已有记录：插入新记录

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的保存条数。
    """
    _mdl = FundIncome
    _field_list = FUND_INCOME_FIELDS.split(",")  # 20 个财务字段名列表
    steps = {}  # 记录每个 symbol 的保存条数
    logger.info("利润表数据获取并导入")
    end_date = datetime.now().strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            # 查询数据库中该 symbol 的最大 rpt_date，用于增量更新
            with SessionLocal() as db:
                max_rpt_date = (
                    db.query(func.max(_mdl.rpt_date))
                    .filter(_mdl.symbol == symbol)
                    .scalar()
                )
            # 增量起点：有数据则从最新 rpt_date + 1 天，否则从 2010-01-01 全量获取
            if max_rpt_date is not None:
                start_date = (max_rpt_date + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = "2010-01-01"
            # 调用 gm 接口获取利润表（分批获取，带超时保护）
            df = _fetch_fund_income_batched(symbol, start_date, end_date)
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            saved_count = 0
            with SessionLocal() as db:
                for _, row in df.iterrows():
                    rpt_date = _to_date(row.get("rpt_date"))
                    if rpt_date is None:
                        continue
                    pub_date = _to_date(row.get("pub_date"))
                    # 查询是否已有该 (symbol, rpt_date) 记录
                    existing = (
                        db.query(_mdl)
                        .filter(_mdl.symbol == symbol, _mdl.rpt_date == rpt_date)
                        .first()
                    )
                    if existing is not None:
                        # 已有记录：新数据 pub_date 更旧则跳过，否则更新所有字段
                        if (pub_date is not None and existing.pub_date is not None
                                and pub_date < existing.pub_date):
                            continue
                        existing.pub_date = pub_date
                        existing.rpt_type = _clean_scalar(row.get("rpt_type"))
                        existing.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(existing, f, _clean_scalar(row.get(f)))
                    else:
                        # 无已有记录：插入新记录
                        obj = _mdl(symbol=symbol, rpt_date=rpt_date)
                        obj.pub_date = pub_date
                        obj.rpt_type = _clean_scalar(row.get("rpt_type"))
                        obj.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(obj, f, _clean_scalar(row.get(f)))
                        db.add(obj)
                    saved_count += 1
                    # 每 100 条 commit 一次
                    if saved_count % 100 == 0:
                        db.commit()
                db.commit()  # 提交剩余记录
            logger.info(f"->{symbol} 成功：{saved_count}")
            steps[symbol] = saved_count
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps


# 现金流量表 20 个财务字段（英文逗号分隔，供 gm 接口 fields 参数使用）
FUND_CASHFLOW_FIELDS = (
    "cash_rcv_sale,cf_in_oper,cash_pur_gds_svc,cash_pay_emp,cash_pay_tax,cf_out_oper,"
    "net_cf_oper,cash_rcv_sale_inv,cf_in_inv,pur_fix_intg_ast,net_cf_inv,brw_rcv,"
    "cf_in_fin,cash_rpay_brw,net_cf_fin,net_prof,efct_er_chg_cash,net_incr_cash_eq,"
    "cash_cash_eq_bgn,cash_cash_eq_end"
)


def _fetch_fund_cashflow_batched(symbol, start_date, end_date):
    """分批获取现金流量表数据（gm API 限制 fields 不超过 20 个）。

    将 20 个字段按 20 个一批分别请求，再以元数据列为 key 合并，
    返回包含全部字段的完整 DataFrame。
    """
    all_fields = FUND_CASHFLOW_FIELDS.split(",")
    batch_size = 20
    dfs = []
    for i in range(0, len(all_fields), batch_size):
        batch_fields = ",".join(all_fields[i:i + batch_size])
        df = call_with_timeout(stk_get_fundamentals_cashflow, timeout=30)(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            fields=batch_fields,
            df=True,
        )
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]
    # 多批数据按元数据列合并（同一 symbol+日期范围返回的行一致）
    merge_cols = ["symbol", "pub_date", "rpt_date", "rpt_type", "data_type"]
    result = dfs[0]
    for df in dfs[1:]:
        result = result.merge(df, on=merge_cols, how="outer")
    return result


# 循环获取指定股票列表的现金流量表数据并 upsert 入库
def upsert_fund_cashflow_sql(symbols):
    """循环获取指定股票列表的现金流量表数据并 upsert 入库。

    增量更新策略：
    - 数据库已有该 symbol 数据：从最新 rpt_date + 1 天开始获取
    - 数据库无该 symbol 数据：全量获取（start_date=None）

    去重规则（按 symbol + rpt_date）：
    - 已有记录且新数据 pub_date >= 已有 pub_date：更新所有字段
    - 已有记录但新数据 pub_date < 已有 pub_date：跳过该行
    - 无已有记录：插入新记录

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的保存条数。
    """
    _mdl = FundCashflow
    _field_list = FUND_CASHFLOW_FIELDS.split(",")  # 20 个财务字段名列表
    steps = {}  # 记录每个 symbol 的保存条数
    logger.info("现金流量表数据获取并导入")
    end_date = datetime.now().strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            # 查询数据库中该 symbol 的最大 rpt_date，用于增量更新
            with SessionLocal() as db:
                max_rpt_date = (
                    db.query(func.max(_mdl.rpt_date))
                    .filter(_mdl.symbol == symbol)
                    .scalar()
                )
            # 增量起点：有数据则从最新 rpt_date + 1 天，否则从 2010-01-01 全量获取
            if max_rpt_date is not None:
                start_date = (max_rpt_date + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = "2010-01-01"
            # 调用 gm 接口获取现金流量表（分批获取，带超时保护）
            df = _fetch_fund_cashflow_batched(symbol, start_date, end_date)
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            saved_count = 0
            with SessionLocal() as db:
                for _, row in df.iterrows():
                    rpt_date = _to_date(row.get("rpt_date"))
                    if rpt_date is None:
                        continue
                    pub_date = _to_date(row.get("pub_date"))
                    # 查询是否已有该 (symbol, rpt_date) 记录
                    existing = (
                        db.query(_mdl)
                        .filter(_mdl.symbol == symbol, _mdl.rpt_date == rpt_date)
                        .first()
                    )
                    if existing is not None:
                        # 已有记录：新数据 pub_date 更旧则跳过，否则更新所有字段
                        if (pub_date is not None and existing.pub_date is not None
                                and pub_date < existing.pub_date):
                            continue
                        existing.pub_date = pub_date
                        existing.rpt_type = _clean_scalar(row.get("rpt_type"))
                        existing.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(existing, f, _clean_scalar(row.get(f)))
                    else:
                        # 无已有记录：插入新记录
                        obj = _mdl(symbol=symbol, rpt_date=rpt_date)
                        obj.pub_date = pub_date
                        obj.rpt_type = _clean_scalar(row.get("rpt_type"))
                        obj.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(obj, f, _clean_scalar(row.get(f)))
                        db.add(obj)
                    saved_count += 1
                    # 每 100 条 commit 一次
                    if saved_count % 100 == 0:
                        db.commit()
                db.commit()  # 提交剩余记录
            logger.info(f"->{symbol} 成功：{saved_count}")
            steps[symbol] = saved_count
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps


# 财务指标字段列表（gm API fields 参数，20个，不超过限制）
FINANCE_DERIV_FIELDS = "roe,roe_weight,roe_avg,roa,roic,sale_gpm,sale_npm,ebitda_toi,ebit_toi,ast_liab_rate,curr_rate,quick_rate,liab_eqy_rate,ttl_ast_turnover_rate,acct_rcv_turnover_days,inv_turnover_days,net_prof_pcom_yoy,ttl_inc_oper_yoy,net_prof_yoy,ttl_asset_yoy"


def _fetch_finance_deriv_batched(symbol, start_date, end_date):
    """分批获取财务指标数据（gm API 限制 fields 不超过 20 个）。

    将 20 个字段按 20 个一批分别请求，再以元数据列为 key 合并，
    返回包含全部字段的完整 DataFrame。
    """
    all_fields = FINANCE_DERIV_FIELDS.split(",")
    batch_size = 20
    dfs = []
    for i in range(0, len(all_fields), batch_size):
        batch_fields = ",".join(all_fields[i:i + batch_size])
        df = call_with_timeout(stk_get_finance_deriv, timeout=30)(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            fields=batch_fields,
            df=True,
        )
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]
    # 多批数据按元数据列合并（同一 symbol+日期范围返回的行一致）
    merge_cols = ["symbol", "pub_date", "rpt_date", "rpt_type", "data_type"]
    result = dfs[0]
    for df in dfs[1:]:
        result = result.merge(df, on=merge_cols, how="outer")
    return result


# 循环获取指定股票列表的财务指标数据并 upsert 入库
def upsert_finance_deriv_sql(symbols):
    """循环获取指定股票列表的财务指标数据并 upsert 入库。

    增量更新策略：
    - 数据库已有该 symbol 数据：从最新 rpt_date + 1 天开始获取
    - 数据库无该 symbol 数据：全量获取（start_date=None）

    去重规则（按 symbol + rpt_date）：
    - 已有记录且新数据 pub_date >= 已有 pub_date：更新所有字段
    - 已有记录但新数据 pub_date < 已有 pub_date：跳过该行
    - 无已有记录：插入新记录

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的保存条数。
    """
    _mdl = FinanceDeriv
    _field_list = FINANCE_DERIV_FIELDS.split(",")  # 20 个财务字段名列表
    steps = {}  # 记录每个 symbol 的保存条数
    logger.info("财务指标数据获取并导入")
    end_date = datetime.now().strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            # 查询数据库中该 symbol 的最大 rpt_date，用于增量更新
            with SessionLocal() as db:
                max_rpt_date = (
                    db.query(func.max(_mdl.rpt_date))
                    .filter(_mdl.symbol == symbol)
                    .scalar()
                )
            # 增量起点：有数据则从最新 rpt_date + 1 天，否则从 2010-01-01 全量获取
            if max_rpt_date is not None:
                start_date = (max_rpt_date + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = "2010-01-01"
            # 调用 gm 接口获取财务指标（分批获取，带超时保护）
            df = _fetch_finance_deriv_batched(symbol, start_date, end_date)
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            saved_count = 0
            with SessionLocal() as db:
                for _, row in df.iterrows():
                    rpt_date = _to_date(row.get("rpt_date"))
                    if rpt_date is None:
                        continue
                    pub_date = _to_date(row.get("pub_date"))
                    # 查询是否已有该 (symbol, rpt_date) 记录
                    existing = (
                        db.query(_mdl)
                        .filter(_mdl.symbol == symbol, _mdl.rpt_date == rpt_date)
                        .first()
                    )
                    if existing is not None:
                        # 已有记录：新数据 pub_date 更旧则跳过，否则更新所有字段
                        if (pub_date is not None and existing.pub_date is not None
                                and pub_date < existing.pub_date):
                            continue
                        existing.pub_date = pub_date
                        existing.rpt_type = _clean_scalar(row.get("rpt_type"))
                        existing.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(existing, f, _clean_scalar(row.get(f)))
                    else:
                        # 无已有记录：插入新记录
                        obj = _mdl(symbol=symbol, rpt_date=rpt_date)
                        obj.pub_date = pub_date
                        obj.rpt_type = _clean_scalar(row.get("rpt_type"))
                        obj.data_type = _clean_scalar(row.get("data_type"))
                        for f in _field_list:
                            setattr(obj, f, _clean_scalar(row.get(f)))
                        db.add(obj)
                    saved_count += 1
                    # 每 100 条 commit 一次
                    if saved_count % 100 == 0:
                        db.commit()
                db.commit()  # 提交剩余记录
            logger.info(f"->{symbol} 成功：{saved_count}")
            steps[symbol] = saved_count
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps


# 估值指标字段列表（gm API fields 参数，20个，不超过限制）
DAILY_VALUATION_FIELDS = "pe_ttm,pe_lyr,pe_mrq,pe_ttm_cut,pe_lyr_cut,pe_mrq_cut,pb_lyr,pb_mrq,pcf_ttm_oper,pcf_ttm_ncf,pcf_lyr_oper,pcf_lyr_ncf,ps_ttm,ps_lyr,ps_mrq,peg_lyr,peg_1q,peg_3q,dy_ttm,dy_lfy"


def _fetch_daily_valuation_batched(symbol, start_date, end_date):
    """分批获取估值指标数据（gm API 限制 fields 不超过 20 个）。

    注意：stk_get_daily_valuation 的 API 签名与三大报表不同，
    fields 为第二个位置参数，无 rpt_type/data_type 参数。
    将 20 个字段按 20 个一批分别请求，再以元数据列为 key 合并，
    返回包含全部字段的完整 DataFrame。
    """
    all_fields = DAILY_VALUATION_FIELDS.split(",")
    batch_size = 20
    dfs = []
    for i in range(0, len(all_fields), batch_size):
        batch_fields = ",".join(all_fields[i:i + batch_size])
        # fields 为第二位置参数，无 rpt_type/data_type
        df = call_with_timeout(stk_get_daily_valuation, timeout=30)(
            symbol,
            batch_fields,
            start_date=start_date,
            end_date=end_date,
            df=True,
        )
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]
    # 多批数据按元数据列合并（估值指标元数据仅 symbol + trade_date）
    merge_cols = ["symbol", "trade_date"]
    result = dfs[0]
    for df in dfs[1:]:
        result = result.merge(df, on=merge_cols, how="outer")
    return result


# 循环获取指定股票列表的估值指标数据并 upsert 入库
def upsert_daily_valuation_sql(symbols):
    """循环获取指定股票列表的估值指标数据并 upsert 入库。

    增量更新策略（基于 trade_date，非 rpt_date）：
    - 数据库已有该 symbol 数据：从最新 trade_date + 1 天开始获取
    - 数据库无该 symbol 数据：从 2010-01-01 全量获取

    去重规则（按 symbol + trade_date 直接覆盖）：
    - 每日估值无修正概念，无需 pub_date 最新保留逻辑
    - 已有记录：直接更新所有字段
    - 无已有记录：插入新记录

    单个 symbol 失败不中断后续步骤，返回 steps 字典记录每个 symbol 的保存条数。
    """
    _mdl = DailyValuation
    _field_list = DAILY_VALUATION_FIELDS.split(",")  # 20 个估值指标字段名列表
    steps = {}  # 记录每个 symbol 的保存条数
    logger.info("估值指标数据获取并导入")
    end_date = datetime.now().strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            # 查询数据库中该 symbol 的最大 trade_date，用于增量更新
            with SessionLocal() as db:
                max_trade_date = (
                    db.query(func.max(_mdl.trade_date))
                    .filter(_mdl.symbol == symbol)
                    .scalar()
                )
            # 增量起点：有数据则从最新 trade_date + 1 天，否则从 2010-01-01 全量获取
            if max_trade_date is not None:
                start_date = (max_trade_date + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = "2010-01-01"
            # 调用 gm 接口获取估值指标（分批获取，带超时保护）
            df = _fetch_daily_valuation_batched(symbol, start_date, end_date)
            if df is None or df.empty:
                logger.info(f"->{symbol} 无需导入")
                steps[symbol] = 0
                continue
            saved_count = 0
            with SessionLocal() as db:
                for _, row in df.iterrows():
                    trade_date = _to_date(row.get("trade_date"))
                    if trade_date is None:
                        continue
                    # 查询是否已有该 (symbol, trade_date) 记录
                    existing = (
                        db.query(_mdl)
                        .filter(_mdl.symbol == symbol, _mdl.trade_date == trade_date)
                        .first()
                    )
                    if existing is not None:
                        # 已有记录：直接覆盖更新所有字段（每日估值无修正概念）
                        for f in _field_list:
                            setattr(existing, f, _clean_scalar(row.get(f)))
                    else:
                        # 无已有记录：插入新记录
                        obj = _mdl(symbol=symbol, trade_date=trade_date)
                        for f in _field_list:
                            setattr(obj, f, _clean_scalar(row.get(f)))
                        db.add(obj)
                    saved_count += 1
                    # 每 100 条 commit 一次
                    if saved_count % 100 == 0:
                        db.commit()
                db.commit()  # 提交剩余记录
            logger.info(f"->{symbol} 成功：{saved_count}")
            steps[symbol] = saved_count
        except Exception as e:
            # 单步失败不中断后续 symbol，记录错误并继续
            logger.error(f"->{symbol} 失败：{str(e)}")
            steps[symbol] = -1
    return steps


def upsert_economic_indicator_sql(indicator_code):
    """同步单个美国宏观经济指标数据并 upsert 入库。

    采集流程：
    1. 从 Config.ECONOMIC_INDICATORS 获取指标元信息（akshare 函数名、列模式等）
    2. 根据 col_pattern 调用对应的 akshare 函数获取 DataFrame（带超时保护）
    3. 根据 col_pattern 清洗列结构，统一为 ORM 字段名
    4. 增量/全量过滤后 upsert 入库

    增量策略：
    - 月度/季度指标（模式A/B）：查询 DB 最大 report_date，仅导入新增行；无数据从 2010-01-01 全量
    - 日度指标（模式C）：获取最近 365 天数据

    返回值：插入/更新条数（int），异常返回 -1。
    """
    _engine = settings.DB_ENGINE
    _mdl = EconomicIndicator

    # 获取指标元信息
    meta = dbsCfg.ECONOMIC_INDICATORS.get(indicator_code)
    if meta is None:
        logger.warning(f"未知经济指标代码：{indicator_code}")
        return -1

    logger.info(f"经济指标 {indicator_code}（{meta['name']}）获取并导入")
    try:
        # 函数内部导入 akshare，避免模块加载时强依赖
        import akshare as ak
        ak_func = getattr(ak, meta['akshare_func'])
        col_pattern = meta['col_pattern']

        # 根据 col_pattern 调用 akshare 函数获取 DataFrame
        if col_pattern == 'C':
            # 日度指标（bond_zh_us_rate）：获取最近 365 天数据
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
            df = call_with_timeout(ak_func, timeout=30)(
                start_date=start_date, end_date=end_date)
        else:
            # 月度/季度指标：全量获取
            df = call_with_timeout(ak_func, timeout=30)()

        if df is None or df.empty:
            logger.info(f"->{indicator_code} 无需导入")
            return 0

        # 根据 col_pattern 清洗 DataFrame 列结构，统一为 ORM 字段名
        if col_pattern == 'A':
            # 模式A：['商品','日期','今值','预测值','前值']
            df = df.rename(columns={
                '日期': 'report_date', '今值': 'value',
                '预测值': 'value_expected', '前值': 'value_prev',
            })
            df['pub_date'] = None
        elif col_pattern == 'B':
            # 模式B：['时间','发布日期','现值','前值']
            df = df.rename(columns={
                '时间': 'report_date', '发布日期': 'pub_date',
                '现值': 'value', '前值': 'value_prev',
            })
            df['value_expected'] = None
        elif col_pattern == 'C':
            # 模式C：bond_zh_us_rate，提取 '日期' 列和 col_name 指定的列
            col_name = meta.get('col_name')
            df = df.rename(columns={
                '日期': 'report_date', col_name: 'value',
            })
            df['pub_date'] = None
            df['value_expected'] = None
            df['value_prev'] = None

        # 添加元信息列
        df['indicator_code'] = indicator_code
        df['indicator_name'] = meta['name']
        df['category'] = meta['category']
        df['unit'] = meta['unit']
        df['frequency'] = meta['frequency']

        # 日期列转换（空值/异常值转为 NaT 后过滤）
        df['report_date'] = pd.to_datetime(df['report_date'], errors='coerce').dt.date
        if 'pub_date' in df.columns:
            df['pub_date'] = pd.to_datetime(df['pub_date'], errors='coerce').dt.date

        # 数值列转换（非数值转为 NaN 后过滤）
        for col in ['value', 'value_prev', 'value_expected']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 过滤掉 report_date 或 value 为 NaN 的行
        df = df[df['report_date'].notna() & df['value'].notna()]

        # 增量过滤：月度/季度指标查询 DB 最大 report_date
        if col_pattern in ('A', 'B'):
            with SessionLocal() as db:
                max_date = (
                    db.query(func.max(_mdl.report_date))
                    .filter(_mdl.indicator_code == indicator_code)
                    .scalar()
                )
            if max_date is not None:
                df = df[df['report_date'] > max_date]
            else:
                # 无数据则从 2010-01-01 全量
                df = df[df['report_date'] >= date(2010, 1, 1)]

        if df.empty:
            logger.info(f"->{indicator_code} 无需导入")
            return 0

        # 选择目标列并入库
        cols = ['indicator_code', 'indicator_name', 'category', 'report_date',
                'pub_date', 'value', 'value_prev', 'value_expected', 'unit', 'frequency']
        df = df[[c for c in cols if c in df.columns]]
        df = df.replace({np.nan: None})

        upsert_df_to_db(df, _mdl.__table__.name, _engine, _mdl.unique_keys)
        count = len(df)
        logger.info(f"->{indicator_code} 成功：{count}")
        return count
    except Exception as e:
        logger.error(f"->{indicator_code} 失败：{str(e)}")
        return -1


def upsert_all_economic_indicators_sql():
    """遍历 Config.ECONOMIC_INDICATORS 全量同步所有经济指标。

    单指标失败不中断（try/except 记录 -1），
    返回 {indicator_code: count, ...} 结果字典。
    """
    steps = {}
    logger.info("全量同步美国宏观经济指标")
    for indicator_code in dbsCfg.ECONOMIC_INDICATORS:
        try:
            steps[indicator_code] = upsert_economic_indicator_sql(indicator_code)
        except Exception as e:
            logger.error(f"->{indicator_code} 失败：{str(e)}")
            steps[indicator_code] = -1
    return steps
