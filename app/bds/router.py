# -*- coding: utf-8 -*-
"""bds 应用路由（从 server_dj/apps/bds/admin.py 迁移）。

提供 2 个 GET 查询路由与 2 个 POST 同步路由：
- GET  /bds/trade-dates         查询交易日历（对应 TradeDateAdmin）
- GET  /bds/symbol-infos        查询证券信息（对应 SymbolInfoAdmin）
- POST /bds/sync/trade-date     同步交易日历
- POST /bds/sync/symbol-info    同步证券信息
"""
from datetime import date, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as PydanticModel
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, text

from server_fast.app.bds.models import DailyValuation, EconomicIndicator, FinanceDeriv, FundBalance, FundCashflow, FundIncome, GoldReserve, IndexConstituent, IndexHistory, SymbolInfo, TradeDate, YieldIndicator
from server_fast.app.bds.schemas import (
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
    YieldIndicatorOut,
)
from server_fast.app.bds.services import (
    fetch_realtime_index_prices,
    insert_trade_date_em_sql,
    upsert_all_economic_indicators_sql,
    upsert_all_gold_reserves_sql,
    upsert_all_yield_indicators_sql,
    upsert_daily_valuation_sql,
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
    upsert_yield_indicator_sql,
)
from server_fast.app.bds.config import Config
from server_fast.common.db import get_db
from server_fast.common.pagination import PageResponse

