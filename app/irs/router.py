# -*- coding: utf-8 -*-
"""irs 应用路由。

提供查询路由与同步路由：
- GET  /irs/value-monitors       估值监测
- GET  /irs/discounts-monitor    贴水监测
- GET  /irs/option-monitors      期权监测
- POST /irs/sync/{target}        按 target 触发对应 service 函数链（4 种 target）
"""
from datetime import date
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server_fast.app.irs import service
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    DiscountMonitor,
    OptionMonitor,
    ValueMonitor,
)
from server_fast.app.irs.schemas import (
    DiscountMonitorOut,
    OptionMonitorOut,
    ValueMonitorCreate,
    ValueMonitorOut,
    ValueMonitorUpdate,
)
from server_fast.common.db import get_db
from server_fast.common.pagination import PageResponse

router = APIRouter(prefix="/irs", tags=["irs"])


# =========================================================================
# 同步任务：target -> service 函数链
# =========================================================================

def _sync_discount_symbol():
    """discount-symbol：从 Config 同步贴水配置 + 更新贴水数据 + 同步实时行情。

    依次执行四步：
    1. upsert_discount_monitor_config_sql：从 Config 写入 symbol_type/con_name
    2. upsert_discount_monitor_em_sql：gm SDK 获取真实合约信息，更新 symbol/delisted_date
    3. update_is_main_em_sql：gm SDK 获取主力合约集合，更新 is_main 标志
    4. discount_yield_em_orm：gm SDK 获取实时行情，触发钩子计算 discount/ratio/ratio_y/days_left
       （第 2 步更新 delisted_date 后需经 ORM flush 才能重算 days_left，故追加此步）
    """
    service.upsert_discount_monitor_config_sql()
    service.upsert_discount_monitor_em_sql()
    service.update_is_main_em_sql()
    service.discount_yield_em_orm()


# 4 种 target -> 同步函数链映射
SYNC_MAP: Dict[str, List[Callable]] = {
    "discount-symbol":   [_sync_discount_symbol],
    "discount-monitor":  [service.discount_yield_em_orm],
    "value-monitor-hlc": [service.update_value_monitor_hlc_sql],
    "value-monitor":     [service.update_value_monitor_em_orm],
}


def _run_sync_chain(target: str, funcs: List[Callable]) -> dict:
    """依次执行同步函数链；参照 bds 模块返回 status 字段。

    - 任一函数抛异常：返回 status=error，前端显示红色提示并携带错误信息
    - 全部成功：返回 status=success，message 包含返回值信息（如有）
    """
    try:
        counts = []  # 收集各函数返回值（如有）
        for func in funcs:
            result = func()
            if result is not None:
                counts.append(result)
        # 构建含条数信息的 message（discount_yield_em_orm 返回 (insert, update) 元组）
        if counts:
            parts = []
            for c in counts:
                if isinstance(c, tuple) and len(c) == 2:
                    parts.append(f"新增{c[0]}条，更新{c[1]}条")
                elif isinstance(c, int):
                    parts.append(f"处理{c}条")
            detail = "；".join(parts) if parts else ""
            message = f"同步完成：{target}，{detail}" if detail else f"同步完成：{target}"
        else:
            message = f"同步完成：{target}"
        return {"status": "success", "message": message}
    except Exception as e:
        return {"status": "error", "message": f"同步失败：{target}，{e}"}


# =========================================================================
# GET 查询路由（5 个，对应原 Admin 注册的模型）
# =========================================================================

