# -*- coding: utf-8 -*-
"""bds 应用路由（从 server_dj/apps/bds/admin.py 迁移）。

提供 2 个 GET 查询路由与 2 个 POST 同步路由：
- GET  /bds/trade-dates       查询交易日历（对应 TradeDateAdmin）
- GET  /bds/symbol-infos      查询证券信息（对应 SymbolInfoAdmin）
- POST /bds/sync/trade-date   同步交易日历
- POST /bds/sync/symbol-info  同步证券信息
"""
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from server_fast.app.bds.models import IndexConstituent, IndexHistory, SymbolInfo, TradeDate
from server_fast.app.bds.schemas import (
    IndexConstituentOut,
    IndexHistoryOut,
    SymbolInfoOut,
    TradeDateOut,
)
from server_fast.app.bds.service import (
    insert_trade_date_em_sql,
    upsert_index_constituent_sql,
    upsert_index_history_sql,
    upsert_symbol_info_excel_sql,
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
    name: Optional[str] = Query(None, description="名称模糊匹配（search_fields）"),
    industry: Optional[str] = Query(None, description="行业精确匹配（list_filter）"),
    limit: int = Query(86, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询证券基本信息，支持可选过滤。

    过滤规则（依据 admin.py）：
    - search_fields(symbol, name) → contains 模糊匹配
    - list_filter(industry)       → == 精确匹配
    - list_per_page 默认 86
    """
    query = db.query(SymbolInfo)
    if symbol:
        query = query.filter(SymbolInfo.symbol.contains(symbol))
    if name:
        query = query.filter(SymbolInfo.name.contains(name))
    if industry:
        query = query.filter(SymbolInfo.industry == industry)
    total = query.count()  # 应用所有过滤条件后的总记录数
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


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
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
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
    if end_date:
        query = query.filter(IndexHistory.trade_date <= end_date)
    total = query.count()
    items = query.order_by(IndexHistory.trade_date.desc()).offset(offset).limit(limit).all()
    # 附加 sec_name 字段（从 Config.INDEX_CODE 查找，不存数据库）
    items_dict = []
    for item in items:
        d = item.to_dict()
        d["sec_name"] = Config.INDEX_CODE.get(item.symbol, {}).get("sec_name")
        items_dict.append(d)
    return {"items": items_dict, "total": total, "limit": limit, "offset": offset}


@router.get("/index-constituents", response_model=PageResponse[IndexConstituentOut])
def list_index_constituents(
    index_code: Optional[List[str]] = Query(default=None, description="指数代码多选精确匹配"),
    symbol: Optional[str] = Query(default=None, description="成分股代码模糊匹配"),
    start_date: Optional[date] = Query(default=None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[date] = Query(default=None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询指数成分股，支持指数代码多选、成分股代码模糊匹配和日期范围筛选。

    响应中每条记录附加 sec_name 字段（从 Config.INDEX_CODE 查找）。
    """
    query = db.query(IndexConstituent)
    # index_code 多选 IN 过滤
    if index_code:
        query = query.filter(IndexConstituent.index_code.in_(index_code))
    # symbol 模糊匹配
    if symbol:
        query = query.filter(IndexConstituent.symbol.contains(symbol))
    # 日期范围
    if start_date:
        query = query.filter(IndexConstituent.trade_date >= start_date)
    if end_date:
        query = query.filter(IndexConstituent.trade_date <= end_date)
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