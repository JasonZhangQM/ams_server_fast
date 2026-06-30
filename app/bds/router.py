# -*- coding: utf-8 -*-
"""bds 应用路由（从 server_dj/apps/bds/admin.py 迁移）。

提供 2 个 GET 查询路由与 2 个 POST 同步路由：
- GET  /bds/trade-dates       查询交易日历（对应 TradeDateAdmin）
- GET  /bds/symbol-infos      查询证券信息（对应 SymbolInfoAdmin）
- POST /bds/sync/trade-date   同步交易日历
- POST /bds/sync/symbol-info  同步证券信息
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from server_fast.app.bds.models import SymbolInfo, TradeDate
from server_fast.app.bds.schemas import SymbolInfoOut, TradeDateOut
from server_fast.app.bds.service import (
    insert_trade_date_em_sql,
    upsert_symbol_info_excel_sql,
)
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
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询交易日历全量数据（对应 TradeDateAdmin，仅分页）。"""
    query = db.query(TradeDate)
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
    """同步交易日历（触发 insert_trade_date_em_sql）。"""
    return _run_sync("trade-date", insert_trade_date_em_sql)


@router.post("/sync/symbol-info")
def sync_symbol_info():
    """同步证券信息（触发 upsert_symbol_info_excel_sql）。"""
    return _run_sync("symbol-info", upsert_symbol_info_excel_sql)
