# -*- coding: utf-8 -*-
"""irs 应用路由（从 server_dj/apps/irs/admin.py + middleware.py 迁移）。

提供 8 个 GET 查询路由 + 1 个 POST 同步路由：
- GET  /irs/value-monitor        估值监测（先同步实时估值再返回，对应 MonitorValueAdmin）
- GET  /irs/symbol-values        估值配置（对应 SymbolValueAdmin）
- GET  /irs/symbol-kpis          估值指标（对应 SymbolKpiAdmin）
- GET  /irs/symbol-options       期权配置（对应 SymbolOptionAdmin）
- GET  /irs/monitor-options      期权监测（对应 MonitorOptionAdmin）
- GET  /irs/monitor-option-ts    期权T型报价（对应 MonitorOptionTAdmin）
- GET  /irs/symbol-discounts     贴水配置（对应 SymbolDiscountAdmin）
- GET  /irs/monitor-discounts    贴水监测（对应 MonitorDiscountAdmin）
- POST /irs/sync/{target}        按 target 触发对应 service 函数链（9 种 target）
"""
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from server_fast.app.irs import service
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    MonitorDiscount,
    MonitorOption,
    MonitorOptionT,
    MonitorValue,
    SymbolDiscount,
    SymbolKpi,
    SymbolOption,
    SymbolUnderlying,
    SymbolValue,
)
from server_fast.app.irs.schemas import (
    MonitorDiscountOut,
    MonitorOptionOut,
    MonitorOptionTOut,
    MonitorValueOut,
    SymbolDiscountOut,
    SymbolKpiOut,
    SymbolOptionOut,
    SymbolValueOut,
)
from server_fast.common.db import get_db
from server_fast.common.pagination import PageResponse

router = APIRouter(prefix="/irs", tags=["irs"])


# =========================================================================
# 序列化辅助
# =========================================================================

def _resolve_field(obj, field_path: str):
    """按 Django ORM 双下划线路径解析字段值（如 'symbol_value__symbol'）。

    支持多级关联访问；任一中间节点为 None 或属性缺失则返回 None。
    兼容原 fields_request 中可能不存在的字段（如 symbol_value__vr）。
    """
    current = obj
    for part in field_path.split("__"):
        if current is None:
            return None
        current = getattr(current, part, None)
    return current


def _serialize_with_related(item, extra_fields: Dict[str, str]) -> dict:
    """序列化 ORM 实例，并按 extra_fields 追加关联字段。

    :param extra_fields: {输出键: '关联路径'}，路径用双下划线分隔多级关系
        例 {'underlying_symbol': 'underlying__symbol',
            'ud_symbol': 'option__underlying__symbol'}
    """
    data = item.to_dict()
    for out_key, path in extra_fields.items():
        data[out_key] = _resolve_field(item, path)
    return data


# =========================================================================
# 同步任务：target -> service 函数链（依据 server_dj/apps/irs/middleware.py）
# =========================================================================

def _sync_symbol_value():
    """symbol-value：Excel 导入估值配置 + 更新历史行情(HLC)。"""
    service.upsert_model_excel_sql(IrsCfg.FOLDER_SYMBOL_VALUE, SymbolValue)
    service.update_symbol_value_hlc_sql()


def _sync_symbol_underlying():
    """symbol-underlying：Excel 导入期权标的。"""
    service.upsert_model_excel_sql(IrsCfg.FOLDER_OPTION, SymbolUnderlying)


def _sync_symbol_discount():
    """symbol-discount：Excel 导入贴水配置 + 更新贴水数据。"""
    service.upsert_model_excel_sql(IrsCfg.FOLDER_SYMBOL_CON, SymbolDiscount)
    service.upsert_discount_em_sql()


def _sync_monitor_option():
    """monitor-option：期权自更新(到期日) + Excel 期权/标的实时行情。"""
    service.symbol_option_update_self_orm()
    service.monitor_option_excel_orm()


def _sync_monitor_option_t():
    """monitor-option-t：期权自更新 + Excel 行情 + T型报价入库。"""
    service.symbol_option_update_self_orm()
    service.monitor_option_excel_orm()
    service.monitor_option_t_orm()


