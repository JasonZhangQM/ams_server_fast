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
from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from server_fast.app.bills.config import Config
from server_fast.app.bills.constants import (
    RZRQ_CATEGORY,
    TRADE_CATS,
    VIRTUAL_SYMBOLS,
    is_attachable,
)
from server_fast.app.bills.models import (
    Bill,
    BillNote,
    Group,
    GroupAcc,
    GroupSymbol,
    Profit,
    ProfitYear,
)
from server_fast.app.bills.schemas import (
    AttributionByPlanOut,
    BillNoteOut,
    BillNoteSave,
    BillOut,
    BillUpdate,
    GroupAccOut,
    GroupOut,
    GroupSymbolCodeOut,
    GroupSymbolOut,
    GroupSymbolVmUpdate,
    GroupUpdate,
    ProfitOut,
    ProfitYearOut,
    RiskBoardOut,
)
from server_fast.app.irs.models import ValueMonitor
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
    keyword: Optional[str] = None,
    value_filter: Optional[str] = Query(default=None, pattern="^(gt|lt|eq|ne)$",
                                        description="市值筛选：gt=>0；lt=<0；eq==0；ne!=0"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回非 cash 类别的 Group 列表。

    account/category 单值精确匹配，symbol 为 search_fields（模糊）。
    keyword 为合并搜索：代码模糊命中，或名称模糊命中对应代码（OR）。
    value_filter 按当前市值（value_total）与 0 比较筛选。
    """
    # 过滤掉 cash 类别
    # query = db.query(Group).filter(Group.category != "cash")
    query = db.query(Group)
    query = _filter_query(
        query,
        [
            (Group.account, account, "eq"),
            (Group.category, category, "eq"),
            (Group.symbol, symbol, "contains"),
        ],
    )
    # 合并搜索：Group 表没有名称列，名称走 bills_bill 的 (symbol, name) 反查代码集合，
    # 再与代码模糊匹配取并集（OR）
    if keyword:
        name_symbols = db.query(Bill.symbol).filter(Bill.name.contains(keyword))
        query = query.filter(or_(Group.symbol.contains(keyword), Group.symbol.in_(name_symbols)))
    # 市值筛选：value_total 与 0 比较（NULL 不满足任何比较，与原 value_only!=0 语义一致）
    if value_filter == "gt":
        query = query.filter(Group.value_total > 0)
    elif value_filter == "lt":
        query = query.filter(Group.value_total < 0)
    elif value_filter == "eq":
        query = query.filter(Group.value_total == 0)
    elif value_filter == "ne":
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
    name: Optional[str] = None,
    keyword: Optional[str] = None,
    vm_symbol: Optional[str] = None,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 Bill 列表。

    account/category 单值精确匹配，symbol 模糊，name 模糊（标的中文名 / 合约简称）。
    keyword 为合并搜索：代码或名称任一模糊命中即可（OR），供前端单输入框使用。
    vm_symbol 按归属标的精确筛选（经 bills_group_symbol 的 (category, symbol) 关联）。

    v3 起每行额外带：`vm_symbol`（经 bills_group_symbol 归属，融资融券恒为 None）
    与两条笔记摘要（想法 / 复盘）。笔记只做一次 IN 查询补齐，不让账单表承担 JOIN。

    排序：固定 `id DESC`。id 为自增主键、等于入库顺序，倒序即最新一笔在前。
    必须显式排序——MySQL 对无 ORDER BY 的查询不保证返回顺序，
    服务端分页（offset/limit）下会出现翻页跳行或重复行。
    """
    query = db.query(Bill)
    query = _filter_query(
        query,
        [
            (Bill.account, account, "eq"),
            (Bill.category, category, "eq"),
            (Bill.symbol, symbol, "contains"),
            (Bill.name, name, "contains"),
        ],
    )
    # 合并搜索：一个输入框同时匹配代码与名称，命中任一即可（AND 在这里是错的）
    if keyword:
        query = query.filter(or_(Bill.symbol.contains(keyword), Bill.name.contains(keyword)))
    # 归属标的筛选：vm_symbol 不是 bills_bill 的列，来自 bills_group_symbol
    # （两表以 (category, symbol) 关联，同一组在 group_symbol 里唯一）。
    # 用 EXISTS 而不是 JOIN：JOIN 在关联逻辑万一不唯一时会放大行数，让 count 与分页全错。
    if vm_symbol:
        query = query.filter(
            db.query(GroupSymbol.id)
            .filter(
                GroupSymbol.category == Bill.category,
                GroupSymbol.symbol == Bill.symbol,
                GroupSymbol.vm_symbol == vm_symbol,
            )
            .exists()
        )
    # 过滤后总数（在 offset/limit 之前计算；count 不吃 order_by，先算再排）
    total = query.count()
    # 按交易日倒序；trade_time 同一天大量重复（日内只到日或分钟精度），
    # 必须用 id 兜底才确定性，否则服务端分页在同值边界会跳行/重行。
    items = query.order_by(Bill.trade_time.desc(), Bill.id.desc()).offset(offset).limit(limit).all()
    dicts = [item.to_dict() for item in items]

    if dicts:
        bill_ids = [d["id"] for d in dicts]
        # 归属：一次 JOIN group_symbol（(category,symbol) 是两表共有键）
        vm_map = {}
        keys = {(d["category"], d["symbol"]) for d in dicts}
        gs_rows = (db.query(GroupSymbol.category, GroupSymbol.symbol, GroupSymbol.vm_symbol)
                   .filter(GroupSymbol.vm_symbol.isnot(None)).all())
        for cat, sym, vm in gs_rows:
            if (cat, sym) in keys:
                vm_map[(cat, sym)] = vm
        # 笔记：一次取完，Python 侧分流
        notes = {}
        for n in (db.query(BillNote).filter(BillNote.bill_id.in_(bill_ids)).all()):
            notes.setdefault(n.bill_id, {})[n.kind] = n
        for d in dicts:
            d["vm_symbol"] = vm_map.get((d["category"], d["symbol"]))
            pair = notes.get(d["id"], {})
            idea = pair.get(BillNote.KIND_IDEA)
            review = pair.get(BillNote.KIND_REVIEW)
            d["note_idea"] = idea.content if idea else None
            d["note_idea_at"] = idea.content_at if idea else None
            d["note_review"] = review.content if review else None
            d["note_review_at"] = review.content_at if review else None
    return {"items": dicts, "total": total, "limit": limit, "offset": offset}


# Profit 列表
@router.get("/profits", response_model=PageResponse[ProfitOut])
def list_profits(
    account: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    keyword: Optional[str] = None,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 Profit 列表，含关联 Bill 的 account/symbol/name 字段。

    Profit 通过 bill_id 外键关联 Bill。原 Admin 的
    search_fields=bill__symbol、list_filter=bill__account
    均作用于关联 Bill；account/category 为单值精确匹配，
    keyword 为代码/名称合并模糊搜索（OR），
    故此处通过 JOIN Bill 实现过滤，并在结果中补充关联字段。

    COUNT 与数据查询共享同一 query 对象（含 JOIN 与过滤条件），
    由于 Profit→Bill 为多对一关系，JOIN 不会产生重复行，count 安全。
    """
    query = db.query(Profit)
    # 仅在存在关联过滤参数时才 JOIN，避免无谓联表
    if account or category or keyword:
        query = query.join(Bill, Profit.bill_id == Bill.id)
        query = _filter_query(
            query,
            [
                (Bill.account, account, "eq"),
                (Bill.category, category, "eq"),
            ],
        )
        # 代码/名称合并模糊匹配：任一字段包含关键词即可
        if keyword:
            query = query.filter(or_(Bill.symbol.contains(keyword), Bill.name.contains(keyword)))
    # COUNT 与数据查询过滤条件一致（JOIN 已包含在内），在 offset/limit 之前计算。
    # 用 count(Profit.id) 直查而非 query.count()：后者会包一层 SELECT 全列的子查询。
    # ⚠️ 必须带列名（count(Profit.id)），写 count(*)/func.count() 会因 with_entities
    #    丢掉隐式 FROM，实际执行 "SELECT count(*)"（无 FROM）恒返回 1。
    total = query.with_entities(func.count(Profit.id)).scalar() or 0
    # 必须显式排序：MySQL 对无 ORDER BY 的查询不保证顺序，服务端分页会跳行/重行。
    # bill_id 在 bills_profit 上是 unique（一对一），单列倒序即确定性排序，无需加 id 兜底。
    # joinedload：一次 JOIN 取回关联 Bill，替代逐行懒加载（原实现每行 1 条 SELECT）。
    items = (
        query.options(joinedload(Profit.bill))
        .order_by(Profit.bill_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
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
    acc_aset_filter: Optional[str] = Query(default=None, pattern="^(gt|lt|eq|ne)$",
                                           description="账户价值筛选：gt=>0；lt=<0；eq==0；ne!=0"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 GroupAcc 列表。

    account 单值精确匹配；acc_aset_filter 按账户价值（acc_aset）与 0 比较筛选。
    """
    query = db.query(GroupAcc)
    query = _filter_query(
        query,
        [
            (GroupAcc.account, account, "eq"),
        ],
    )
    # 账户价值筛选（NULL 不满足任何比较）
    if acc_aset_filter == "gt":
        query = query.filter(GroupAcc.acc_aset > 0)
    elif acc_aset_filter == "lt":
        query = query.filter(GroupAcc.acc_aset < 0)
    elif acc_aset_filter == "eq":
        query = query.filter(GroupAcc.acc_aset == 0)
    elif acc_aset_filter == "ne":
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
    mapped: Optional[str] = Query(default=None, pattern="^(yes|no)$",
                                  description="归属筛选：yes=已归属；no=未归属（不含融资融券）"),
    vm_symbol: Optional[str] = Query(default=None,
                                     description="按归属标的精确筛选（如 SHSE.000300）"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """返回 GroupSymbol 列表。

    category 单值精确匹配，symbol 为 search_fields（模糊）。
    value_only 为 True 时仅返回当前市值（value_total）不为 0 的记录（兼容旧参数）。
    pos_state：holding=持仓中（value_total 非 0）、closed=已平仓（value_total 为 0 或 NULL）。
    mapped：yes=已归属标的；no=未归属（**排除融资融券**——它本来就不归属，不是漏配）。
    vm_symbol：按具体归属标的精确筛选（下拉值来自 irs_value_monitor.symbol）。
    与 mapped 是 AND 关系：mapped=no 且 vm_symbol 有值时结果恒为空，前端选了具体标的后会清掉 mapped。

    v3 起本表没有笔记列也没有 theme：主题只在 `/irs/value-monitors` 维护，
    唯一的人工列是 `vm_symbol`。
    """
    query = db.query(GroupSymbol)
    query = _filter_query(
        query,
        [
            (GroupSymbol.category, category, "eq"),
            (GroupSymbol.symbol, symbol, "contains"),
            # 归属标的是受控代码值，必须精确；_filter_query 只认 'eq'，
            # 写成 '==' 会落进 else 分支做 contains，把 SHSE.000300 串成前缀匹配
            (GroupSymbol.vm_symbol, vm_symbol, "eq"),
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
    if mapped == "yes":
        query = query.filter(GroupSymbol.category.in_(TRADE_CATS),
                             GroupSymbol.symbol.notin_(sorted(VIRTUAL_SYMBOLS)),
                             or_(
                                 GroupSymbol.vm_symbol.isnot(None),
                                 GroupSymbol.symbol.in_(db.query(ValueMonitor.symbol))))
    elif mapped == "no":
        # 与 unmapped-count 同口径（constants.is_attachable）：排除资金流水行与同代码自动命中
        query = query.filter(GroupSymbol.category.in_(TRADE_CATS),
                             GroupSymbol.symbol.notin_(sorted(VIRTUAL_SYMBOLS)),
                             GroupSymbol.vm_symbol.is_(None),
                             ~GroupSymbol.symbol.in_(db.query(ValueMonitor.symbol)))
    # 过滤后总数（在 offset/limit 之前计算）
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/group-symbols/unmapped-count")
def count_unmapped_group_symbols(db: Session = Depends(get_db)):
    """归属计数：已归属 / 未归属 / 不参与归属（顶部提示用）。

    三态口径，加起来等于 total：
    - `mapped`   已归属：有 vm_symbol，或 `symbol` 与某标的同代码（自动命中，无需填）；
    - `unmapped` 真未归属：可归属的交易行里既无 vm_symbol、也不同代码 —— 这才是可操作清单；
    - `excluded` 不参与归属：资金流水行（`CRJ`/`SYFY`/`cash`/`RZRQ` + 理财逆回购），
                 给了 vm_symbol 也没意义，故不算漏配。

    口径唯一来源见 `app/bills/constants.py::is_attachable`。

    ⚠️ 必须声明在 `/group-symbols/{id_}` 之前，否则 "unmapped-count" 会被当成 id。
    """
    vm_symbols = {r[0] for r in db.query(ValueMonitor.symbol).all()}
    rows = db.query(GroupSymbol.category, GroupSymbol.symbol, GroupSymbol.vm_symbol).all()
    mapped_n = unmapped_n = excluded_n = 0
    for cat, sym, vm in rows:
        if not is_attachable(cat, sym):
            excluded_n += 1
        elif vm or sym in vm_symbols:
            mapped_n += 1
        else:
            unmapped_n += 1
    return {"mapped": mapped_n, "unmapped": unmapped_n, "excluded": excluded_n,
            "total": len(rows)}


@router.get("/group-symbols/symbol-options", response_model=List[GroupSymbolCodeOut])
def list_group_symbol_options(
    category: Optional[str] = Query(
        default="期权交易",
        description="交易分类精确匹配；传 * 表示不限（仍只返回三类真交易）"),
    db: Session = Depends(get_db),
):
    """券商码候选：给「期权行情同步清单」的券商码内联下拉当选项源。

    **跨模块调用的唯一正道**：bds 前端直接调本接口，bds 后端一行 import 都不加
    ——`bds_option_config.broker_symbol` 要选什么值属于账单事实，权威在 bills。
    写入仍走 `PUT /bds/option-config/{id}`，本接口只读。

    口径（与 `constants.is_attachable` 一致，不在这里另写一份白名单）：

    - 只取三类真交易，且排除资金流水占位码（`RZRQ`/`CRJ`/`SYFY`/`cash`）；
      理财行（`OTC`/`TTB`）靠 `category` 不在 TRADE_CATS 自动排除；
    - 按 `symbol` **去重**：本表唯一键是 `(category, symbol)`，同一个码在多个分类下
      会占多行，不去重会让下拉出现重复项且值相同；
    - 空串/None 的 symbol 一律剔除（表里实测存在一个空串行）；
    - 持仓中（`value_total` 非 0）的排前面 —— 81 个候选里真正在用只有几张，
      不排序的话每次都要滚半天。

    ⚠️ **必须声明在 `/group-symbols/{id_}` 之前**，否则 "symbol-options" 会被当成
    id 去解析（本文件 `unmapped-count` 已有同样的位置约束）。
    """
    if category and category != "*" and category not in TRADE_CATS:
        raise HTTPException(
            status_code=400,
            detail=f"category 只能是 {list(TRADE_CATS)} 之一，或传 * 表示不限：{category}")

    value_sum = func.sum(GroupSymbol.value_total)
    query = db.query(
        GroupSymbol.symbol.label("symbol"),
        func.max(GroupSymbol.category).label("category"),
        func.max(GroupSymbol.p_total).label("p_total"),
        value_sum.label("value_total"),
    ).filter(
        GroupSymbol.symbol.isnot(None),
        GroupSymbol.symbol != "",
        GroupSymbol.category.in_(TRADE_CATS),
        GroupSymbol.symbol.notin_(sorted(VIRTUAL_SYMBOLS)),
    )
    if category and category != "*":
        query = query.filter(GroupSymbol.category == category)

    rows = (query.group_by(GroupSymbol.symbol)
            # 持仓中优先：市值同号求和后绝对值大的在前；NULL（无市值）自然排最后
            .order_by(func.abs(value_sum).desc(), GroupSymbol.symbol.asc())
            .all())

    return [
        GroupSymbolCodeOut(symbol=sym, category=cat, p_total=p, value_total=v)
        for sym, cat, p, v in rows
    ]


@router.patch("/group-symbols/{id_}/vm")
def update_group_symbol_vm(
    id_: int,
    payload: GroupSymbolVmUpdate,
    db: Session = Depends(get_db),
):
    """修改合约行的归属标的（PATCH /bills/group-symbols/{id}/vm）。

    铁律 4.9：**只写 vm_symbol 一列**（`update_columns=['vm_symbol']` 的白名单语义），
    禁止整行回写——否则派生列会被上一屏的旧值覆盖。

    不可归属行（`constants.is_attachable` 为假：融资融券 / 出入金 CRJ / 收益费用 SYFY /
    cash / 理财逆回购）传任何非空值 -> 400：这些行的 symbol 只是资金流水占位代码，
    给它们挂标的不产生任何归属意义。传 null 仍允许（无事发生，便于批量清理）。
    """
    gs = db.get(GroupSymbol, id_)
    if gs is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    if payload.vm_symbol is not None and not is_attachable(gs.category, gs.symbol):
        raise HTTPException(
            status_code=400,
            detail=f"{gs.category} / {gs.symbol} 是资金流水行，不参与标的归属")
    if payload.vm_symbol:
        exists = db.query(ValueMonitor.symbol).filter(ValueMonitor.symbol == payload.vm_symbol).first()
        if exists is None:
            raise HTTPException(status_code=400,
                                detail=f"标的 {payload.vm_symbol} 不在估值监测里，请先新增标的")
    try:
        gs.vm_symbol = payload.vm_symbol or None
        db.commit()
        return {"status": "success", "vm_symbol": gs.vm_symbol}
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
# 单行写路由（PATCH 改 / DELETE 删，按 id 操作）
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


@router.delete("/bills/{id_}")
def delete_bill(
    id_: int,
    db: Session = Depends(get_db),
):
    """删除一条账单流水，并级联删除对应的账单收益行（DELETE /bills/bills/{id}）。

    硬约束：只允许删除该账户 trade_time 最大的那一笔。
    原因：导入是逐账户按 trade_time > MAX(trade_time) 增量过滤的，
    删中间或前面的行不会被下次导入补回，会留下永久缺口；
    只有从末尾倒着删，再导入才能把删掉的行补回来。

    副作用：
    - bills_profit 中 bill_id 对应的行一并删除；
    - 受影响的 bills_group 行 profit_time 置 NULL，标记待重算
      （end_time 不会因为删行自动变小，不清就不会触发重算）。
    """
    bill = db.get(Bill, id_)
    if bill is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")

    acc_max = db.query(func.max(Bill.trade_time)).filter(Bill.account == bill.account).scalar()
    if acc_max is not None and bill.trade_time < acc_max:
        raise HTTPException(
            status_code=400,
            detail=(
                f"只能删除账户 {bill.account} 的最后一笔。"
                f"该账户最新交易时间为 {acc_max:%Y-%m-%d %H:%M:%S}，"
                f"本行为 {bill.trade_time:%Y-%m-%d %H:%M:%S}。"
                "请先删除该账户更晚的流水，或让账单重新导入补齐。"
            ),
        )

    account, category, symbol = bill.account, bill.category, bill.symbol
    try:
        # 先删子表再删主表，不依赖数据库层 ON DELETE CASCADE 是否真的建成
        deleted_profit = (
            db.query(Profit).filter(Profit.bill_id == id_).delete(synchronize_session=False)
        )
        db.delete(bill)
        # 派生汇总标记过期：不显式清，end_time==profit_time 会让重算条件不成立
        flagged_groups = (
            db.query(Group)
            .filter(
                Group.account == account,
                Group.category == category,
                Group.symbol == symbol,
                Group.profit_time.isnot(None),
            )
            .update({Group.profit_time: None}, synchronize_session=False)
        )
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")

    return {
        "status": "success",
        "message": "删除成功",
        "deleted_profit": int(deleted_profit or 0),
        "flagged_groups": int(flagged_groups or 0),
    }


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


# =========================================================================
# v3：成交笔记 与 无计划成交
# =========================================================================

@router.get("/unplanned")
def list_unplanned_bills(
    account: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$",
                                 description="只统计某月（YYYY-MM）；空=全部"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """无计划成交：成交时间早于**该标的任何计划**的建立时间。

    这是"先斩后奏 / 事后补计划"的唯一可算证据——研究报告日期、计划日期、成交日期
    三者的先后，就是纪律的度量，文本永远算不出来。

    口径：只看有归属标的的成交（融资融券不参与，它本来就没有"计划委托"这一层）。
    """
    sql = """
        SELECT b.id, b.trade_time, b.account, b.category, b.symbol, b.name,
               b.b_s, b.o_c, b.price, b.vol, g.vm_symbol, p.first_plan_at
        FROM bills_bill b
        JOIN bills_group_symbol g
          ON g.category = b.category AND g.symbol = b.symbol
        LEFT JOIN (SELECT vm_symbol, MIN(create_time) AS first_plan_at
                     FROM irs_vm_plan GROUP BY vm_symbol) p
          ON p.vm_symbol = g.vm_symbol
        WHERE g.vm_symbol IS NOT NULL
          AND b.category <> '融资融券'
          AND (p.first_plan_at IS NULL OR b.trade_time < DATE(p.first_plan_at))
    """
    params: dict = {}
    if account:
        sql += " AND b.account = :account"
        params["account"] = account
    if month:
        sql += " AND DATE_FORMAT(b.trade_time, '%%Y-%%m') = :month"
        params["month"] = month
    sql += " ORDER BY b.trade_time DESC LIMIT :lim"
    params["lim"] = limit
    rows = db.execute(text(sql), params).mappings().all()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/{id_}/note", response_model=List[BillNoteOut])
def list_bill_notes(id_: int, db: Session = Depends(get_db)):
    """某笔成交的全部笔记（想法/复盘，最多两条）。"""
    if db.get(Bill, id_) is None:
        raise HTTPException(status_code=404, detail=f"成交不存在：{id_}")
    return db.query(BillNote).filter(BillNote.bill_id == id_).order_by(BillNote.kind).all()


@router.put("/{id_}/note/{kind}")
def save_bill_note(
    id_: int,
    kind: str,
    payload: BillNoteSave,
    db: Session = Depends(get_db),
):
    """写一笔成交的笔记（kind = 想法 / 复盘）。

    - 独立端点，不混进 PATCH /bills/{id_} —— 那是 36 字段的宽口子，
      一旦有人给它加"回写全列"，笔记会被静默冲成 NULL；
    - content_at 由后端在首次写入时打，前端不可传；
    - 传空串 = 删除该条（不留空壳）。
    """
    bill = db.get(Bill, id_)
    if bill is None:
        raise HTTPException(status_code=404, detail=f"成交不存在：{id_}")
    # 延迟导入：services/__init__ 会拉起多个子模块，顶层导入易与 models 形成环
    from server_fast.app.bills.services.bill_note import upsert_note
    try:
        result = upsert_note(db, bill, kind, payload.content, plan_id=payload.plan_id)
        db.commit()
        return {"status": "success", **result}
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{str(e)}")


@router.get("/attribution/by-plan", response_model=AttributionByPlanOut)
def attribution_by_plan(db: Session = Depends(get_db)):
    """剧本盈亏归因（P3-3）。

    读 bills_bill_note.plan_id 按剧本聚合；每笔成交只取一条带 plan_id 的决策笔记
    （MIN 去重，避免「想法 + 复盘」两条都带 plan_id 时重复计盈亏）。
    pl = pl_long + pl_short（平仓盈亏 l/s，正=盈利）；未归因成交单列残差，不静默丢弃。
    剧本名/标的不在此处返回，由前端按 plan_id 与 irs 计划列表映射（bills 不依赖 irs）。
    """
    assigned_sql = text("""
        SELECT a.plan_id,
               COUNT(*) AS exec_count,
               COALESCE(SUM(p.pl_long), 0) + COALESCE(SUM(p.pl_short), 0) AS total_pl,
               SUM(CASE WHEN COALESCE(p.pl_long, 0) + COALESCE(p.pl_short, 0) > 0
                        THEN 1 ELSE 0 END) AS win_count,
               MAX(b.trade_time) AS last_trade_time
        FROM (
            SELECT bill_id, MIN(plan_id) AS plan_id
            FROM bills_bill_note
            WHERE plan_id IS NOT NULL
            GROUP BY bill_id
        ) a
        JOIN bills_bill b ON b.id = a.bill_id
        JOIN bills_profit p ON p.bill_id = a.bill_id
        GROUP BY a.plan_id
        ORDER BY total_pl DESC
    """)
    items = []
    for r in db.execute(assigned_sql).mappings().all():
        total = float(r["total_pl"] or 0)
        n = int(r["exec_count"])
        w = int(r["win_count"] or 0)
        items.append({
            "plan_id": int(r["plan_id"]),
            "exec_count": n,
            "total_pl": total,
            "win_count": w,
            "win_rate": round(w / n, 4) if n else 0.0,
            "avg_pl": round(total / n, 2) if n else 0.0,
            "last_trade_time": r["last_trade_time"],
        })
    unassigned_sql = text("""
        SELECT COUNT(*) AS exec_count,
               COALESCE(SUM(p.pl_long), 0) + COALESCE(SUM(p.pl_short), 0) AS total_pl
        FROM bills_bill b
        JOIN bills_profit p ON p.bill_id = b.id
        WHERE NOT EXISTS (
            SELECT 1 FROM bills_bill_note n
            WHERE n.bill_id = b.id AND n.plan_id IS NOT NULL
        )
    """)
    u = db.execute(unassigned_sql).mappings().first()
    return {
        "items": items,
        "unattributed": {
            "exec_count": int(u["exec_count"] or 0),
            "total_pl": float(u["total_pl"] or 0),
        },
    }


# =========================================================================
# 风险看板（L3）：实时只读 + 日终落库
# =========================================================================
@router.get("/risk-board", response_model=RiskBoardOut)
def get_risk_board(
    horizon: str = Query("now", pattern="^(now|expiry)$",
                         description="时点口径：now=即期 / expiry=到期"),
    iv_mult: float = Query(1.0, ge=0.5, le=3.0,
                           description="IV 冲击倍数"),
    detail: bool = Query(False, description="是否返回逐仓情景明细（保证金结构图用）"),
):
    """风险看板实时计算（只读）。

    不调 `expire_configs()`：GET 不得产生写操作，过期合约由引擎在计算时兜底剔除。
    行情过期/混日/缺字典等 ValueError 统一转 409，前端据此展示修复指引。
    """
    from server_fast.app.bills.services import risk_scenario
    try:
        return risk_scenario.compute(horizon=horizon, iv_mult=iv_mult,
                                     include_detail=detail)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"风险看板计算失败：{str(e)}")


@router.post("/run/risk-board")
def run_risk_board():
    """日终重算：停用过期合约 → 计算 → UPSERT bills_scenario_grid。

    与 `/sync/group` 一样同步执行（bills 模块暂无任务表），前端按钮需防重复点击。
    """
    from server_fast.app.bills.services import risk_scenario
    try:
        out = risk_scenario.run_and_persist()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"风险看板重算失败：{str(e)}")
    b = out["board"]
    return {
        "status": "ok",
        "engine_ver": b["engine_ver"],
        "calc_date": b["calc_date"],
        "rows_upserted": out["rows_upserted"],
        "disabled_contracts": out["disabled_contracts"],
        "net_total": b["net_total"],
        "margin_total": b["margin_total"],
        "real_total": b["real_total"],
        "first_call": b["first_call"],
        "warnings": b["warnings"],
    }
