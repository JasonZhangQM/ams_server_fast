# -*- coding: utf-8 -*-
"""bds 应用路由。

bds 模块只负责"数据管理"：数据同步 + 基础查询 + 下拉选项。
可视化分析查询（如 /economic-indicators/latest）已迁移至 irs 模块。

提供查询路由与同步路由：
- GET  /bds/trade-dates             查询交易日历
- GET  /bds/symbol-infos            查询证券信息
- GET  /bds/symbol-industries       行业下拉选项
- GET  /bds/index-histories         指数历史行情
- GET  /bds/index-codes             指数代码下拉
- GET  /bds/index-constituents      指数成分股
- GET  /bds/index-cum-returns       指数累计收益率
- GET  /bds/fund-balances           资产负债表
- GET  /bds/fund-incomes            利润表
- GET  /bds/fund-cashflows         现金流量表
- GET  /bds/finance-derivs          财务指标
- GET  /bds/daily-valuations        估值指标
- GET  /bds/daily-mktvalues         每日市值
- GET  /bds/economic-indicators     经济指标（基础查询，可视化分析用 irs）
- GET  /bds/economic-indicator-codes 经济指标代码下拉
- GET  /bds/gold-reserves           黄金储备
- GET  /bds/gold-reserve-countries  黄金储备国家下拉
- GET  /bds/daily-indicators        美债收益率指标
- GET  /bds/daily-indicator-codes   美债收益率代码下拉
- POST /bds/sync/{target}           数据同步（按 target 触发对应 service）
- POST /bds/sync/mark-synced        标记标的当日已完成批量同步（写 bds_symbol_info.sync_at）
"""
from datetime import date, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as PydanticModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, text

from server_fast.app.bds.models import (
    BdsSyncTask,
    DailyIndicator, 
    DailyMktvalue, 
    DailyValuation, 
    EconomicIndicator, 
    FinanceDeriv,
    FundBalance,
    FundCashflow, 
    FundIncome, 
    GoldReserve, 
    IndexConstituent, 
    IndexHistory, 
    SymbolInfo, 
    TradeDate,
)
from server_fast.app.bds.schemas import (
    BdsSyncTaskOut,
    BatchSyncRequest,
    DailyIndicatorOut,
    DailyMktvalueOut,
    DailyValuationOut,
    EconomicIndicatorOut,
    FinanceDerivOut,
    FundBalanceOut,
    FundCashflowOut,
    FundIncomeOut,
    GoldReserveOut,
    IndexConstituentOut,
    IndexHistoryOut,
    SymbolInfoOut,
    TradeDateOut,
)
from server_fast.app.bds.services import (
    fetch_realtime_index_prices,
    insert_trade_date_em_sql,
    upsert_all_economic_indicators_sql,
    upsert_all_gold_reserves_sql,
    upsert_all_daily_indicators_sql,
    upsert_daily_valuation_sql,
    upsert_daily_mktvalue_sql,
    upsert_economic_indicator_from_wscn_sql,
    upsert_economic_indicator_sql,
    upsert_finance_deriv_sql,
    upsert_fund_balance_sql,
    upsert_fund_cashflow_sql,
    upsert_fund_income_sql,
    upsert_gold_reserve_sql,
    upsert_index_constituent_sql,
    upsert_index_history_sql,
    upsert_symbol_info_excel_sql,
    upsert_daily_indicator_sql,
)
from server_fast.app.bds.config import Config
from server_fast.common.db import SessionLocal, get_db
from server_fast.common.pagination import PageResponse

# 批量同步后台线程需要的依赖
import threading
from server_fast.sync_runner import TABLES, resolve_symbols, run_tasks

# 模块级停止信号字典：{task_id: threading.Event}，供 POST /bds/stop/batch-sync 触发
_stop_events: dict[int, threading.Event] = {}

router = APIRouter(prefix="/bds", tags=["bds"])


def _cleanup_zombies(db: Session, minutes: int = 5) -> int:
    """清理僵尸任务（running 状态超过指定分钟数未更新）。返回清理数量。"""
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(minutes=minutes)
    zombies = db.query(BdsSyncTask).filter(
        BdsSyncTask.status == "running",
        BdsSyncTask.update_time < cutoff,
    ).all()
    for z in zombies:
        z.status = "failed"
        z.errors = (z.errors or []) + [{"symbol": "", "table": "", "message": f"僵尸任务（running 超 {minutes} 分钟未更新，后端重启残留）"}]
        z.end_time = z.update_time
        print(f'[batch-sync] 清理僵尸 #{z.id} (update_time={z.update_time})')
    if zombies:
        db.commit()
    return len(zombies)