@router.get("/value-monitors", response_model=PageResponse[ValueMonitorOut])
def list_value_monitors(
    symbol: Optional[str] = Query(None, description="代码精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """估值监测（对应 ValueMonitor 独立表，支持代码精确匹配）。"""
    query = db.query(ValueMonitor)
    if symbol:
        query = query.filter(ValueMonitor.symbol == symbol)
    total = query.count()
    items = query.order_by(ValueMonitor.symbol).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/value-monitors")
def create_value_monitor(
    payload: ValueMonitorCreate,
    db: Session = Depends(get_db),
):
    """新增估值监测记录。

    接收 7 个必填字段，插入 irs_value_monitor 表。
    symbol 重复时返回 HTTP 400。
    """
    try:
        vm = ValueMonitor(
            symbol=payload.symbol,
            name=payload.name,
            pp_el=payload.pp_el,
            pp_l=payload.pp_l,
            pp_m=payload.pp_m,
            pp_h=payload.pp_h,
            pp_eh=payload.pp_eh,
        )
        db.add(vm)
        db.commit()
        return {"status": "success", "message": "新增成功"}
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"代码已存在：{payload.symbol}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"新增失败：{str(e)}")


@router.put("/value-monitors/{id_}")
def update_value_monitor(
    id_: int,
    payload: ValueMonitorUpdate,
    db: Session = Depends(get_db),
):
    """修改估值监测记录。

    按 id 更新 6 个必填字段（symbol 不可改）；
    py_close/y_high/y_low/price 为可选行情字段，None 表示不修改（兜底编辑）。
    id 不存在时返回 HTTP 404。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        vm.name = payload.name
        vm.pp_el = payload.pp_el
        vm.pp_l = payload.pp_l
        vm.pp_m = payload.pp_m
        vm.pp_h = payload.pp_h
        vm.pp_eh = payload.pp_eh
        # 行情字段：非 None 才更新（None 表示不修改）
        if payload.py_close is not None:
            vm.py_close = payload.py_close
        if payload.y_high is not None:
            vm.y_high = payload.y_high
        if payload.y_low is not None:
            vm.y_low = payload.y_low
        if payload.price is not None:
            vm.price = payload.price
        db.commit()
        return {"status": "success", "message": "修改成功"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"修改失败：{str(e)}")


@router.delete("/value-monitors/{id_}")
def delete_value_monitor(
    id_: int,
    db: Session = Depends(get_db),
):
    """删除估值监测记录。

    按 id 删除记录。id 不存在时返回 HTTP 404。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        db.delete(vm)
        db.commit()
        return {"status": "success", "message": "删除成功"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败：{str(e)}")


@router.get("/discounts-monitor", response_model=PageResponse[DiscountMonitorOut])
def list_discounts_monitor(
    symbol_type: Optional[str] = Query(None, description="合约类别精确匹配"),
    con_name: Optional[str] = Query(None, description="连续周期精确匹配"),
    is_main: Optional[bool] = Query(None, description="是否主力精确匹配"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """贴水监测（合并配置+监测单表，对应 DiscountMonitor）。

    查询前先触发 discount_yield_em_orm() 同步实时贴水数据（失败不阻塞查询），
    再返回最新数据。过滤条件直接作用于 DiscountMonitor 字段（无 JOIN）。
    """
    # 查询前先同步实时贴水数据，失败不阻塞查询
    try:
        service.discount_yield_em_orm()
    except Exception as e:
        print(f"-->discounts-monitor 同步失败:{e}")
    query = db.query(DiscountMonitor)
    if symbol_type:
        query = query.filter(DiscountMonitor.symbol_type == symbol_type)
    if con_name:
        query = query.filter(DiscountMonitor.con_name == con_name)
    if is_main is not None:
        query = query.filter(DiscountMonitor.is_main == is_main)
    total = query.count()
    items = query.order_by(DiscountMonitor.symbol_con).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/discount-options")
def list_discount_options():
    """返回贴水监测下拉选项（数据源 Config.SYMBOL_CON_LIST，无数据库查询）。

    从配置字典提取去重的 symbol_types 和 con_names 列表，供前端 NSelect 使用。
    """
    symbol_types = sorted({
        v['symbol_type'] for v in IrsCfg.SYMBOL_CON_LIST.values()
    })
    con_names = sorted({
        v['con_name'] for v in IrsCfg.SYMBOL_CON_LIST.values()
    })
    return {"symbol_types": symbol_types, "con_names": con_names}


@router.get("/option-monitors", response_model=PageResponse[OptionMonitorOut])
def list_option_monitors(
    underlying_symbol: Optional[str] = Query(None, description="标的代码精确匹配"),
    option_type: Optional[str] = Query(None, description="期权类型(call/put)精确匹配"),
    symbol: Optional[str] = Query(None, description="期权代码模糊匹配"),
    end_month: Optional[str] = Query(None, description="到期月(YYYYMM)，按 delisted_date 所在月范围筛选"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """期权监测合并（对应 OptionMonitor，合并配置+监测单表）。

    过滤条件直接作用于 OptionMonitor 字段（无 JOIN，因 underlying_symbol 已是本表字符串字段）。
    end_month 按 delisted_date 所在月范围筛选（含顺延至下月初的情况，取该月第1天至下月第1天前）。
    按标的代码、行权价升序排列。
    """
    query = db.query(OptionMonitor)
    if underlying_symbol:
        query = query.filter(OptionMonitor.underlying_symbol == underlying_symbol)
    if option_type:
        query = query.filter(OptionMonitor.option_type == option_type)
    if symbol:
        query = query.filter(OptionMonitor.symbol.like(f"%{symbol}%"))
    if end_month:
        # end_month 格式 "YYYYMM"，转为 delisted_date 所在月范围
        year = int(end_month[:4])
        month = int(end_month[4:])
        month_start = date(year, month, 1)
        # 下月第1天（12月则跨年至次年1月）
        month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        query = query.filter(
            OptionMonitor.delisted_date >= month_start,
            OptionMonitor.delisted_date < month_end,
        )
    total = query.count()
    items = query.order_by(
        OptionMonitor.underlying_symbol.asc(),
        OptionMonitor.price_strike.desc(),
        OptionMonitor.option_type.asc(),
    ).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/option-underlyings")
def list_option_underlyings():
    """返回期权标的下拉选项（数据源 Config.OPTIONS_MARCH，无数据库查询）。

    从配置元组提取 underlying_symbol 列表，供前端 NSelect 使用。
    label 格式为 `option_name`，value 为 underlying_symbol。
    """
    underlying_symbols = [
        {"label": item['option_name'], "value": item['underlying_symbol']}
        for item in IrsCfg.OPTIONS_MARCH
    ]
    return {"underlying_symbols": underlying_symbols}


# =========================================================================
# POST 同步路由
# =========================================================================

@router.post("/sync/option-monitor")
def sync_option_monitor(
    option_name: str = Query(..., description="期权品种名称（如 沪深300股指期权）"),
    end_month: str = Query(..., description="到期年月，格式 YYYYMM（如 202608）"),
):
    """同步期权行情（akshare 获取期权行情 + gm 获取标的现价）。

    需放在 /sync/{target} 之前注册：FastAPI 按注册顺序匹配，具体路径优先于路径参数。
    不加入 SYNC_MAP（需额外参数，与其他无参数 target 不同）。
    """
    try:
        result = service.option_monitor_sync_orm(option_name, end_month)
        return {"status": "success", "message": f"同步完成：option-monitor，{result} 条"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败：{str(e)}")


@router.post("/clean/option-monitor")
def clean_option_monitor(db: Session = Depends(get_db)):
    """清理已到期期权数据：删除剩余天数 days_left <= 0 的记录。

    返回删除条数。days_left 由模型钩子按 delisted_date 实时计算，已到期合约保留无意义。
    """
    deleted = db.query(OptionMonitor).filter(OptionMonitor.days_left <= 0).delete(synchronize_session=False)
    db.commit()
    return {"status": "success", "message": f"清理完成：option-monitor，删除{deleted}条"}


@router.post("/sync/{target}")
def sync_data(target: str):
    """根据 target 触发对应同步逻辑。

    target 取值见 SYNC_MAP；service 函数内部自管理 session，无需注入 db。
    """
    funcs = SYNC_MAP.get(target)
    if funcs is None:
        raise HTTPException(status_code=400, detail=f"unknown target: {target}")
    return _run_sync_chain(target, funcs)
