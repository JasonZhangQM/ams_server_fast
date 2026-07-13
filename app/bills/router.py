# -*- coding: utf-8 -*-
"""bills 应用路由（Task 9）。

覆盖原 Django Admin 注册的 5 个模型查询
（Group / Bill / Profit / GroupAcc / GroupSymbol）
及原中间件触发的 3 个同步操作。

约定：
- GET 路由通过 Depends(get_db) 获取会话，直接查询模型；
- POST 同步路由调用 service 内部函数（service 内部自管理 session）；
- 过滤参数映射 Admin 配置：search_fields 用 contains（模糊），
  list_filter 用 ==（精确），与原 Django Admin 行为一致。
- 同步顺序与原 middleware.py 完全一致。
"""
from typing import Any, Callable, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from server_fast.app.bills.config import Config
from server_fast.app.bills.models import Bill, Group, GroupAcc, GroupSymbol, Profit, ProfitYear
from server_fast.app.bills.schemas import (
    BillOut,
    GroupAccOut,
    GroupOut,
    GroupSymbolOut,
    ProfitOut,
    ProfitYearOut,
)
from server_fast.common.db import get_db
from server_fast.common.pagination import PageResponse

router = APIRouter(prefix="/bills", tags=["bills"])


# 通用类别列表接口：返回 Config.MAP_CATEGORY 字典的所有 key 作为类别列表
@router.get("/categories")
def list_categories():
    """返回应用配置的交易类别列表。

    数据源为 Config.MAP_CATEGORY 字典的 key 集合，确保前端下拉选项
    与后端交易类型映射配置保持一致，无需依赖数据库实际数据。
    """
    categories = list(Config.MAP_CATEGORY.keys())
    return {"categories": categories}


# 通用账户列表接口：返回 Config.ACCOUNT_INFO 字典的所有 key 作为账户列表
@router.get("/accounts")
def list_accounts():
    """返回应用配置的账户列表。

    数据源为 Config.ACCOUNT_INFO 字典的 key 集合，确保前端下拉选项
    与后端账户配置保持一致，无需依赖数据库实际数据。
    """
    accounts = list(Config.ACCOUNT_INFO.keys())
    return {"accounts": accounts}


def _filter_query(
    query,
    conditions: List[Tuple[Any, Optional[Any], str]],
):
    """通用查询过滤。

    :param conditions: (列对象, 值, 模式) 列表
        - 模式 'eq'：精确匹配，对应 Admin list_filter
        - 模式 'contains'：模糊匹配，对应 Admin search_fields
        - 模式 'in'：多值 IN 匹配，value 需为非空列表
    :return: 过滤后的 query（值为 None 的条件自动跳过；
        'in' 模式下空列表也会跳过）
    """
    for column, value, mode in conditions:
        if value is None:
            continue
        if mode == "eq":
            query = query.filter(column == value)
        elif mode == "in":
            # 多值匹配：value 为空列表时跳过，避免生成空 IN 语句
            if not value:
                continue
            query = query.filter(column.in_(value))
        else:  # contains 模糊搜索
            query = query.filter(column.contains(value))
    return query