def _run_sync(task_name: str, func) -> dict:
    """统一的同步任务执行器：调用 service 函数并捕获异常。

    service 函数内部自管理 session（使用 pandas.to_sql + settings.DB_ENGINE），
    因此此处不再注入 db 依赖。
    """
    try:
        func()
        return {"status": "ok", "message": f"{task_name} synced"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _build_sync_response(symbol: str, steps: dict) -> dict:
    """根据 steps 中 symbol 对应的保存条数构建同步响应。

    - -2：跳过（库内报告期已是最新报告期，未调 gm）
    - -1：同步失败（gm 终端未启动或接口异常）
    - 0：无数据可导入
    - >0：成功导入 count 条
    """
    count = steps.get(symbol, 0)
    if count == -2:
        return {"status": "no_data", "message": f"已是最新报告期，无需导入：{symbol}"}
    if count == -1:
        return {"status": "error", "message": f"同步失败：{symbol}，请检查 gm 终端是否启动"}
    if count == 0:
        return {"status": "no_data", "message": f"无数据可导入：{symbol}，请检查 gm 终端是否启动"}
    return {"status": "success", "message": f"同步完成：{symbol}，更新 {count} 条", "steps": steps}


@router.get("/trade-dates", response_model=PageResponse[TradeDateOut])
def list_trade_dates(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询交易日历数据，支持可选的日期范围筛选。"""
    query = db.query(TradeDate)
    if start_date:
        query = query.filter(TradeDate.trade_date >= start_date)
    if end_date:
        query = query.filter(TradeDate.trade_date <= end_date)
    total = query.count()  # 满足过滤条件的总记录数
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/symbol-infos", response_model=PageResponse[SymbolInfoOut])
def list_symbol_infos(
    symbol: Optional[str] = Query(None, description="代码精确匹配（对应 UK 最左前缀）"),
    industry: Optional[str] = Query(None, description="行业精确匹配（list_filter）"),
    keyword: Optional[str] = Query(None, description="代码或名称任一模糊匹配（OR，用于远程搜索）"),
    exclude_synced_today: bool = Query(
        False, description="为 True 时排除 sync_at 等于当日（服务器本地日）的标的，用于批量同步断点续跑"
    ),
    limit: int = Query(86, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询证券基本信息，支持可选过滤。

    过滤规则：
    - symbol          → == 精确匹配（对应 UK 最左前缀）
    - industry        → == 精确匹配
    - keyword         → symbol/name 任一包含即命中（OR，远程搜索保留模糊）
    - exclude_synced_today → 剔除当日已批量同步完成的标的（sync_at 为空或不为当日者保留）
    """
    query = db.query(SymbolInfo)
    if symbol:
        query = query.filter(SymbolInfo.symbol == symbol)
    if industry:
        query = query.filter(SymbolInfo.industry == industry)
    if keyword:
        # 关键字对 symbol 和 name 做 OR 模糊匹配，支持输入代码或名称搜索
        query = query.filter(
            or_(
                SymbolInfo.symbol.contains(keyword),
                SymbolInfo.name.contains(keyword),
            )
        )
    if exclude_synced_today:
        # 只保留「从未标记」或「标记日期早于今日」的标的
        query = query.filter(
            or_(SymbolInfo.sync_at.is_(None), SymbolInfo.sync_at != date.today())
        )
    total = query.count()  # 应用所有过滤条件后的总记录数
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/symbol-industries")
def list_symbol_industries(db: Session = Depends(get_db)):
    """返回证券信息表中去重后的行业列表。

    数据源为 bds_symbol_info 表的 industry 字段（DISTINCT 去重，排除 NULL 与空串），
    按行业名称升序排序，供前端行业筛选下拉使用。无需分页。
    """
    rows = (
        db.query(SymbolInfo.industry)
        .filter(SymbolInfo.industry.isnot(None), SymbolInfo.industry != "")
        .distinct()
        .order_by(SymbolInfo.industry.asc())
        .all()
    )
    industries = [r[0] for r in rows]
    return {"industries": industries}


@router.post("/sync/trade-date")
def sync_trade_date():
    """同步交易日历（触发 insert_trade_date_em_sql）。

    返回值说明：
    - status: 同步状态，成功为 "ok"
    - message: 同步结果描述信息
    - updated_count: 新增的交易日数量（0 表示无需更新）
    """
    try:
        updated_count = insert_trade_date_em_sql()
        return {"status": "ok", "message": "trade-date synced", "updated_count": updated_count or 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync/symbol-info")
def sync_symbol_info():
    """同步证券信息（触发 upsert_symbol_info_excel_sql）。"""
    return _run_sync("symbol-info", upsert_symbol_info_excel_sql)


@router.post("/sync/mark-synced")
def mark_synced(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    db: Session = Depends(get_db),
):
    """标记某标的「当日已完成批量同步」：写 bds_symbol_info.sync_at = 服务器本地当日。

    由批量同步 notebook 在该 symbol 的全部目标表都返回 success / no_data 之后调用。
    幂等：重复调用只把 sync_at 刷新为当日，不回写其他字段。

    ⚠️ 本端点只写 sync_at 一个字段，与 upsert_symbol_info_excel_sql 的
    ON DUPLICATE KEY UPDATE 子句不重叠，故两者互不覆盖。

    返回值说明：
    - status: ok / error
    - symbol: 标的代码
    - sync_at: 写入的日期（YYYY-MM-DD）
    - updated: 受影响行数（0 表示库中无该 symbol）
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空", "updated": 0}
    today = date.today()
    updated = (
        db.query(SymbolInfo)
        .filter(SymbolInfo.symbol == symbol)
        .update({"sync_at": today}, synchronize_session=False)
    )
    db.commit()
    if updated == 0:
        return {"status": "error", "message": f"未找到标的：{symbol}", "updated": 0}
    return {
        "status": "ok",
        "symbol": symbol,
        "sync_at": today.strftime("%Y-%m-%d"),
        "updated": updated,
    }


@router.post("/sync/index-history")
def sync_index_history():
    """同步指数历史行情数据到数据库。

    触发底层 upsert_index_history_sql() 函数，遍历配置中所有指数代码，
    通过 GM 接口获取历史行情并执行 upsert 操作。

    返回值说明：
    - status: 同步状态，成功为 "ok"
    - message: 同步结果描述信息
    - updated_count: 成功更新的指数代码数量（统计获取条数 > 0 的 symbol 数量）

    异常处理：
    - 捕获所有异常并抛出 HTTP 500 错误，返回异常详情
    """
    try:
        # 调用底层服务函数执行同步，返回各 symbol 的获取条数字典
        steps = upsert_index_history_sql()
        # 统计实际有更新数据的 symbol 数量（获取条数 > 0 表示有新增数据）
        updated_count = sum(1 for count in steps.values() if count > 0)
        return {"status": "ok", "message": "index-history synced", "updated_count": updated_count}
    except Exception as e:
        # 捕获异常并转换为 HTTP 500 错误响应
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/index-histories", response_model=PageResponse[IndexHistoryOut])
def list_index_histories(
    symbol: Optional[List[str]] = Query(None, description="代码多选精确匹配"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询指数历史行情，支持代码多选和日期范围筛选。

    响应中每条记录附加 sec_name 字段（从 Config.INDEX_CODE 查找）。
    """
    query = db.query(IndexHistory)
    if symbol:
        query = query.filter(IndexHistory.symbol.in_(symbol))
    if start_date:
        query = query.filter(IndexHistory.trade_date >= start_date)
    total = query.count()
    items = query.order_by(IndexHistory.trade_date.desc()).offset(offset).limit(limit).all()
    # 附加 sec_name 字段（从 Config.INDEX_CODE 查找，不存数据库）
    items_dict = []
    for item in items:
        d = item.to_dict()
        d["sec_name"] = Config.INDEX_CODE.get(item.symbol, {}).get("sec_name")
        items_dict.append(d)
    return {"items": items_dict, "total": total, "limit": limit, "offset": offset}


@router.get("/index-codes")
def list_index_codes():
    """返回应用配置的指数代码列表。

    数据源为 Config.INDEX_CODE 字典，确保前端下拉选项与后端指数配置
    保持一致，无需依赖数据库查询。每项包含 code（指数代码）和
    sec_name（指数名称）。
    """
    index_codes = [
        {"code": code, "sec_name": info["sec_name"]}
        for code, info in Config.INDEX_CODE.items()
    ]
    return {"index_codes": index_codes}


@router.get("/index-constituents", response_model=PageResponse[IndexConstituentOut])
def list_index_constituents(
    index_code: Optional[str] = Query(default=None, description="指数代码精确匹配"),
    symbol: Optional[str] = Query(default=None, description="成分股代码精确匹配"),
    trade_date: Optional[date] = Query(default=None, description="交易日期 YYYY-MM-DD"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询指数成分股，支持指数代码精确匹配、成分股代码精确匹配和具体交易日期筛选。

    响应中每条记录附加 sec_name 字段（从 Config.INDEX_CODE 查找）。
    """
    query = db.query(IndexConstituent)
    # index_code 精确匹配
    if index_code:
        query = query.filter(IndexConstituent.index_code == index_code)
    # symbol 精确匹配
    if symbol:
        query = query.filter(IndexConstituent.symbol == symbol)
    # 具体交易日期精确匹配
    if trade_date:
        query = query.filter(IndexConstituent.trade_date == trade_date)
    # 按 trade_date 降序
    query = query.order_by(IndexConstituent.trade_date.desc())
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    # 附加 sec_name 字段（从 Config.INDEX_CODE 查找，不存数据库）
    result_items = []
    for item in items:
        item_dict = item.to_dict()
        item_dict["sec_name"] = Config.INDEX_CODE.get(item.index_code, {}).get("sec_name")
        result_items.append(item_dict)
    return {"items": result_items, "total": total, "limit": limit, "offset": offset}


@router.post("/sync/index-constituent")
def sync_index_constituent(trade_date: Optional[date] = None):
    """同步指数成分股数据到数据库。

    触发底层 upsert_index_constituent_sql() 函数，遍历配置中所有指数代码，
    通过 GM 接口获取指定日期（未指定则为最新交易日）的成分股并执行追加操作。

    参数说明：
    - trade_date: 可选，指定同步的交易日（%Y-%m-%d），未指定则获取最新交易日数据

    返回值说明：
    - status: 同步状态，成功为 "success"
    - message: 同步结果描述信息
    - steps: 各 index_code 的结果字典（1=已保存，0=未变化或空数据跳过，-1=失败）
    """
    try:
        result = upsert_index_constituent_sql(trade_date=trade_date)
        return {"status": "success", "message": "指数成分股同步完成", "steps": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/index-cum-returns")
def list_index_cum_returns(
    start_date: Optional[str] = Query(
        default=None, description="起始日期 YYYY-MM-DD，默认当前日期前10年"
    ),
    db: Session = Depends(get_db),
):
    """查询指数累计收益率。

    逻辑说明：
    1. 查询 bds_index_history 表中 trade_date >= start_date 且 symbol 在
       Config.INDEX_CODE 字典键中的全部记录。
    2. 用 pandas 透视数据：行索引为 trade_date（升序），列名为通过
       Config.INDEX_CODE[symbol]['sec_name'] 映射的指数名称，值为 close 收盘价。
    3. 累计收益率计算：(df/df.iloc[0]-1)*100，保留两位小数。
    4. 空数据时返回 {"trade_dates": [], "series": {}}。
    5. 非空时构造响应：trade_dates 为日期字符串列表（YYYY-MM-DD），
       series 为 {指数名称: 累计收益率列表}，NaN 转 null。
    """
    # 默认起始日：当前日期前 10 年
    if start_date is None:
        start_date = (date.today() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")

    # 查询满足条件的全部指数历史记录（symbol 限定在 Config.INDEX_CODE 键中）
    rows = (
        db.query(IndexHistory)
        .filter(
            IndexHistory.trade_date >= start_date,
            IndexHistory.symbol.in_(list(Config.INDEX_CODE.keys())),
        )
        .all()
    )

    # 空数据直接返回空结构
    if not rows:
        return {
            "trade_dates": [],
            "series": {},
            "max_drawdown": {},
            "realtime_source": None,
        }

    # 构造 DataFrame 并透视：行=trade_date，列=symbol，值=close
    df = pd.DataFrame(
        [
            {
                "trade_date": r.trade_date,
                "symbol": r.symbol,
                "close": float(r.close) if r.close is not None else np.nan,
            }
            for r in rows
        ]
    )
    df = df.pivot(index="trade_date", columns="symbol", values="close")
    # 按 trade_date 升序排序（在 rename 前排序，便于后续以 symbol 列名拼接实时行情）
    df = df.sort_index(ascending=True)

    # 优化：按列检查每个指数在今日是否有数据，仅对缺失的指数调用实时接口
    # 场景：部分指数历史数据已更新到今日（如 A 股已收盘入库），另一部分未更新（如 SP500 时差延迟）
    today = date.today()
    if today in df.index:
        # 历史 index 含今日：检查每列在今日是否有非 NaN 值
        today_row = df.loc[today]
        missing_symbols = [sym for sym in df.columns if pd.isna(today_row[sym])]
    else:
        # 历史 index 不含今日：所有指数都缺今日数据
        missing_symbols = list(df.columns)

    if missing_symbols:
        # 仅为缺失的指数调用实时接口，避免不必要的网络开销
        realtime_prices, realtime_source = fetch_realtime_index_prices(missing_symbols)
    else:
        # 所有指数今日数据均已入库，无需调用实时接口
        realtime_prices, realtime_source = {}, None

    # 拼接当日实时行情：此时 df 列名仍为 symbol，可直接用 realtime_prices 匹配
    if realtime_prices:
        # 构造今日实时行：每个 symbol 列填入实时价（缺失填 NaN）
        today_row = pd.Series(
            {sym: realtime_prices.get(sym, np.nan) for sym in df.columns},
            name=today,
        )
        # 历史末尾日期早于今日，必然走新增行分支（覆盖分支仅防御性保留）
        if today in df.index:
            for sym, price in realtime_prices.items():
                if sym in df.columns:
                    df.loc[today, sym] = price
        else:
            df = pd.concat([df, pd.DataFrame([today_row])])
            df = df.sort_index(ascending=True)

    # 列名由 symbol 映射为 sec_name（指数名称）
    df = df.rename(columns={k: v["sec_name"] for k, v in Config.INDEX_CODE.items()})

    # 累计净值：当日收盘价 / 首日收盘价
    cum = df / df.iloc[0]
    # 累计收益率：(累计净值 - 1) * 100，保留两位小数
    cum_df = ((cum - 1) * 100).round(2)

    # 最大回撤：当日累计净值 / 截至当日历史峰值 - 1，结果为非正值（≤0）
    running_max = cum.expanding().max()
    max_drawdown_df = ((cum / running_max - 1) * 100).round(2)

    # 日期字符串列表（YYYY-MM-DD 格式）
    trade_dates = [d.strftime("%Y-%m-%d") for d in cum_df.index]

    # 构造 series：{指数名称: 累计收益率列表}，NaN 转 None（JSON 序列化为 null）
    series = {
        col_name: [None if pd.isna(v) else float(v) for v in cum_df[col_name]]
        for col_name in cum_df.columns
    }

    # 构造 max_drawdown：{指数名称: 最大回撤列表}，NaN 转 None
    max_drawdown = {
        col_name: [None if pd.isna(v) else float(v) for v in max_drawdown_df[col_name]]
        for col_name in max_drawdown_df.columns
    }

    return {
        "trade_dates": trade_dates,
        "series": series,
        "max_drawdown": max_drawdown,
        "realtime_source": realtime_source,
    }


@router.post("/sync/fund-balance")
def sync_fund_balance(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步资产负债表数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_fund_balance_sql([symbol], force=force)
    return _build_sync_response(symbol, steps)


def _query_fund_reports(model, db: Session, symbol, rpt_type, start_date, limit, offset):
    """通用财务报表查询（FundBalance/FundIncome/FundCashflow/FinanceDeriv 共用）。

    过滤规则：symbol 精确匹配、rpt_type 精确匹配、rpt_date 起始日。
    排序规则：rpt_date 降序，同 rpt_date 按 pub_date 降序。
    """
    query = db.query(model)
    if symbol:
        query = query.filter(model.symbol == symbol)
    if rpt_type is not None:
        query = query.filter(model.rpt_type == rpt_type)
    if start_date:
        query = query.filter(model.rpt_date >= start_date)
    total = query.count()
    items = (
        query.order_by(model.rpt_date.desc(), model.pub_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/fund-balances", response_model=PageResponse[FundBalanceOut])
def list_fund_balances(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询资产负债表数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FundBalance, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/fund-income")
def sync_fund_income(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步利润表数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_fund_income_sql([symbol], force=force)
    return _build_sync_response(symbol, steps)


@router.get("/fund-incomes", response_model=PageResponse[FundIncomeOut])
def list_fund_incomes(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询利润表数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FundIncome, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/fund-cashflow")
def sync_fund_cashflow(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步现金流量表数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_fund_cashflow_sql([symbol], force=force)
    return _build_sync_response(symbol, steps)


@router.get("/fund-cashflows", response_model=PageResponse[FundCashflowOut])
def list_fund_cashflows(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询现金流量表数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FundCashflow, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/finance-deriv")
def sync_finance_deriv(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步财务指标数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_finance_deriv_sql([symbol], force=force)
    return _build_sync_response(symbol, steps)


@router.get("/finance-derivs", response_model=PageResponse[FinanceDerivOut])
def list_finance_derivs(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询财务指标数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FinanceDeriv, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/daily-valuation")
def sync_daily_valuation(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步估值指标数据，接收单个股票代码，获取并入库。"""
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_daily_valuation_sql([symbol])
    return _build_sync_response(symbol, steps)


@router.get("/daily-valuations", response_model=PageResponse[DailyValuationOut])
def list_daily_valuations(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    start_date: Optional[date] = Query(default=None, description="交易日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询估值指标数据，支持代码精确匹配和交易日期范围筛选。

    排序规则：trade_date 降序。
    symbol 使用精确匹配（走 uk(symbol, trade_date) 复合索引最左前缀，~3ms），
    原 LIKE '%xxx%' 前导通配无法走 B+Tree 索引，120 万行下需全表扫描 ~7s。
    """
    query = db.query(DailyValuation)
    if symbol:
        query = query.filter(DailyValuation.symbol == symbol)
    if start_date:
        query = query.filter(DailyValuation.trade_date >= start_date)
    total = query.count()
    items = (
        query.order_by(DailyValuation.trade_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/daily-mktvalue")
def sync_daily_mktvalue(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步每日市值数据，接收单个股票代码，获取并入库。"""
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_daily_mktvalue_sql([symbol])
    return _build_sync_response(symbol, steps)


@router.get("/daily-mktvalues", response_model=PageResponse[DailyMktvalueOut])
def list_daily_mktvalues(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    start_date: Optional[date] = Query(default=None, description="交易日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询每日市值数据，支持代码精确匹配和交易日期范围筛选。

    排序规则：trade_date 降序。
    symbol 使用精确匹配（走 uk(symbol, trade_date) 复合索引最左前缀，~3ms）。
    """
    query = db.query(DailyMktvalue)
    if symbol:
        query = query.filter(DailyMktvalue.symbol == symbol)
    if start_date:
        query = query.filter(DailyMktvalue.trade_date >= start_date)
    total = query.count()
    items = (
        query.order_by(DailyMktvalue.trade_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/economic-indicators", response_model=PageResponse[EconomicIndicatorOut])
def list_economic_indicators(
    indicator_code: Optional[str] = Query(default=None, description="指标代码精确匹配"),
    category: Optional[str] = Query(default=None, description="类别精确匹配"),
    country: Optional[str] = Query(default=None, description="国别精确匹配"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    end_date: Optional[date] = Query(default=None, description="报告日期结束日"),
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询经济指标数据，支持指标代码/类别/国别精确匹配和报告日期范围筛选。

    排序规则：report_date 降序，同 report_date 按 indicator_code 升序。
    """
    query = db.query(EconomicIndicator)
    # indicator_code 精确匹配
    if indicator_code:
        query = query.filter(EconomicIndicator.indicator_code == indicator_code)
    # category 精确匹配
    if category:
        query = query.filter(EconomicIndicator.category == category)
    # country 精确匹配
    if country:
        query = query.filter(EconomicIndicator.country == country)
    # 报告日期范围过滤
    if start_date:
        query = query.filter(EconomicIndicator.report_date >= start_date)
    if end_date:
        query = query.filter(EconomicIndicator.report_date <= end_date)
    total = query.count()
    items = (
        query.order_by(EconomicIndicator.report_date.desc(), EconomicIndicator.indicator_code.asc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/economic-indicator-codes")
def list_economic_indicator_codes():
    """返回经济指标配置列表（数据源 Config.ECONOMIC_INDICATORS，无数据库查询）。

    每项包含 indicator_code、indicator_name、category、country、unit、frequency，
    供前端下拉选项使用。
    """
    indicators = [
        {
            "indicator_code": code,
            "indicator_name": info["name"],
            "indicator_short_name": info["short_name"],
            "category": info["category"],
            "country": info["country"],
            "unit": info["unit"],
            "frequency": info["frequency"],
        }
        for code, info in Config.ECONOMIC_INDICATORS.items()
    ]
    return indicators


@router.post("/sync/economic-indicator")
def sync_economic_indicator(indicator_code: str = Query(..., description="指标代码，精确匹配单个指标")):
    """同步单个经济指标数据。

    返回值说明：
    - status: success/no_data/error
    - message: 同步结果描述信息
    - indicator_code: 指标代码
    - count: 插入/更新条数（-1 表示失败）
    """
    if not indicator_code:
        return {"status": "error", "message": "indicator_code 不能为空",
                "indicator_code": indicator_code, "count": -1}
    count = upsert_economic_indicator_sql(indicator_code)
    if count == -1:
        return {"status": "error", "message": f"同步失败：{indicator_code}",
                "indicator_code": indicator_code, "count": -1}
    if count == 0:
        return {"status": "no_data", "message": f"无数据可导入：{indicator_code}",
                "indicator_code": indicator_code, "count": 0}
    return {"status": "success", "message": f"同步完成：{indicator_code}，更新 {count} 条",
            "indicator_code": indicator_code, "count": count}


@router.post("/sync/economic-indicators-all")
def sync_economic_indicators_all():
    """同步全部经济指标数据。

    遍历 Config.ECONOMIC_INDICATORS 中所有指标代码，逐个调用同步函数。
    单指标失败不中断，返回 results 包含每个指标的结果。

    返回值说明：
    - status: success
    - message: 同步结果描述信息
    - results: {indicator_code: count, ...} 各指标同步条数字典
    """
    try:
        results = upsert_all_economic_indicators_sql()
        return {"status": "success", "message": "经济指标全量同步完成", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync/economic-indicator-wscn")
def sync_economic_indicator_wscn():
    """通过华尔街见闻日历接口同步经济指标数据。

    数据源：https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas
    覆盖 Config.WSCN_INDICATOR_MAP 中 11 个可映射的美国指标，
    补充 FRED 缺失的 forecast/importance/revised/pub_date 字段。

    返回值说明：
    - status: success/no_data/error
    - message: 同步结果描述信息
    - count: 插入/更新条数（-1 表示失败）
    """
    count = upsert_economic_indicator_from_wscn_sql()
    if count == -1:
        return {"status": "error", "message": "wscn 同步失败，请查看后端日志", "count": count}
    if count == 0:
        return {"status": "no_data", "message": "wscn 无新数据可导入", "count": count}
    return {"status": "success", "message": f"wscn 同步完成，更新 {count} 条", "count": count}


@router.get("/gold-reserve-countries")
def list_gold_reserve_countries():
    """返回央行黄金储备主要持有国列表。

    数据源为 Config.GOLD_RESERVE_COUNTRIES 字典，确保前端下拉选项与后端配置
    保持一致，无需依赖数据库查询。每项包含 country_code、country_name、imf_code。
    """
    countries = [
        {"country_code": code, "country_name": info["country_name"], "imf_code": info["imf_code"]}
        for code, info in Config.GOLD_RESERVE_COUNTRIES.items()
    ]
    return {"countries": countries}


@router.get("/gold-reserves", response_model=PageResponse[GoldReserveOut])
def list_gold_reserves(
    country_code: Optional[List[str]] = Query(None, description="国家代码多选精确匹配"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询黄金储备数据，支持国家多选和日期范围筛选。

    按 rpt_date 降序、country_code 升序排序。
    """
    query = db.query(GoldReserve)
    if country_code:
        query = query.filter(GoldReserve.country_code.in_(country_code))
    if start_date:
        query = query.filter(GoldReserve.rpt_date >= start_date)
    if end_date:
        query = query.filter(GoldReserve.rpt_date <= end_date)
    total = query.count()
    items = query.order_by(GoldReserve.rpt_date.desc(), GoldReserve.country_code.asc()).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/gold-reserve")
def sync_gold_reserve(country_code: str = Query(..., description="国家代码，精确匹配单个国家")):
    """同步单个国家央行黄金储备数据。

    返回值说明：
    - status: success/no_data/error
    - country_code: 同步的国家代码
    - count: 更新条数（-1 表示失败）
    - message: 同步结果说明，失败时含代理配置指引
    """
    if country_code not in Config.GOLD_RESERVE_COUNTRIES:
        raise HTTPException(status_code=400, detail=f"未知国家代码：{country_code}")
    try:
        count = upsert_gold_reserve_sql(country_code)
        if count == -1:
            # 同步失败：通常是 IMF SDMX API 不可达（DNS/SSL），提示用户配置代理
            return {
                "status": "error",
                "country_code": country_code,
                "count": -1,
                "message": (
                    f"同步失败：{country_code}。IMF SDMX API 不可达，"
                    "请在 server_fast/.env 中配置 HTTPS_PROXY 后重试"
                ),
            }
        if count == 0:
            return {"status": "no_data", "country_code": country_code, "count": 0, "message": f"无数据可导入：{country_code}"}
        return {"status": "success", "country_code": country_code, "count": count, "message": f"同步完成：{country_code}，更新 {count} 条"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync/gold-reserves-all")
def sync_gold_reserves_all():
    """全量同步所有国家央行黄金储备数据。

    单国家失败不中断，返回各国同步结果字典。
    若全部失败，通常为网络无法访问 IMF API，需在 .env 配置 HTTPS_PROXY。
    """
    try:
        steps = upsert_all_gold_reserves_sql()
        # 全部失败时附加提示
        all_failed = all(v == -1 for v in steps.values())
        message = "全量同步完成"
        if all_failed:
            message = "全部国家同步失败，IMF SDMX API 不可达，请在 server_fast/.env 中配置 HTTPS_PROXY"
        return {"status": "ok", "steps": steps, "message": message}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/daily-indicator-codes")
def list_daily_indicator_codes():
    """返回美债收益率指标配置列表（数据源 Config.DAILY_INDICATORS，无数据库查询）。

    每项包含 indicator_code、indicator_name、indicator_short_name、category、
    country、unit、frequency，供前端下拉选项使用。
    """
    indicators = [
        {
            "indicator_code": code,
            "indicator_name": info["name"],
            "indicator_short_name": info["short_name"],
            "category": info["category"],
            "country": info["country"],
            "unit": info["unit"],
            "frequency": info["frequency"],
        }
        for code, info in Config.DAILY_INDICATORS.items()
    ]
    return indicators


@router.get("/daily-indicators", response_model=PageResponse[DailyIndicatorOut])
def list_daily_indicators(
    indicator_code: Optional[List[str]] = Query(default=None, description="指标代码多选 IN 匹配"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    end_date: Optional[date] = Query(default=None, description="报告日期结束日"),
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询美债收益率指标数据，支持指标代码多选 IN、报告日期范围筛选与分页。

    排序规则：report_date 降序，同 report_date 按 indicator_code 升序。
    """
    query = db.query(DailyIndicator)
    # indicator_code 多选 IN 过滤
    if indicator_code:
        query = query.filter(DailyIndicator.indicator_code.in_(indicator_code))
    # 报告日期范围过滤
    if start_date:
        query = query.filter(DailyIndicator.report_date >= start_date)
    if end_date:
        query = query.filter(DailyIndicator.report_date <= end_date)
    total = query.count()
    items = (
        query.order_by(DailyIndicator.report_date.desc(), DailyIndicator.indicator_code.asc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/daily-indicator")
def sync_daily_indicator(indicator_code: str = Query(..., description="指标代码，精确匹配单个指标")):
    """同步单个日频经济指标数据。

    返回值说明：
    - status: success/no_data/error
    - message: 同步结果描述信息
    - indicator_code: 指标代码
    - count: 插入/更新条数（-1 表示失败）
    """
    if not indicator_code:
        return {"status": "error", "message": "indicator_code 不能为空",
                "indicator_code": indicator_code, "count": -1}
    if indicator_code not in Config.DAILY_INDICATORS:
        return {"status": "error", "message": f"未知收益率指标代码：{indicator_code}",
                "indicator_code": indicator_code, "count": -1}
    count = upsert_daily_indicator_sql(indicator_code)
    if count == -1:
        return {"status": "error", "message": f"同步失败：{indicator_code}",
                "indicator_code": indicator_code, "count": -1}
    if count == 0:
        return {"status": "no_data", "message": f"无数据可导入：{indicator_code}",
                "indicator_code": indicator_code, "count": 0}
    return {"status": "success", "message": f"同步完成：{indicator_code}，更新 {count} 条",
            "indicator_code": indicator_code, "count": count}


@router.post("/sync/daily-indicators-all")
def sync_daily_indicators_all():
    """全量同步所有美债收益率指标数据。

    遍历 Config.DAILY_INDICATORS 中 4 个指标代码，逐个调用同步函数。
    单指标失败不中断，返回 steps 包含每个指标的结果。

    返回值说明：
    - status: success
    - message: 同步结果描述信息
    - steps: {indicator_code: count, ...} 各指标同步条数字典
    """
    try:
        steps = upsert_all_daily_indicators_sql()
        return {"status": "success", "message": "美债收益率指标全量同步完成", "steps": steps}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# 批量同步：后台线程异步执行，进度实时写 bds_sync_task 表
# ============================================================================

def _run_batch_sync_thread(task_id: int, req: BatchSyncRequest):
    """后台线程：执行批量同步并实时写进度到 DB。

    与 HTTP 调用完全隔离：本线程内部创建独立 SessionLocal，
    不复用请求的 db 依赖。异常时将 task 状态置为 failed 并记录错误。
    """
    db = SessionLocal()
    task = db.query(BdsSyncTask).get(task_id)
    tables = req.tables or TABLES
    errors = []

    try:
        # 1. 解析 symbol 列表
        syms = resolve_symbols(
            req.scope,
            explicit=req.symbols,
            industry=req.industry,
            index_code=req.index_code,
            max_symbols=req.max_symbols,
            exclude_synced_today=req.exclude_synced_today,
        )

        tasks = [(s, t) for s in syms for t in tables]
        total_count = len(tasks)
        print(f'[batch-sync #{task_id}] 任务总数 {total_count} (symbols={len(syms)}, tables={len(tables)})')

        # 2. 更新 task 状态为 running
        task.status = "running"
        task.total_count = total_count
        task.start_time = date.today()  # DB 列是 DateTime，但这里只需要日期精度
        db.commit()

        # 3. 定义进度回调
        def on_progress(symbol, table, status, done_count, total):
            # 每次回调都开独立 session，避免长事务
            s = SessionLocal()
            try:
                t = s.query(BdsSyncTask).get(task_id)
                t.done_count = done_count
                if status == "success":
                    t.success_count += 1
                elif status == "no_data":
                    t.no_data_count += 1
                else:
                    t.error_count += 1
                    t.errors = (t.errors or []) + [{"symbol": symbol, "table": table, "message": ""}]
                t.progress = round(done_count / total * 100, 2) if total else 100
                s.commit()
            finally:
                s.close()

        # 4. 执行同步（复用 sync_runner，限速 + 重试 + 连续失败退避 + 当日标记 全在 run_tasks 内）
        stop_event = _stop_events.get(task_id)
        results = run_tasks(
            tasks,
            mark_synced_when_done=req.mark_synced,
            on_progress=on_progress,
            stop_event=stop_event,
        )

        # 5. 收集失败明细
        for r in results:
            if r["status"] == "error":
                errors.append({"symbol": r["symbol"], "table": r["table"], "message": r.get("message", "")})

        # 6. 结束：检查是否被停止
        if stop_event is not None and stop_event.is_set():
            task.status = "stopped"
        else:
            task.status = "done"
        task.errors = errors
        task.end_time = date.today()
        db.commit()
        print(f'[batch-sync #{task_id}] {task.status}：success={task.success_count}, no_data={task.no_data_count}, error={task.error_count}')

    except Exception as e:
        # 兜底：线程异常也要把 task 状态置为 failed
        errors.append({"symbol": "", "table": "", "message": f"线程异常: {type(e).__name__}: {e}"})
        task.status = "failed"
        task.errors = errors
        task.end_time = date.today()
        db.commit()
        print(f'[batch-sync #{task_id}] 失败: {e}')
    finally:
        # 清理停止信号，避免内存泄漏
        _stop_events.pop(task_id, None)
        db.close()


@router.post("/run/batch-sync", response_model=BdsSyncTaskOut)
def run_batch_sync(req: BatchSyncRequest):
    """启动批量同步任务（后台线程异步执行）。

    启动前检查是否已有 status='running' 的任务：有则返回 409，防止重复启动。
    立即返回 task 对象（status=pending），前端可轮询 GET /sync-tasks/{id} 查看进度。
    """
    db = next(get_db())  # 获取请求 session（用于查 + 建 task，线程内另起 session）
    try:
        # 清理僵尸任务（running 超 5 分钟未更新 = 后端崩溃残留）
        _cleanup_zombies(db, minutes=5)

        running = db.query(BdsSyncTask).filter(BdsSyncTask.status == "running").first()
        if running:
            raise HTTPException(status_code=409, detail=f"已有进行中的任务 #{running.id}，请等待完成后再启动")

        # scope 参数校验
        if req.scope not in ("all", "index", "industry", "explicit"):
            raise HTTPException(status_code=400, detail=f"非法 scope: {req.scope}")
        if req.scope == "index" and not req.index_code:
            raise HTTPException(status_code=400, detail="scope=index 时 index_code 必填")
        if req.scope == "industry" and not req.industry:
            raise HTTPException(status_code=400, detail="scope=industry 时 industry 必填")
        if req.scope == "explicit" and not req.symbols:
            raise HTTPException(status_code=400, detail="scope=explicit 时 symbols 必填")

        # 组装 scope_params
        scope_params = {}
        if req.scope == "index":
            scope_params["index_code"] = req.index_code
        elif req.scope == "industry":
            scope_params["industry_name"] = req.industry
        elif req.scope == "explicit":
            scope_params["symbols"] = req.symbols

        task = BdsSyncTask(
            scope=req.scope,
            scope_params=scope_params,
            tables=req.tables or TABLES,
            mark_synced=1 if req.mark_synced else 0,
            exclude_synced_today=1 if req.exclude_synced_today else 0,
            max_symbols=req.max_symbols,
            status="pending",
            total_count=0,
            done_count=0,
            success_count=0,
            no_data_count=0,
            error_count=0,
            progress=0,
            errors=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # 注册停止信号（后台线程从 _stop_events[task.id] 取；/stop/batch-sync 端点 set）
        _stop_events[task.id] = threading.Event()

        # 启动后台线程（daemon=True：进程退出时自动终止）
        t = threading.Thread(target=_run_batch_sync_thread, args=(task.id, req), daemon=True)
        t.start()

        return task

    finally:
        db.close()


@router.get("/sync-tasks", response_model=List[BdsSyncTaskOut])
def list_sync_tasks(limit: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)):
    """查询批量同步任务列表。顺带清理僵尸任务（running 超 5 分钟未更新）。"""
    _cleanup_zombies(db, minutes=5)
    items = db.query(BdsSyncTask).order_by(BdsSyncTask.create_time.desc()).limit(limit).all()
    return items


@router.get("/sync-tasks/latest", response_model=BdsSyncTaskOut)
def get_latest_sync_task(db: Session = Depends(get_db)):
    """查询最新一条批量同步任务（供前端轮询进度）。顺带清理僵尸任务。"""
    _cleanup_zombies(db, minutes=5)
    task = db.query(BdsSyncTask).order_by(BdsSyncTask.create_time.desc()).first()
    if not task:
        raise HTTPException(status_code=404, detail="暂无同步任务记录")
    return task


@router.get("/sync-tasks/{task_id}", response_model=BdsSyncTaskOut)
def get_sync_task(task_id: int, db: Session = Depends(get_db)):
    """查询单条批量同步任务详情（含 progress / errors）。顺带清理僵尸任务。"""
    _cleanup_zombies(db, minutes=5)
    task = db.query(BdsSyncTask).get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 #{task_id} 不存在")
    return task


@router.post("/stop/batch-sync")
def stop_batch_sync(task_id: int = Query(..., description="要停止的任务 ID")):
    """停止正在运行的批量同步任务。

    原理：对该任务对应的 threading.Event.set()。后台线程每次 sync_one 返回后检查，
    发现 set 就跳出循环，不再启动下一次 sync_one。正在执行的 HTTP 请求无法中断，
    等它自然返回后就停。任务状态最终会被置为 stopped。

    - pending（还没开始跑）：Event.set() 后线程进入 run_tasks 会立即停止，状态=stopped
    - running（正在跑）：等当前 symbol×table 的 sync_one 返回后停止
    - done/failed/stopped：返回 400，任务已结束，无需停止
    """
    ev = _stop_events.get(task_id)
    if ev is None:
        raise HTTPException(status_code=404, detail=f"任务 #{task_id} 不在运行中或不存在")

    if ev.is_set():
        return {"status": "already_stopped", "message": f"任务 #{task_id} 已发出停止信号"}

    ev.set()
    print(f'[batch-sync] 已发送停止信号 task #{task_id}')
    return {"status": "stopping", "message": f"任务 #{task_id} 停止信号已发出，正在等待当前请求返回..."}