router = APIRouter(prefix="/bds", tags=["bds"])


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

    - -1：同步失败（gm 终端未启动或接口异常）
    - 0：无数据可导入
    - >0：成功导入 count 条
    """
    count = steps.get(symbol, 0)
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
    symbol: Optional[str] = Query(None, description="代码模糊匹配（search_fields）"),
    industry: Optional[str] = Query(None, description="行业精确匹配（list_filter）"),
    keyword: Optional[str] = Query(None, description="代码或名称任一模糊匹配（OR，用于远程搜索）"),
    limit: int = Query(86, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询证券基本信息，支持可选过滤。

    过滤规则（依据 admin.py）：
    - search_fields(symbol)      → contains 模糊匹配
    - list_filter(industry)      → == 精确匹配
    - list_per_page 默认 86
    - keyword → symbol/name 任一包含即命中（OR），用于前端远程搜索场景
    """
    query = db.query(SymbolInfo)
    if symbol:
        query = query.filter(SymbolInfo.symbol.contains(symbol))
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
    index_code: Optional[List[str]] = Query(default=None, description="指数代码多选精确匹配"),
    symbol: Optional[str] = Query(default=None, description="成分股代码模糊匹配"),
    trade_date: Optional[date] = Query(default=None, description="交易日期 YYYY-MM-DD"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询指数成分股，支持指数代码多选、成分股代码模糊匹配和具体交易日期筛选。

    响应中每条记录附加 sec_name 字段（从 Config.INDEX_CODE 查找）。
    """
    query = db.query(IndexConstituent)
    # index_code 多选 IN 过滤
    if index_code:
        query = query.filter(IndexConstituent.index_code.in_(index_code))
    # symbol 模糊匹配
    if symbol:
        query = query.filter(IndexConstituent.symbol.contains(symbol))
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
        default=None, description="起始日期 YYYY-MM-DD，默认当前日期前30天"
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
    # 默认起始日：当前日期前 30 天
    if start_date is None:
        start_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")

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
def sync_fund_balance(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步资产负债表数据，接收单个股票代码，获取并入库。"""
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_fund_balance_sql([symbol])
    return _build_sync_response(symbol, steps)


@router.get("/fund-balances", response_model=PageResponse[FundBalanceOut])
def list_fund_balances(
    symbol: Optional[str] = Query(default=None, description="股票代码模糊匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询资产负债表数据，支持代码模糊匹配、报表类型和报告日期起始日筛选。

    排序规则：rpt_date 降序，同 rpt_date 按 pub_date 降序。
    """
    query = db.query(FundBalance)
    # symbol 模糊匹配
    if symbol:
        query = query.filter(FundBalance.symbol.like(f"%{symbol}%"))
    # rpt_type 精确匹配
    if rpt_type is not None:
        query = query.filter(FundBalance.rpt_type == rpt_type)
    # 报告日期起始日过滤
    if start_date:
        query = query.filter(FundBalance.rpt_date >= start_date)
    total = query.count()
    items = (
        query.order_by(FundBalance.rpt_date.desc(), FundBalance.pub_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/fund-income")
def sync_fund_income(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步利润表数据，接收单个股票代码，获取并入库。"""
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_fund_income_sql([symbol])
    return _build_sync_response(symbol, steps)


@router.get("/fund-incomes", response_model=PageResponse[FundIncomeOut])
def list_fund_incomes(
    symbol: Optional[str] = Query(default=None, description="股票代码模糊匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询利润表数据，支持代码模糊匹配、报表类型和报告日期起始日筛选。

    排序规则：rpt_date 降序，同 rpt_date 按 pub_date 降序。
    """
    query = db.query(FundIncome)
    # symbol 模糊匹配
    if symbol:
        query = query.filter(FundIncome.symbol.like(f"%{symbol}%"))
    # rpt_type 精确匹配
    if rpt_type is not None:
        query = query.filter(FundIncome.rpt_type == rpt_type)
    # 报告日期起始日过滤
    if start_date:
        query = query.filter(FundIncome.rpt_date >= start_date)
    total = query.count()
    items = (
        query.order_by(FundIncome.rpt_date.desc(), FundIncome.pub_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/fund-cashflow")
def sync_fund_cashflow(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步现金流量表数据，接收单个股票代码，获取并入库。"""
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_fund_cashflow_sql([symbol])
    return _build_sync_response(symbol, steps)


@router.get("/fund-cashflows", response_model=PageResponse[FundCashflowOut])
def list_fund_cashflows(
    symbol: Optional[str] = Query(default=None, description="股票代码模糊匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询现金流量表数据，支持代码模糊匹配、报表类型和报告日期起始日筛选。

    排序规则：rpt_date 降序，同 rpt_date 按 pub_date 降序。
    """
    query = db.query(FundCashflow)
    # symbol 模糊匹配
    if symbol:
        query = query.filter(FundCashflow.symbol.like(f"%{symbol}%"))
    # rpt_type 精确匹配
    if rpt_type is not None:
        query = query.filter(FundCashflow.rpt_type == rpt_type)
    # 报告日期起始日过滤
    if start_date:
        query = query.filter(FundCashflow.rpt_date >= start_date)
    total = query.count()
    items = (
        query.order_by(FundCashflow.rpt_date.desc(), FundCashflow.pub_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/finance-deriv")
def sync_finance_deriv(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步财务指标数据，接收单个股票代码，获取并入库。"""
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_finance_deriv_sql([symbol])
    return _build_sync_response(symbol, steps)


@router.get("/finance-derivs", response_model=PageResponse[FinanceDerivOut])
def list_finance_derivs(
    symbol: Optional[str] = Query(default=None, description="股票代码模糊匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询财务指标数据，支持代码模糊匹配、报表类型和报告日期起始日筛选。

    排序规则：rpt_date 降序，同 rpt_date 按 pub_date 降序。
    """
    query = db.query(FinanceDeriv)
    # symbol 模糊匹配
    if symbol:
        query = query.filter(FinanceDeriv.symbol.like(f"%{symbol}%"))
    # rpt_type 精确匹配
    if rpt_type is not None:
        query = query.filter(FinanceDeriv.rpt_type == rpt_type)
    # 报告日期起始日过滤
    if start_date:
        query = query.filter(FinanceDeriv.rpt_date >= start_date)
    total = query.count()
    items = (
        query.order_by(FinanceDeriv.rpt_date.desc(), FinanceDeriv.pub_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/daily-valuation")
def sync_daily_valuation(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步估值指标数据，接收单个股票代码，获取并入库。"""
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps = upsert_daily_valuation_sql([symbol])
    return _build_sync_response(symbol, steps)


@router.get("/daily-valuations", response_model=PageResponse[DailyValuationOut])
def list_daily_valuations(
    symbol: Optional[str] = Query(default=None, description="股票代码模糊匹配"),
    start_date: Optional[date] = Query(default=None, description="交易日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询估值指标数据，支持代码模糊匹配和交易日期范围筛选。

    排序规则：trade_date 降序。
    """
    query = db.query(DailyValuation)
    # symbol 模糊匹配
    if symbol:
        query = query.filter(DailyValuation.symbol.like(f"%{symbol}%"))
    # 交易日期起始日过滤
    if start_date:
        query = query.filter(DailyValuation.trade_date >= start_date)
    total = query.count()
    items = (
        query.order_by(DailyValuation.trade_date.desc())
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


@router.get("/economic-indicators/latest", response_model=List[EconomicIndicatorOut])
def list_economic_indicators_latest(db: Session = Depends(get_db)):
    """查询各指标最新值（每个 indicator_code 取 report_date 降序第一条）。

    实现方式：子查询获取每个 indicator_code 的最大 report_date，
    再 join 主表取对应记录，等价于：
    SELECT * FROM bds_economic_indicator
    WHERE (indicator_code, report_date) IN (
        SELECT indicator_code, MAX(report_date)
        FROM bds_economic_indicator GROUP BY indicator_code)
    """
    # 子查询：每个 indicator_code 的最大 report_date
    subq = (
        db.query(
            EconomicIndicator.indicator_code,
            func.max(EconomicIndicator.report_date).label("max_date"),
        )
        .group_by(EconomicIndicator.indicator_code)
        .subquery()
    )
    # join 主表取对应记录
    items = (
        db.query(EconomicIndicator)
        .join(
            subq,
            (EconomicIndicator.indicator_code == subq.c.indicator_code)
            & (EconomicIndicator.report_date == subq.c.max_date),
        )
        .all()
    )
    return [item.to_dict() for item in items]


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


@router.get("/yield-indicator-codes")
def list_yield_indicator_codes():
    """返回美债收益率指标配置列表（数据源 Config.YIELD_INDICATORS，无数据库查询）。

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
        for code, info in Config.YIELD_INDICATORS.items()
    ]
    return indicators


@router.get("/yield-indicators", response_model=PageResponse[YieldIndicatorOut])
def list_yield_indicators(
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
    query = db.query(YieldIndicator)
    # indicator_code 多选 IN 过滤
    if indicator_code:
        query = query.filter(YieldIndicator.indicator_code.in_(indicator_code))
    # 报告日期范围过滤
    if start_date:
        query = query.filter(YieldIndicator.report_date >= start_date)
    if end_date:
        query = query.filter(YieldIndicator.report_date <= end_date)
    total = query.count()
    items = (
        query.order_by(YieldIndicator.report_date.desc(), YieldIndicator.indicator_code.asc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/yield-indicator")
def sync_yield_indicator(indicator_code: str = Query(..., description="指标代码，精确匹配单个指标")):
    """同步单个美债收益率指标数据。

    返回值说明：
    - status: success/no_data/error
    - message: 同步结果描述信息
    - indicator_code: 指标代码
    - count: 插入/更新条数（-1 表示失败）
    """
    if not indicator_code:
        return {"status": "error", "message": "indicator_code 不能为空",
                "indicator_code": indicator_code, "count": -1}
    if indicator_code not in Config.YIELD_INDICATORS:
        return {"status": "error", "message": f"未知收益率指标代码：{indicator_code}",
                "indicator_code": indicator_code, "count": -1}
    count = upsert_yield_indicator_sql(indicator_code)
    if count == -1:
        return {"status": "error", "message": f"同步失败：{indicator_code}",
                "indicator_code": indicator_code, "count": -1}
    if count == 0:
        return {"status": "no_data", "message": f"无数据可导入：{indicator_code}",
                "indicator_code": indicator_code, "count": 0}
    return {"status": "success", "message": f"同步完成：{indicator_code}，更新 {count} 条",
            "indicator_code": indicator_code, "count": count}


@router.post("/sync/yield-indicators-all")
def sync_yield_indicators_all():
    """全量同步所有美债收益率指标数据。

    遍历 Config.YIELD_INDICATORS 中 4 个指标代码，逐个调用同步函数。
    单指标失败不中断，返回 steps 包含每个指标的结果。

    返回值说明：
    - status: success
    - message: 同步结果描述信息
    - steps: {indicator_code: count, ...} 各指标同步条数字典
    """
    try:
        steps = upsert_all_yield_indicators_sql()
        return {"status": "success", "message": "美债收益率指标全量同步完成", "steps": steps}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))