# 9 种 target -> 同步函数链映射（对应 middleware.py 各 Admin 路径触发逻辑）
SYNC_MAP: Dict[str, List[Callable]] = {
    "symbol-value":      [_sync_symbol_value],
    "symbol-kpi":        [service.symbol_value_em_orm],
    "monitor-value":     [service.monitor_value_em_orm],
    "symbol-option":     [service.symbol_option_update_self_orm],
    "symbol-underlying": [_sync_symbol_underlying],
    "monitor-option":    [_sync_monitor_option],
    "monitor-option-t":  [_sync_monitor_option_t],
    "symbol-discount":   [_sync_symbol_discount],
    "monitor-discount":  [service.discount_yield_em_orm],
}


def _run_sync_chain(target: str, funcs: List[Callable]) -> dict:
    """依次执行同步函数链；任一函数抛异常即中止并返回 500。"""
    try:
        for func in funcs:
            func()
        return {"status": "ok", "message": f"{target} synced"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# GET 查询路由（8 个，对应原 Admin 注册的 8 个模型）
# =========================================================================

@router.get("/value-monitor", response_model=PageResponse[MonitorValueOut])
def value_monitor(
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """估值监测（原 value_monitor 视图）。

    先触发 monitor_value_em_orm() 同步实时估值（失败不阻塞查询），
    再按 MonitorValue.fields_request 返回字段（含关联 SymbolValue 字段）。
    """
    # 同步实时数据，异常时仅打印日志，继续返回已有数据
    try:
        service.monitor_value_em_orm()
    except Exception as e:
        print(f"-->value-monitor 同步失败:{e}")
    query = db.query(MonitorValue)
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    # 按 fields_request 构建返回字段（路径如 symbol_value__symbol 自动解析）
    return {
        "items": [
            {field: _resolve_field(mv, field) for field in MonitorValue.fields_request}
            for mv in items
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/symbol-values", response_model=PageResponse[SymbolValueOut])
def list_symbol_values(
    symbol: Optional[str] = Query(None, description="代码精确匹配（search_fields）"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """估值配置（对应 SymbolValueAdmin，支持 symbol 过滤）。"""
    query = db.query(SymbolValue)
    if symbol:
        query = query.filter(SymbolValue.symbol == symbol)
    total = query.count()
    items = query.order_by(SymbolValue.m_tot.desc()).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/symbol-kpis", response_model=PageResponse[SymbolKpiOut])
def list_symbol_kpis(
    symbol: Optional[str] = Query(None, description="关联 SymbolValue.symbol 精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """估值指标（对应 SymbolKpiAdmin，支持 symbol 过滤，关联 SymbolValue）。"""
    query = db.query(SymbolKpi)
    if symbol:
        # 通过关联 SymbolValue 过滤
        query = query.join(
            SymbolValue, SymbolKpi.symbol_value_id == SymbolValue.id
        ).filter(SymbolValue.symbol == symbol)
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/symbol-options", response_model=PageResponse[SymbolOptionOut])
def list_symbol_options(
    underlying_symbol: Optional[str] = Query(None, description="标的代码精确匹配"),
    underlying_name: Optional[str] = Query(None, description="标的名称精确匹配"),
    price_strike: Optional[float] = Query(None, description="行权价精确匹配"),
    days_left: Optional[int] = Query(None, description="剩余天数精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """期权配置（对应 SymbolOptionAdmin，含关联 SymbolUnderlying 字段）。"""
    query = db.query(SymbolOption)
    # 需要关联标的信息时统一 join 一次
    if underlying_symbol or underlying_name:
        query = query.join(
            SymbolUnderlying, SymbolOption.underlying_id == SymbolUnderlying.id
        )
        if underlying_symbol:
            query = query.filter(SymbolUnderlying.symbol == underlying_symbol)
        if underlying_name:
            query = query.filter(SymbolUnderlying.name == underlying_name)
    if price_strike is not None:
        query = query.filter(SymbolOption.price_strike == price_strike)
    if days_left is not None:
        query = query.filter(SymbolOption.days_left == days_left)
    # JOIN 与过滤条件已应用，count 对主表 SymbolOption 安全
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    extra = {
        "underlying_symbol": "underlying__symbol",
        "underlying_name": "underlying__name",
        "underlying_multiplier": "underlying__multiplier",
    }
    return {"items": [_serialize_with_related(item, extra) for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/monitor-options", response_model=PageResponse[MonitorOptionOut])
def list_monitor_options(
    symbol: Optional[str] = Query(None, description="期权代码精确匹配"),
    underlying_symbol: Optional[str] = Query(None, description="标的代码精确匹配"),
    underlying_name: Optional[str] = Query(None, description="标的名称精确匹配"),
    option_type: Optional[str] = Query(None, description="期权类型(call/put)精确匹配"),
    days_left: Optional[int] = Query(None, description="剩余天数精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """期权监测（对应 MonitorOptionAdmin，含关联 SymbolOption/SymbolUnderlying 字段）。"""
    query = db.query(MonitorOption)
    if symbol:
        query = query.filter(MonitorOption.symbol == symbol)
    if option_type:
        query = query.filter(MonitorOption.option_type == option_type)
    # 需要 SymbolOption 关联字段时统一 join
    if days_left is not None or underlying_symbol or underlying_name:
        query = query.join(SymbolOption, MonitorOption.option_id == SymbolOption.id)
        if days_left is not None:
            query = query.filter(SymbolOption.days_left == days_left)
        if underlying_symbol or underlying_name:
            query = query.join(
                SymbolUnderlying, SymbolOption.underlying_id == SymbolUnderlying.id
            )
            if underlying_symbol:
                query = query.filter(SymbolUnderlying.symbol == underlying_symbol)
            if underlying_name:
                query = query.filter(SymbolUnderlying.name == underlying_name)
    # JOIN 与过滤条件已应用，count 对主表 MonitorOption 安全
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    extra = {
        "option_price_strike": "option__price_strike",
        "option_delisted_date": "option__delisted_date",
        "option_days_left": "option__days_left",
        "option_value_per": "option__value_per",
        "underlying_symbol": "option__underlying__symbol",
        "underlying_name": "option__underlying__name",
    }
    return {"items": [_serialize_with_related(item, extra) for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/monitor-option-ts", response_model=PageResponse[MonitorOptionTOut])
def list_monitor_option_ts(
    underlying_symbol: Optional[str] = Query(None, description="标的代码精确匹配"),
    underlying_name: Optional[str] = Query(None, description="标的名称精确匹配"),
    price_strike: Optional[float] = Query(None, description="行权价精确匹配"),
    days_left: Optional[int] = Query(None, description="剩余天数精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """期权T型报价（对应 MonitorOptionTAdmin，含关联 SymbolOption/SymbolUnderlying 字段）。"""
    query = db.query(MonitorOptionT)
    # 所有过滤均经由关联 SymbolOption，统一 join
    if any(v is not None for v in (underlying_symbol, underlying_name, price_strike, days_left)):
        query = query.join(SymbolOption, MonitorOptionT.option_id == SymbolOption.id)
        if price_strike is not None:
            query = query.filter(SymbolOption.price_strike == price_strike)
        if days_left is not None:
            query = query.filter(SymbolOption.days_left == days_left)
        if underlying_symbol or underlying_name:
            query = query.join(
                SymbolUnderlying, SymbolOption.underlying_id == SymbolUnderlying.id
            )
            if underlying_symbol:
                query = query.filter(SymbolUnderlying.symbol == underlying_symbol)
            if underlying_name:
                query = query.filter(SymbolUnderlying.name == underlying_name)
    # JOIN 与过滤条件已应用，count 对主表 MonitorOptionT 安全
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    extra = {
        "option_price_strike": "option__price_strike",
        "option_delisted_date": "option__delisted_date",
        "option_days_left": "option__days_left",
        "underlying_symbol": "option__underlying__symbol",
        "underlying_name": "option__underlying__name",
    }
    return {"items": [_serialize_with_related(item, extra) for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/symbol-discounts", response_model=PageResponse[SymbolDiscountOut])
def list_symbol_discounts(
    symbol_type: Optional[str] = Query(None, description="合约类别精确匹配（list_filter）"),
    is_main: Optional[bool] = Query(None, description="是否主力精确匹配（list_filter）"),
    symbol: Optional[str] = Query(None, description="真实合约代码精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """贴水配置（对应 SymbolDiscountAdmin，支持 symbol_type/is_main/symbol 过滤）。"""
    query = db.query(SymbolDiscount)
    if symbol_type:
        query = query.filter(SymbolDiscount.symbol_type == symbol_type)
    if is_main is not None:
        query = query.filter(SymbolDiscount.is_main == is_main)
    if symbol:
        query = query.filter(SymbolDiscount.symbol == symbol)
    total = query.count()
    items = query.order_by(SymbolDiscount.symbol_con).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/monitor-discounts", response_model=PageResponse[MonitorDiscountOut])
def list_monitor_discounts(
    symbol: Optional[str] = Query(None, description="关联 SymbolDiscount.symbol 精确匹配"),
    symbol_con: Optional[str] = Query(None, description="关联连续合约精确匹配"),
    symbol_type: Optional[str] = Query(None, description="关联合约类别精确匹配"),
    is_main: Optional[bool] = Query(None, description="关联是否主力精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """贴水监测（对应 MonitorDiscountAdmin，含关联 SymbolDiscount 字段）。"""
    query = db.query(MonitorDiscount)
    # 四个过滤均来自关联 SymbolDiscount，统一 join 一次
    if any(v is not None for v in (symbol, symbol_con, symbol_type, is_main)):
        query = query.join(
            SymbolDiscount, MonitorDiscount.symbol_real_id == SymbolDiscount.id
        )
        if symbol:
            query = query.filter(SymbolDiscount.symbol == symbol)
        if symbol_con:
            query = query.filter(SymbolDiscount.symbol_con == symbol_con)
        if symbol_type:
            query = query.filter(SymbolDiscount.symbol_type == symbol_type)
        if is_main is not None:
            query = query.filter(SymbolDiscount.is_main == is_main)
    # JOIN 与过滤条件已应用，count 对主表 MonitorDiscount 安全
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    extra = {
        "symbol": "symbol_real__symbol",
        "symbol_con": "symbol_real__symbol_con",
        "is_main": "symbol_real__is_main",
        "symbol_type": "symbol_real__symbol_type",
        "symbol_ud": "symbol_real__symbol_ud",
        "delisted_date": "symbol_real__delisted_date",
    }
    return {"items": [_serialize_with_related(item, extra) for item in items], "total": total, "limit": limit, "offset": offset}


# =========================================================================
# POST 同步路由
# =========================================================================

@router.post("/sync/{target}")
def sync_data(target: str):
    """根据 target 触发对应同步逻辑（对应 middleware.py 中 9 种 Admin 路径触发）。

    target 取值见 SYNC_MAP；service 函数内部自管理 session，无需注入 db。
    """
    funcs = SYNC_MAP.get(target)
    if funcs is None:
        raise HTTPException(status_code=400, detail=f"unknown target: {target}")
    return _run_sync_chain(target, funcs)


# =========================================================================
# 定时脚本拆分路由（对应 run.py 中 irs 部分，每个功能单独 POST 触发）
# 使用 /run/ 前缀，避免与 /sync/{target} 路径参数路由冲突
# =========================================================================

@router.post("/run/symbol-value-import")
def run_symbol_value_import():
    """估值数据导入（对应 run.py: upsert_model_excel_sql(FOLDER_SYMBOL_VALUE, SymbolValue)）。"""
    return _run_sync_chain(
        "symbol-value-import",
        [lambda: service.upsert_model_excel_sql(IrsCfg.FOLDER_SYMBOL_VALUE, SymbolValue)],
    )


@router.post("/run/symbol-underlying-import")
def run_symbol_underlying_import():
    """期权标的导入（对应 run.py: upsert_model_excel_sql(FOLDER_OPTION, SymbolUnderlying)）。"""
    return _run_sync_chain(
        "symbol-underlying-import",
        [lambda: service.upsert_model_excel_sql(IrsCfg.FOLDER_OPTION, SymbolUnderlying)],
    )


@router.post("/run/symbol-discount-import")
def run_symbol_discount_import():
    """贴水标的(连续合约)导入（对应 run.py: upsert_model_excel_sql(FOLDER_SYMBOL_CON, SymbolDiscount)）。"""
    return _run_sync_chain(
        "symbol-discount-import",
        [lambda: service.upsert_model_excel_sql(IrsCfg.FOLDER_SYMBOL_CON, SymbolDiscount)],
    )


@router.post("/run/discount-em")
def run_discount_em():
    """连续合约信息完善（对应 run.py: upsert_discount_em_sql）。"""
    return _run_sync_chain("discount-em", [service.upsert_discount_em_sql])


@router.post("/run/symbol-option-self")
def run_symbol_option_self():
    """SymbolOption 更新到期日（对应 run.py: symbol_option_update_self_orm）。"""
    return _run_sync_chain("symbol-option-self", [service.symbol_option_update_self_orm])
