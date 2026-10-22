# -*- coding: utf-8 -*-
"""bills 应用路由。

提供 Group / Bill / Profit / GroupAcc / GroupSymbol 的查询接口
及 batch-import / sync 同步操作。

约定：
- GET 路由通过 Depends(get_db) 获取会话，直接查询模型；
- POST 同步路由调用 service 内部函数（service 内部自管理 session）；
- search_fields 用 contains（模糊），list_filter 用 ==（精确）。
"""
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server_fast.app.bills.config import Config
from server_fast.app.bills.models import (
    Bill,
    Group,
    GroupAcc,
    GroupSymbol,
    GroupSymbolNoteLog,
    Profit,
    ProfitYear,
)
from server_fast.app.bills.schemas import (
    BillOut,
    BillUpdate,
    GroupAccOut,
    GroupOut,
    GroupSymbolNoteUpdate,
    GroupSymbolOut,
    GroupUpdate,
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

    groupsymbol / groupacc 会先执行 value_float_em_sql 再执行对应 upsert。
    任一步骤异常则抛出 500。
    """
    results = []
    try:
        for name, fn in steps:
            results.append({"step": name, "result": fn()})
        return {"status": "ok", "steps": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Group 列表
@router.get("/group", response_model=PageResponse[GroupOut])
def list_groups(
    account: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    symbol: Optional[str] = None,
    value_only: Optional[bool] = Query(default=None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回非 cash 类别的 Group 列表。

    account/category 单值精确匹配，symbol 为 search_fields（模糊）。
    value_only 为 True 时仅返回当前市值（value_total）不为 0 的记录。
    """
    # 过滤掉 cash 类别
    # query = db.query(Group).filter(Group.category != "cash")
    query = db.query(Group)
    query = _filter_query(
        query,
        [
            (Group.account, account, "=="),
            (Group.category, category, "=="),
            (Group.symbol, symbol, "contains"),
        ],
    )
    # 仅保留当前市值不为 0 的记录
    if value_only:
        query = query.filter(Group.value_total != 0)
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    # 按账户、代码升序排列
    items = query.order_by(Group.account.asc(), Group.symbol.asc()).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# Bill 列表
@router.get("/bills", response_model=PageResponse[BillOut])
def list_bills(
    account: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 Bill 列表。

    account/category 单值精确匹配，
    symbol 为 search_fields（模糊）。
    """
    query = db.query(Bill)
    query = _filter_query(
        query,
        [
            (Bill.account, account, "=="),
            (Bill.category, category, "=="),
            (Bill.symbol, symbol, "contains"),
        ],
    )
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# Profit 列表
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


# GroupAcc 列表
@router.get("/group-accs", response_model=PageResponse[GroupAccOut])
def list_group_accs(
    account: Optional[str] = Query(default=None),
    acc_aset_only: Optional[bool] = Query(default=None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 GroupAcc 列表。

    account 单值精确匹配，acc_aset_only 为 True 时仅返回账户价值不为 0 的记录。
    """
    query = db.query(GroupAcc)
    query = _filter_query(
        query,
        [
            (GroupAcc.account, account, "=="),
        ],
    )
    # 仅保留账户价值不为 0 的记录
    if acc_aset_only:
        query = query.filter(GroupAcc.acc_aset != 0)
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# GroupSymbol 列表
@router.get("/group-symbols", response_model=PageResponse[GroupSymbolOut])
def list_group_symbols(
    category: Optional[str] = Query(default=None),
    symbol: Optional[str] = None,
    value_only: Optional[bool] = Query(default=None),
    pos_state: Optional[str] = Query(default=None, pattern="^(holding|closed)$"),
    has_note: Optional[bool] = Query(default=None),
    theme: Optional[str] = Query(default=None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 GroupSymbol 列表。

    category 单值精确匹配，
    symbol 为 search_fields（模糊）。
    value_only 为 True 时仅返回当前市值（value_total）不为 0 的记录（兼容旧参数）。
    pos_state：holding=持仓中（value_total 非 0）、closed=已平仓（value_total 为 0 或 NULL）。
    has_note 为 True 时只返回写过笔记（5 个手动字段至少一项非空）的记录。
    theme 为主题标签精确匹配（不能模糊：否则「黄金」会把「黄金ETF」也带出来）。
    """
    query = db.query(GroupSymbol)
    query = _filter_query(
        query,
        [
            (GroupSymbol.category, category, "=="),
            (GroupSymbol.symbol, symbol, "contains"),
            (GroupSymbol.theme, theme, "eq"),
        ],
    )
    # 仅保留当前市值不为 0 的记录
    if value_only:
        query = query.filter(GroupSymbol.value_total != 0)
    if pos_state == "holding":
        query = query.filter(GroupSymbol.value_total != 0)
    elif pos_state == "closed":
        query = query.filter(or_(
            GroupSymbol.value_total == 0, GroupSymbol.value_total.is_(None)))
    if has_note:
        query = query.filter(or_(*[
            and_(getattr(GroupSymbol, field).isnot(None), getattr(GroupSymbol, field) != '')
            for field in _GS_NOTE_FIELDS
        ]))
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


# 标的笔记可编辑字段：与 GroupSymbolNoteUpdate 保持一致，其余派生字段一律不允许手动改
_GS_NOTE_FIELDS = ('logic', 'plan', 'invalidate', 'review', 'theme')


@router.get("/group-symbols/themes")
def list_group_symbol_themes(db: Session = Depends(get_db)):
    """返回已使用过的主题标签（去重，按行数降序），供筛选下拉。

    必须独立成接口：若从列表页数据里提取，一旦按某主题筛选，下拉选项就会塌缩成
    只剩当前主题，用户没法再切到别的标签。

    ⚠️ 路由必须声明在 `/group-symbols/{id_}` 之类带路径参数的路由之前，
    否则 "themes" 会被当成 id 解析。
    """
    rows = (
        db.query(GroupSymbol.theme, func.count(GroupSymbol.id).label('cnt'))
        .filter(GroupSymbol.theme.isnot(None), GroupSymbol.theme != '')
        .group_by(GroupSymbol.theme)
        .order_by(func.count(GroupSymbol.id).desc(), GroupSymbol.theme.asc())
        .all()
    )
    return {"items": [{"theme": theme, "count": cnt} for theme, cnt in rows]}


@router.patch("/group-symbols/{id_}")
def update_group_symbol_note(
    id_: int,
    payload: GroupSymbolNoteUpdate,
    db: Session = Depends(get_db),
):
    """修改标的笔记（PATCH /bills/group-symbols/{id}）。

    仅允许修改 logic / plan / invalidate / review / theme 五个手动字段。
    语义按 exclude_unset 区分：未传 = 不修改；传空串 = 清空该字段。
    每次真正发生变化的字段写一条 bills_group_symbol_note_log，与主表同一事务提交。
    note_at 只在首次落笔（原为 NULL 且本次写了非空内容）时由后端写，前端不可传。
    """
    gs = db.get(GroupSymbol, id_)
    if gs is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")

    data = payload.model_dump(exclude_unset=True)
    # 空串/纯空白统一归一为 None，避免库里出现 '' 与 NULL 两种「空」
    changes = []
    for field in _GS_NOTE_FIELDS:
        if field not in data:
            continue
        new_val = data[field]
        if isinstance(new_val, str):
            new_val = new_val.strip() or None
        if field == 'theme' and new_val is not None and len(new_val) > 32:
            raise HTTPException(status_code=400, detail=f"theme 超过 32 字符：{new_val}")
        old_val = getattr(gs, field)
        if (old_val or None) == (new_val or None):
            continue
        changes.append((field, old_val, new_val))

    if not changes:
        return {"status": "success", "message": "内容未变化", "fields": [], "note_at": gs.note_at}

    try:
        for field, old_val, new_val in changes:
            setattr(gs, field, new_val)
            db.add(GroupSymbolNoteLog(
                category=gs.category,
                symbol=gs.symbol,
                field=field,
                old_value=old_val,
                new_value=new_val,
                source='manual',
            ))
        if gs.note_at is None:
            gs.note_at = datetime.now()
        db.commit()
        return {
            "status": "success",
            "message": f"已更新 {len(changes)} 个字段",
            "fields": [field for field, _, _ in changes],
            "note_at": gs.note_at,
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"修改失败：{str(e)}")


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


# =========================================================================
# PATCH 修改路由（2 个，按 id 更新）
# =========================================================================


@router.patch("/bills/{id_}")
def update_bill(
    id_: int,
    payload: BillUpdate,
    db: Session = Depends(get_db),
):
    """修改 Bill 记录（PATCH /bills/bills/{id}）。

    只更新 payload 中非 None 的字段。None 表示不修改该字段。
    Bill 含唯一约束 (account, trade_time, symbol, exec_type, amount_act, id_agree, cash)，
    修改唯一键字段可能触发 IntegrityError，捕获后返回 400。
    """
    bill = db.get(Bill, id_)
    if bill is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        data = payload.model_dump(exclude_none=True)
        for field, value in data.items():
            setattr(bill, field, value)
        db.commit()
        return {"status": "success", "message": "修改成功"}
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"唯一约束冲突：{str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"修改失败：{str(e)}")


@router.patch("/group/{id_}")
def update_group(
    id_: int,
    payload: GroupUpdate,
    db: Session = Depends(get_db),
):
    """修改 Group 记录（PATCH /bills/group/{id}）。

    仅允许修改 profit_time 和 value_time 两个时间戳字段。
    其余字段由后端批量导入/同步流程自动生成，不允许前端手动编辑。
    """
    group = db.get(Group, id_)
    if group is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        data = payload.model_dump(exclude_none=True)
        for field, value in data.items():
            setattr(group, field, value)
        db.commit()
        return {"status": "success", "message": "修改成功"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"修改失败：{str(e)}")


# =========================================================================
# 同步路由
# =========================================================================

# 合并后的同步路由：顺序执行实时估值 → 标的汇总 → 账户汇总
@router.post("/sync/group")
def sync_group():
    """触发实时估值 + 标的汇总 + 账户汇总同步。

    顺序执行：value_float_em_sql → upsert_group_symbol_sql → upsert_group_acc_sql。
    """
    from server_fast.app.bills.services.value_calc import value_float_em_sql
    from server_fast.app.bills.services.group_summary import upsert_group_symbol_sql, upsert_group_acc_sql

    return _run_sync_steps(
        [
            ("value_float_em_sql", value_float_em_sql),
            ("upsert_group_acc_sql", upsert_group_acc_sql),
            ("upsert_group_symbol_sql", upsert_group_symbol_sql),
        ]
    )
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
    9. value_float_em_sql          市值更新
    10. upsert_group_acc_sql        账户汇总
    11. upsert_group_symbol_sql     标的汇总
    """
    from server_fast.app.bills.services.bill_import import insert_bill_all_excel_sql
    from server_fast.app.bills.services.group_summary import (
        update_symbol_bill_sql,
        del_old_symbol_group_sql,
        upsert_group_cash_sql,
        upsert_group_profit_sql,
        upsert_group_acc_sql,
        upsert_group_symbol_sql,
    )
    from server_fast.app.bills.services.profit_calc import upsert_profit_group_sql
    from server_fast.app.bills.services.cash_calc import cash_update_group_sql
    from server_fast.app.bills.services.profit_year import upsert_profit_year_sql
    from server_fast.app.bills.services.value_calc import value_float_em_sql

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
            ("value_float_em_sql", value_float_em_sql),
            ("upsert_group_acc_sql", upsert_group_acc_sql),
            ("upsert_group_symbol_sql", upsert_group_symbol_sql),
        ]
    )
