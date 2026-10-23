# -*- coding: utf-8 -*-
"""irs 应用路由。

提供查询路由与同步路由：
- GET  /irs/value-monitors              估值监测
- GET  /irs/discounts-monitor           贴水监测
- GET  /irs/option-monitors             期权监测
- POST /irs/sync/{target}               按 target 触发对应 service 函数链（4 种 target）
"""
from datetime import date
import re
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server_fast.app.irs import service
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    DiscountMonitor,
    OptionMonitor,
    ValueMonitor,
    VmInvalidate,
    VmLogic,
    VmPlan,
    VmResearch,
    VmVersion,
)
from server_fast.app.irs.schemas import (
    DiscountMonitorOut,
    OptionMonitorOut,
    ValueMonitorCreate,
    ValueMonitorOut,
    ValueMonitorUpdate,
    VmInvalidateOut,
    VmInvalidateSave,
    VmLogicOut,
    VmLogicSave,
    VmPlanOut,
    VmPlanSave,
    VmResearchOut,
    VmResearchSave,
    VmVersionOut,
    VmVersionSave,
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

def _enrich_value_monitors(db: Session, items: list) -> list:
    """给估值监测列表补 v3 派生字段（8 行数据，逐项一次 IN 查询，成本为零）。

    必须在 response_model 里声明这些字段，否则会被 Pydantic 裁掉（铁律 4.10）。
    """
    if not items:
        return items
    symbols = [it["symbol"] for it in items]
    today = date.today()

    # 版本聚合：当前生效版日期 / 版本数 / 最早的未来版
    ver_rows = (db.query(VmVersion.symbol,
                         func.count(VmVersion.id).label("n"),
                         func.max(VmVersion.effective_date).label("max_d"))
                .filter(VmVersion.symbol.in_(symbols), VmVersion.effective_date <= today)
                .group_by(VmVersion.symbol).all())
    cur_date = {r.symbol: r.max_d for r in ver_rows}
    ver_count = {}
    for sym, n, _ in (db.query(VmVersion.symbol, func.count(VmVersion.id), func.max(VmVersion.effective_date))
                      .filter(VmVersion.symbol.in_(symbols)).group_by(VmVersion.symbol).all()):
        ver_count[sym] = n
    pend = {}
    for sym, d in (db.query(VmVersion.symbol, func.min(VmVersion.effective_date))
                   .filter(VmVersion.symbol.in_(symbols), VmVersion.effective_date > today)
                   .group_by(VmVersion.symbol).all()):
        pend[sym] = d

    # 最新研究（<=today）
    research = service.vm_research.latest_map(db, symbols)

    # 计划计数（状态在 Python 侧派生：8 个标的、计划行数很少）
    plan_pending: Dict[str, int] = {}
    plan_done: Dict[str, int] = {}
    for p in db.query(VmPlan).filter(VmPlan.vm_symbol.in_(symbols)).all():
        if p.bill_id is not None:
            plan_done[p.vm_symbol] = plan_done.get(p.vm_symbol, 0) + 1
        elif not p.is_cancelled:
            plan_pending[p.vm_symbol] = plan_pending.get(p.vm_symbol, 0) + 1

    for it in items:
        s = it["symbol"]
        it["pp_effective_date"] = cur_date.get(s)
        it["pp_version_count"] = ver_count.get(s, 0)
        it["pp_pending_date"] = pend.get(s)
        r = research.get(s) or {}
        it["research_date"] = r.get("research_date")
        it["research_conclusion"] = r.get("conclusion")
        it["next_review"] = r.get("next_review")
        it["plan_pending"] = plan_pending.get(s, 0)
        it["plan_done"] = plan_done.get(s, 0)
    return items


@router.get("/value-monitors", response_model=PageResponse[ValueMonitorOut])
def list_value_monitors(
    symbol: Optional[str] = Query(None, description="代码精确匹配"),
    theme: Optional[List[str]] = Query(None, description="主题多选精确匹配（主题只在标的层维护）"),
    research_state: Optional[str] = Query(
        default=None, pattern="^(none|due)$",
        description="研究状态：none=还没做过研究；due=最新一版研究的复核日已到期"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """估值监测（对应 ValueMonitor 独立表，支持代码精确/主题多选/研究状态）。

    返回体除主表全字段外，还带 v3 派生：当前生效档位日期、版本数、待生效日期、
    最新研究结论/复核日、计划待执行/已执行数。

    `theme` 走多值直通（铁律 4.5：`arrayFormat:'repeat'`），不降级成前端过滤。
    """
    query = db.query(ValueMonitor)
    if symbol:
        query = query.filter(ValueMonitor.symbol == symbol)
    if theme:
        query = query.filter(ValueMonitor.theme.in_(theme))
    if research_state == "none":
        # 「还没做过研究」比「研究过期」更常见，这是档案空白的可操作清单
        query = query.filter(~ValueMonitor.symbol.in_(db.query(VmResearch.symbol)))
    elif research_state == "due":
        # 每个标的最新一版（<=today）的 next_review 已到期
        latest = (db.query(VmResearch.symbol.label("symbol"),
                           func.max(VmResearch.research_date).label("d"))
                  .filter(VmResearch.research_date <= date.today())
                  .group_by(VmResearch.symbol).subquery())
        query = (query.join(latest, latest.c.symbol == ValueMonitor.symbol)
                 .join(VmResearch,
                       (VmResearch.symbol == latest.c.symbol)
                       & (VmResearch.research_date == latest.c.d))
                 .filter(VmResearch.next_review.isnot(None),
                         VmResearch.next_review <= date.today()))
    total = query.count()
    items = query.order_by(ValueMonitor.symbol).offset(offset).limit(limit).all()
    dicts = _enrich_value_monitors(db, [item.to_dict() for item in items])
    return {"items": dicts, "total": total, "limit": limit, "offset": offset}


@router.get("/value-monitors/coverage")
def value_monitor_coverage(db: Session = Depends(get_db)):
    """归属覆盖率：每个标的的合约行数与成交笔数（融资融券不计）。"""
    rows = service.vm_map.coverage(db)
    total_bills = sum(r["bill_rows"] or 0 for r in rows)
    return {"items": rows, "total_bills": total_bills}


@router.get("/value-monitors/unmapped")
def value_monitor_unmapped(
    limit: int = Query(300, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """未归属的合约行（排除融资融券——它本来就不归属，不是漏配）。"""
    return {
        "items": service.vm_map.unmapped(db, limit),
        "count": service.vm_map.unmapped_count(db),
    }


@router.get("/value-monitors/{id_}/detail")
def get_value_monitor_detail(id_: int, db: Session = Depends(get_db)):
    """详情抽屉首屏：主表 + 当前档位 + 最新研究 + 计划 + 归属合约 + 覆盖数。

    一次给全，避免前端开抽屉连打 5 个请求；各 Tab 的明细仍走各自端点。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    sym = vm.symbol
    plans = service.vm_plan.list_by_symbol(db, sym)
    cur_ver = service.vm_version.effective_version(db, sym)
    rows = db.execute(text("""
        SELECT g.category, g.symbol, g.count, g.pf_total,
               (SELECT COUNT(*) FROM bills_bill b
                 WHERE b.category=g.category AND b.symbol=g.symbol) AS bill_rows
        FROM bills_group_symbol g WHERE g.vm_symbol = :s
        ORDER BY bill_rows DESC
    """), {"s": sym}).mappings().all()
    trades = service.vm_map.trades_of(db, sym, limit=50)
    return {
        "vm": vm.to_dict(),
        "current_version": ({"effective_date": cur_ver.effective_date, "note": cur_ver.note}
                            if cur_ver else None),
        "versions": service.vm_version.list_versions(db, sym),
        "research": service.vm_research.list_by_symbol(db, sym),
        "plans": plans,
        "groups": [dict(r) for r in rows],
        "trades": trades,
        "logic": [VmLogicOut.model_validate(x).model_dump() for x in
                  db.query(VmLogic).filter(VmLogic.symbol == sym).order_by(VmLogic.seq).all()],
        "invalidate": [VmInvalidateOut.model_validate(x).model_dump() for x in
                       db.query(VmInvalidate).filter(VmInvalidate.symbol == sym).order_by(VmInvalidate.seq).all()],
    }


@router.get("/value-monitors/{id_}/trades")
def list_value_monitor_trades(
    id_: int,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """标的页看全部成交（含两条交易笔记）。融资融券不在其中（规则一）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return {"items": service.vm_map.trades_of(db, vm.symbol, limit)}


# ---------------------------------------------------------------- 档位版本
@router.get("/value-monitors/{id_}/versions", response_model=List[VmVersionOut])
def list_value_monitor_versions(id_: int, db: Session = Depends(get_db)):
    """该标的全部档位版本（日期倒序），每行带现算 pv_* / status / 与上一版差值。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.vm_version.list_versions(db, vm.symbol)


@router.post("/value-monitors/{id_}/versions")
def save_value_monitor_version(
    id_: int,
    payload: VmVersionSave,
    db: Session = Depends(get_db),
):
    """保存档位版本：选新日期=新建，选历史日期=改那一版。

    这是档位五档的**唯一写入口**：版本 upsert + 回写主表 pp_*（→ 钩子重算 pv_*）。
    返回 action=created|updated|unchanged，前端据此提示"将新建 X 日 / 将覆盖 X 日"。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        result = service.vm_version.save_version(
            db, vm.symbol, payload.effective_date or date.today(),
            {"pp_el": payload.pp_el, "pp_l": payload.pp_l, "pp_m": payload.pp_m,
             "pp_h": payload.pp_h, "pp_eh": payload.pp_eh},
            payload.note)
        db.commit()
        return {"status": "success", **result}
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.delete("/value-monitors/{id_}/versions/{version_id}")
def delete_value_monitor_version(id_: int, version_id: int, db: Session = Depends(get_db)):
    """删除某版。删的是"某一版档位"，不是删标的；删完主表按剩余版本回写。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    ver = db.get(VmVersion, version_id)
    if ver is None or ver.symbol != vm.symbol:
        raise HTTPException(status_code=404, detail=f"版本不存在：{version_id}")
    try:
        db.delete(ver)
        db.flush()
        service.vm_version.refresh_current_pp(db, vm.symbol)
        db.commit()
        return {"status": "success", "message": "已删除该版并回写当前档位"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")


# ---------------------------------------------------------------- 研究定稿
@router.get("/value-monitors/{id_}/research", response_model=List[VmResearchOut])
def list_value_monitor_research(id_: int, db: Session = Depends(get_db)):
    """该标的全部研究定稿（日期倒序）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.vm_research.list_by_symbol(db, vm.symbol)


@router.put("/value-monitors/{id_}/research")
def save_value_monitor_research(
    id_: int,
    payload: VmResearchSave,
    db: Session = Depends(get_db),
):
    """按 (symbol, research_date) upsert 一版研究。

    选新日期=新建一版，选历史日期=改那一版（前端必须二次确认"将改写 X 日的结论"）。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        result = service.vm_research.upsert(
            db, vm.symbol, payload.research_date,
            payload.model_dump(exclude={"research_date"}, exclude_unset=True))
        db.commit()
        return {"status": "success", **result}
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.delete("/value-monitors/{id_}/research")
def delete_value_monitor_research(
    id_: int,
    research_date: date = Query(..., description="要删除的版本日期"),
    db: Session = Depends(get_db),
):
    """删除某一版研究（按日期定位，不是按 id）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    ok = service.vm_research.delete(db, vm.symbol, research_date)
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail=f"该日期没有研究版本：{research_date}")
    return {"status": "success", "message": "已删除"}


# ---------------------------------------------------------------- 计划委托
@router.get("/value-monitors/{id_}/plans", response_model=List[VmPlanOut])
def list_value_monitor_plans(id_: int, db: Session = Depends(get_db)):
    """该标的的计划委托（含派生状态与有效触发价）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.vm_plan.list_by_symbol(db, vm.symbol)


@router.put("/value-monitors/{id_}/plans")
def save_value_monitor_plan(
    id_: int,
    payload: VmPlanSave,
    db: Session = Depends(get_db),
):
    """按 seq upsert 一条计划（同标的同 seq 覆盖）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        result = service.vm_plan.upsert(db, vm.symbol, payload.model_dump())
        db.commit()
        return {"status": "success", **result}
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.delete("/value-monitors/{id_}/plans/{seq}")
def delete_value_monitor_plan(id_: int, seq: int, db: Session = Depends(get_db)):
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    ok = service.vm_plan.delete(db, vm.symbol, seq)
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail=f"没有 seq={seq} 的计划")
    return {"status": "success", "message": "已删除"}


@router.get("/value-monitors/{id_}/unplanned")
def list_value_monitor_unplanned(id_: int, db: Session = Depends(get_db)):
    """无计划成交：成交时间早于该标的任何计划建立时间。

    这是"先斩后奏 / 事后补计划"的唯一可算证据，文本永远算不出来。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    rows = db.execute(text(service.vm_plan.unplanned_bills_sql(vm.symbol)),
                      {"symbol": vm.symbol}).mappings().all()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


# ---------------------------------------------------------------- 逻辑 / 证伪
@router.get("/value-monitors/{id_}/logic", response_model=List[VmLogicOut])
def list_value_monitor_logic(id_: int, db: Session = Depends(get_db)):
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return db.query(VmLogic).filter(VmLogic.symbol == vm.symbol).order_by(VmLogic.seq).all()


@router.put("/value-monitors/{id_}/logic")
def save_value_monitor_logic(id_: int, payload: VmLogicSave, db: Session = Depends(get_db)):
    """按 seq upsert 一条投资逻辑；state 变化时才写 state_at（"这条逻辑哪天坏的"）。

    ⚠️ 本接口是**全字段覆盖**：content/pricing/state/evidence 全部无条件写入。
    行内只改 state 的调用方必须把该行其余字段一起回填，否则 content 会被写成空串。
    """
    from datetime import datetime as _dt
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    if not (payload.content or "").strip():
        raise HTTPException(status_code=400, detail="逻辑内容不能为空")
    row = (db.query(VmLogic).filter(VmLogic.symbol == vm.symbol, VmLogic.seq == payload.seq)
           .one_or_none())
    action = "created" if row is None else "updated"
    try:
        if row is None:
            row = VmLogic(symbol=vm.symbol, seq=payload.seq)
            db.add(row)
        elif row.state != payload.state:
            row.state_at = _dt.now()
        row.content = payload.content
        row.pricing = payload.pricing
        row.state = payload.state
        row.evidence = payload.evidence
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")
    # 纲领要求逻辑 2-4 条，但**不硬拦**：先登记、后精简是正常节奏，拦了就不登记了。
    # 判据用**条数**而不是 seq：seq 会有空洞（删过第 3 条后新建得 4），拿 seq 判断会误报。
    warning = None
    if action == "created":
        total = (db.query(func.count(VmLogic.id))
                 .filter(VmLogic.symbol == vm.symbol).scalar() or 0)
        if total > 4:
            warning = f"本标的逻辑已有 {total} 条，超过纲领建议的 2-4 条，已保存（不拦截）"
    return {"status": "success", "action": action, "id": row.id, "warning": warning}


@router.delete("/value-monitors/{id_}/logic/{seq}")
def delete_value_monitor_logic(id_: int, seq: int, db: Session = Depends(get_db)):
    """删除一条投资逻辑（按 seq）。

    删前必须摘掉证伪对它的引用：`irs_vm_invalidate.logic_id` 是裸 Integer，没有外键约束，
    直接删逻辑会留下悬空 id（"对应逻辑"指向一条不存在的记录，且没有任何报错）。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    row = (db.query(VmLogic).filter(VmLogic.symbol == vm.symbol, VmLogic.seq == seq)
           .one_or_none())
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有 seq={seq} 的逻辑")
    try:
        unlinked = (db.query(VmInvalidate).filter(VmInvalidate.logic_id == row.id)
                    .update({VmInvalidate.logic_id: None}, synchronize_session=False))
        db.delete(row)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")
    return {"status": "success", "message": "已删除", "unlinked_invalidates": unlinked}


@router.get("/value-monitors/{id_}/invalidate", response_model=List[VmInvalidateOut])
def list_value_monitor_invalidate(id_: int, db: Session = Depends(get_db)):
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    rows = db.query(VmInvalidate).filter(VmInvalidate.symbol == vm.symbol) \
        .order_by(VmInvalidate.seq).all()
    out = []
    for r in rows:
        d = VmInvalidateOut.model_validate(r).model_dump()
        # 判据是否可量化：必须出现"比较符 + 数字"或"数字 + %"，只有数字不算
        d["quantified"] = bool(re.search(r"[><≥≤=]\s*\d|\d+\s*%", r.signal or ""))
        out.append(d)
    return out


@router.put("/value-monitors/{id_}/invalidate")
def save_value_monitor_invalidate(id_: int, payload: VmInvalidateSave,
                                 db: Session = Depends(get_db)):
    """按 seq upsert 一条证伪条件（只登记，不自动计算触发）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    row = (db.query(VmInvalidate)
           .filter(VmInvalidate.symbol == vm.symbol, VmInvalidate.seq == payload.seq)
           .one_or_none())
    action = "created" if row is None else "updated"
    try:
        if row is None:
            row = VmInvalidate(symbol=vm.symbol, seq=payload.seq)
            db.add(row)
        for f in ("proposition", "signal", "logic_id", "source", "action", "status", "triggered_at"):
            setattr(row, f, getattr(payload, f))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")
    # 不可量化**不阻断**：先登记、后补数字是正常节奏，硬拦会让人干脆不登记
    quantified = bool(re.search(r"[><≥≤=]\s*\d|\d+\s*%", payload.signal or ""))
    return {
        "status": "success", "action": action, "id": row.id, "quantified": quantified,
        "warning": None if quantified else "证伪信号里没有数字阈值，已保存但会被标记为「不可量化」",
    }


@router.delete("/value-monitors/{id_}/invalidate/{seq}")
def delete_value_monitor_invalidate(id_: int, seq: int, db: Session = Depends(get_db)):
    """删除一条证伪条件（按 seq）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    n = (db.query(VmInvalidate)
         .filter(VmInvalidate.symbol == vm.symbol, VmInvalidate.seq == seq)
         .delete(synchronize_session=False))
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail=f"没有 seq={seq} 的证伪条件")
    return {"status": "success", "message": "已删除"}


@router.post("/value-monitors")
def create_value_monitor(
    payload: ValueMonitorCreate,
    db: Session = Depends(get_db),
):
    """新增估值监测记录。

    v3 起档位只有一个写入口：先建主表行（pp_* 置 NULL），再 save_version() 建首版，
    由 save_version 回写主表 pp_* 并触发钩子算 pv_*。
    symbol 重复时返回 HTTP 400。
    """
    try:
        vm = ValueMonitor(
            symbol=payload.symbol,
            name=payload.name,
            theme=payload.theme,
        )
        db.add(vm)
        db.flush()
        service.vm_version.save_version(
            db, payload.symbol, payload.effective_date or date.today(),
            {"pp_el": payload.pp_el, "pp_l": payload.pp_l, "pp_m": payload.pp_m,
             "pp_h": payload.pp_h, "pp_eh": payload.pp_eh},
            "新建标的时建立首版")
        db.commit()
        return {"status": "success", "message": "新增成功"}
    except IntegrityError:
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

    - **五档不直写主表**：任一带了 pp_* 就转发 save_version()（版本表 upsert + 回写主表），
      版本日期取 payload.effective_date（空=今天）。pp_* 全空表示"不改档位"。
    - name/theme 直写主表；行情四个字段 None 表示不修改（兜底编辑）。
    - symbol 不可改。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        vm.name = payload.name
        if payload.theme is not None:
            vm.theme = payload.theme
        if payload.py_close is not None:
            vm.py_close = payload.py_close
        if payload.y_high is not None:
            vm.y_high = payload.y_high
        if payload.y_low is not None:
            vm.y_low = payload.y_low
        if payload.price is not None:
            vm.price = payload.price
        db.flush()

        pp = {"pp_el": payload.pp_el, "pp_l": payload.pp_l, "pp_m": payload.pp_m,
              "pp_h": payload.pp_h, "pp_eh": payload.pp_eh}
        ver_result = None
        if any(v is not None for v in pp.values()):
            ver_result = service.vm_version.save_version(
                db, vm.symbol, payload.effective_date or date.today(), pp, None)
        db.commit()
        return {"status": "success", "message": "修改成功", "version": ver_result}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"修改失败：{str(e)}")


@router.delete("/value-monitors/{id_}")
def delete_value_monitor(
    id_: int,
    cascade: bool = Query(False, description="true = 连同研究/档位/逻辑/证伪/计划一起删"),
    db: Session = Depends(get_db),
):
    """删除估值监测记录。

    外键是 RESTRICT：有子数据时返回 **409 + 各子表计数**，让"删标的"先看代价；
    传 ?cascade=true 才按序删子表再删主表（不删成交笔记——它挂 bill_id，与标的无关）。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    counts = {
        "version": db.query(func.count(VmVersion.id)).filter(VmVersion.symbol == vm.symbol).scalar(),
        "research": db.query(func.count(VmResearch.id)).filter(VmResearch.symbol == vm.symbol).scalar(),
        "logic": db.query(func.count(VmLogic.id)).filter(VmLogic.symbol == vm.symbol).scalar(),
        "invalidate": db.query(func.count(VmInvalidate.id)).filter(VmInvalidate.symbol == vm.symbol).scalar(),
        "plan": db.query(func.count(VmPlan.id)).filter(VmPlan.vm_symbol == vm.symbol).scalar(),
    }
    has_child = any(v for v in counts.values())
    if has_child and not cascade:
        raise HTTPException(status_code=409, detail={
            "message": f"标的 {vm.symbol} 还有档案，确认后请带 ?cascade=true",
            "symbol": vm.symbol, "counts": counts,
        })
    try:
        if has_child:
            for model, key in ((VmPlan, "vm_symbol"), (VmInvalidate, "symbol"),
                               (VmLogic, "symbol"), (VmResearch, "symbol"),
                               (VmVersion, "symbol")):
                db.query(model).filter(getattr(model, key) == vm.symbol).delete(synchronize_session=False)
            db.flush()
        db.delete(vm)
        db.commit()
        return {"status": "success", "message": "删除成功", "deleted_children": counts if has_child else {}}
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