def _run_sync_steps(steps: List[Tuple[str, Callable]]):
    """依次执行同步函数列表，返回每步结果。

    与原 middleware.py 保持一致：groupsymbol / groupacc 会先执行
    value_float_em_sql 再执行对应 upsert。任一步骤异常则抛出 500。
    """
    results = []
    try:
        for name, fn in steps:
            results.append({"step": name, "result": fn()})
        return {"status": "ok", "steps": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# SubTask 9.2: 原 index 视图，对应 GroupAdmin
@router.get("/group", response_model=PageResponse[GroupOut])
def list_groups(
    account: Optional[List[str]] = Query(default=None),
    category: Optional[List[str]] = Query(default=None),
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回非 cash 类别的 Group 列表。

    原 Django index 视图使用 Group.objects.exclude(category='cash')，
    此处对应 SQLAlchemy 的 Group.category != 'cash'。
    account/category 支持多值 IN 匹配，symbol 为 search_fields（模糊）。
    """
    # 核心：过滤掉 cash 类别（与原 index 视图一致）
    query = db.query(Group).filter(Group.category != "cash")
    query = _filter_query(
        query,
        [
            (Group.account, account, "in"),
            (Group.category, category, "in"),
            (Group.symbol, symbol, "contains"),
        ],
    )
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# SubTask 9.3: 对应 BillAdmin
@router.get("/bills", response_model=PageResponse[BillOut])
def list_bills(
    account: Optional[List[str]] = Query(default=None),
    category: Optional[List[str]] = Query(default=None),
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 Bill 列表。

    account/category 支持多值 IN 匹配，
    symbol 为 search_fields（模糊）。
    """
    query = db.query(Bill)
    query = _filter_query(
        query,
        [
            (Bill.account, account, "in"),
            (Bill.category, category, "in"),
            (Bill.symbol, symbol, "contains"),
        ],
    )
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# SubTask 9.4: 对应 ProfitAdmin
@router.get("/profits", response_model=PageResponse[ProfitOut])
def list_profits(
    account: Optional[List[str]] = Query(default=None),
    category: Optional[List[str]] = Query(default=None),
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 Profit 列表，含关联 Bill 的 account/symbol/name 字段。

    Profit 通过 bill_id 外键关联 Bill。原 Admin 的
    search_fields=bill__symbol、list_filter=bill__account
    均作用于关联 Bill；account/category 支持多值 IN 匹配，
    故此处通过 JOIN Bill 实现过滤，并在结果中补充关联字段。

    COUNT 与数据查询共享同一 query 对象（含 JOIN 与过滤条件），
    由于 Profit→Bill 为多对一关系，JOIN 不会产生重复行，count 安全。
    """
    query = db.query(Profit)
    # 仅在存在关联过滤参数时才 JOIN，避免无谓联表
    if any(v is not None for v in (account, category, symbol)):
        query = query.join(Bill, Profit.bill_id == Bill.id)
        query = _filter_query(
            query,
            [
                (Bill.account, account, "in"),
                (Bill.category, category, "in"),
                (Bill.symbol, symbol, "contains"),
            ],
        )
    # COUNT 与数据查询过滤条件一致（JOIN 已包含在内），在 offset/limit 之前计算
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    result = []
    for p in items:
        d = p.to_dict()
        # 补充关联 Bill 的 account/symbol/name/trade_time（对应原 ProfitAdmin 的 bill_* 方法）
        if p.bill:
            d["account"] = p.bill.account
            d["symbol"] = p.bill.symbol
            d["name"] = p.bill.name
            d["trade_time"] = p.bill.trade_time
        result.append(d)
    return {"items": result, "total": total, "limit": limit, "offset": offset}


# SubTask 9.5: 对应 GroupAccAdmin（全量列表，无过滤参数）
@router.get("/group-accs", response_model=PageResponse[GroupAccOut])
def list_group_accs(
    account: Optional[List[str]] = Query(default=None),
    acc_aset_only: Optional[bool] = Query(default=None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 GroupAcc 列表。

    account 支持多值 IN 匹配，acc_aset_only 为 True 时仅返回账户净值不为 0 的记录。
    """
    query = db.query(GroupAcc)
    query = _filter_query(
        query,
        [
            (GroupAcc.account, account, "in"),
        ],
    )
    # 仅保留账户净值不为 0 的记录
    if acc_aset_only:
        query = query.filter(GroupAcc.acc_aset != 0)
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# SubTask 9.6: 对应 GroupSymbolAdmin
@router.get("/group-symbols", response_model=PageResponse[GroupSymbolOut])
def list_group_symbols(
    category: Optional[List[str]] = Query(default=None),
    symbol: Optional[str] = None,
    value_only: Optional[bool] = Query(default=None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 GroupSymbol 列表。

    category 支持多值 IN 匹配（可传入多个值，命中任一即返回），
    symbol 为 search_fields（模糊）。
    value_only 为 True 时仅返回当前市值（value_total）不为 0 的记录。
    """
    query = db.query(GroupSymbol)
    query = _filter_query(
        query,
        [
            (GroupSymbol.category, category, "in"),
            (GroupSymbol.symbol, symbol, "contains"),
        ],
    )
    # 仅保留当前市值不为 0 的记录
    if value_only:
        query = query.filter(GroupSymbol.value_total != 0)
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# 标的分组类别查询：返回 Config.MAP_CATEGORY 字典的所有 key 作为类别列表
@router.get("/group-symbols/categories")
def list_group_symbol_categories():
    """返回应用配置的交易类别列表。

    数据源为 Config.MAP_CATEGORY 字典的 key 集合，确保前端下拉选项
    与后端交易类型映射配置保持一致，无需依赖数据库实际数据。
    """
    # 直接取字典 key 列表，避免查询数据库
    categories = list(Config.MAP_CATEGORY.keys())
    return {"categories": categories}


# 年度收益查询
@router.get("/profit-years", response_model=PageResponse[ProfitYearOut])
def list_profit_years(
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回年度收益列表，按年度降序排列。"""
    query = db.query(ProfitYear).order_by(ProfitYear.year.desc())
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# 合并后的同步路由：顺序执行实时估值 → 标的汇总 → 账户汇总
@router.post("/sync/group")
def sync_group():
    """触发实时估值 + 标的汇总 + 账户汇总同步。

    顺序执行：value_float_em_sql → upsert_group_symbol_sql → upsert_group_acc_sql。
    """
    from server_fast.app.bills.services.value_calc import value_float_em_sql
    from server_fast.app.bills.services.account_summary import upsert_group_symbol_sql, upsert_group_acc_sql

    return _run_sync_steps(
        [
            ("value_float_em_sql", value_float_em_sql),
            ("upsert_group_symbol_sql", upsert_group_symbol_sql),
            ("upsert_group_acc_sql", upsert_group_acc_sql),
        ]
    )


# =========================================================================
# 定时脚本拆分路由（对应 run.py 中 bills 部分8个步骤，顺序执行）
# =========================================================================

@router.post("/run/batch-import")
def run_batch_import():
    """顺序执行账单导入到账户汇总的完整流程（对应 run.py 中 bills 部分）。

    执行顺序：
    1. insert_bill_all_excel_sql   导入账单数据
    2. update_symbol_bill_sql      更新账单中的代码
    3. del_old_symbol_group_sql    删除汇总表中的旧代码
    4. upsert_group_cash_sql       资金汇总
    5. upsert_group_profit_sql     收益汇总
    6. upsert_profit_group_sql     收益试算
    7. cash_update_group_sql       资金试算
    8. upsert_profit_year_sql      年度收益统计
    9. upsert_group_acc_sql        账户汇总
    10. upsert_group_symbol_sql     标的汇总
    """
    from server_fast.app.bills.services.bill_import import insert_bill_all_excel_sql
    from server_fast.app.bills.services.group_summary import (
        update_symbol_bill_sql,
        del_old_symbol_group_sql,
        upsert_group_cash_sql,
        upsert_group_profit_sql,
    )
    from server_fast.app.bills.services.profit_calc import upsert_profit_group_sql
    from server_fast.app.bills.services.cash_calc import cash_update_group_sql
    from server_fast.app.bills.services.profit_year import upsert_profit_year_sql
    from server_fast.app.bills.services.account_summary import (
        upsert_group_acc_sql,
        upsert_group_symbol_sql,
    )

    return _run_sync_steps(
        [
            ("insert_bill_all_excel_sql", insert_bill_all_excel_sql),
            ("update_symbol_bill_sql", update_symbol_bill_sql),
            ("del_old_symbol_group_sql", del_old_symbol_group_sql),
            ("upsert_group_cash_sql", upsert_group_cash_sql),
            ("upsert_group_profit_sql", upsert_group_profit_sql),
            ("upsert_profit_group_sql", upsert_profit_group_sql),
            ("cash_update_group_sql", cash_update_group_sql),
            ("upsert_profit_year_sql", upsert_profit_year_sql),
            ("upsert_group_acc_sql", upsert_group_acc_sql),
            ("upsert_group_symbol_sql", upsert_group_symbol_sql),
        ]
    )
