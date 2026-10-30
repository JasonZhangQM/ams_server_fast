# -*- coding: utf-8 -*-
"""irs 应用路由。

提供查询路由与同步路由：
- GET  /irs/value-monitors              估值监测
- GET  /irs/discounts-monitor           贴水监测
- POST /irs/sync/{target}               按 target 触发对应 service 函数链（4 种 target）
"""
from datetime import date, datetime
import re
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server_fast.app.irs import service
from server_fast.app.bds.config import Config as BdsCfg
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    DiscountMonitor,
    ValueMonitor,
    VmInvalidate,
    VmLogic,
    VmPlan,
    VmResearch,
    VmReview,
    VmStateLog,
    VmTarget,
    VmVersion,
)
from server_fast.app.irs.schemas import (
    DiscountMonitorOut,
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
    VmReviewOut,
    VmReviewSave,
    VmVersionOut,
    VmVersionSave,
    TodayItem,
    VmStateLogOut,
    VmTargetOut,
    VmTargetSave,
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

    # 剧本计数（状态在 Python 侧派生：8 个标的、剧本行数很少）
    plan_active: Dict[str, int] = {}
    for p in db.query(VmPlan).filter(VmPlan.vm_symbol.in_(symbols)).all():
        if not p.is_cancelled:
            plan_active[p.vm_symbol] = plan_active.get(p.vm_symbol, 0) + 1

    # 指数估值分位（P0-4/P2-1 纠偏，失败返回空——分位是纠偏不是列表主链）
    try:
        pct_map = service.vm_today.percentiles()
    except Exception:
        pct_map = {}

    for it in items:
        s = it["symbol"]
        it["pp_effective_date"] = cur_date.get(s)
        it["pp_version_count"] = ver_count.get(s, 0)
        it["pp_pending_date"] = pend.get(s)
        r = research.get(s) or {}
        it["research_date"] = r.get("research_date")
        it["research_conclusion"] = r.get("conclusion")
        it["next_review"] = r.get("next_review")
        it["plan_active"] = plan_active.get(s, 0)
        # 当前价所在档位带（纯派生，口径唯一在 service，不在前端各算一套）
        it["zone"] = service.vm_version.compute_zone(
            {f: it.get(f) for f in ("pp_el", "pp_l", "pp_m", "pp_h", "pp_eh")},
            it.get("price"))
        # 档位对照的 PE/PB 分位（bds 只读，非指数标的为 None）
        it["pe_pct"] = pct_map.get((s, "pe_ttm"))
        it["pb_pct"] = pct_map.get((s, "pb_mrq"))
    return items


@router.get("/today", response_model=List[TodayItem])
def get_today(db: Session = Depends(get_db)):
    """今日页：跨标的到价清单（按「需要动作」排序）。

    纯聚合、不落库：8 标的 zone / 命中剧本 / 逻辑减弱 / 证伪触发 / 复核到期 / 裸仓 /
    目标仓位与漂移，全部在 service 里算好，前端只展示不重算（铁律 4.5）。
    """
    return service.vm_today.today_items(db)


@router.get("/plans", response_model=List[VmPlanOut])
def list_all_plans(db: Session = Depends(get_db)):
    """全量剧本（跨标的），供前端归因表 plan_id → 剧本名/标的映射。

    bills 侧归因只返回裸 plan_id（bills 不依赖 irs，铁律 4.0），
    名字在这里补；前端把两边只读结果拼起来，不在后端新增跨模块调用。
    """
    return service.vm_plan.list_all(db)


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
    最新研究结论/复核日、生效剧本数。

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
    """详情抽屉首屏（概览最小集）。

    2026-10-30 瘦身：只返回概览 Tab 用到的数据 + 轻量计数。
    各 Tab 数据改为切到时懒加载（versions/research/logic/invalidate/plans/trades/groups）。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    sym = vm.symbol
    # 当前生效档位（概览五档 + 剧本 trigger_ref 解析都要用）
    cur_ver = service.vm_version.effective_version(db, sym)
    pp = {"pp_el": cur_ver.pp_el, "pp_l": cur_ver.pp_l, "pp_m": cur_ver.pp_m,
          "pp_h": cur_ver.pp_h, "pp_eh": cur_ver.pp_eh} if cur_ver else {}
    # 轻量计数：概览只关心"有多少"，不关心"每一行"
    plans_total = db.query(VmPlan).filter(VmPlan.vm_symbol == sym).count()
    plans_active = db.query(VmPlan).filter(VmPlan.vm_symbol == sym, VmPlan.is_cancelled == False).count()
    logic_total = db.query(VmLogic).filter(VmLogic.symbol == sym).count()
    invalidate_total = db.query(VmInvalidate).filter(VmInvalidate.symbol == sym).count()
    # 归属覆盖（groups + bill_rows，供概览底部文字提示）
    cov = service.vm_map.coverage(db)
    cov_row = next((r for r in cov if r["vm_symbol"] == sym), {"group_rows": 0, "bill_rows": 0})
    # 剧本前 10 条摘要（概览「剧本」板块只读展示，够用）
    plans_preview = service.vm_plan.list_by_symbol(db, sym, pp=pp)[:10]
    # 研究最新一版（概览决策卡只用这一条）
    latest_research = service.vm_research.latest(db, sym)
    vm_dict = vm.to_dict()
    vm_dict["zone"] = service.vm_version.compute_zone(
        {f: getattr(vm, f) for f in ("pp_el", "pp_l", "pp_m", "pp_h", "pp_eh")}, vm.price)
    # 档位对照的 PE/PB 分位（bds 只读，非指数标的 None）
    try:
        pct_map = service.vm_today.percentiles()
        vm_dict["pe_pct"] = pct_map.get((sym, "pe_ttm"))
        vm_dict["pb_pct"] = pct_map.get((sym, "pb_mrq"))
    except Exception:
        vm_dict["pe_pct"] = None
        vm_dict["pb_pct"] = None
    return {
        "vm": vm_dict,
        "current_version": ({"effective_date": cur_ver.effective_date, "note": cur_ver.note}
                            if cur_ver else None),
        # —— 以下是概览专用数据，不再有全量 versions/research/plans/groups/trades/logic/invalidate ——
        "plans_preview": plans_preview,   # 剧本前 10 条（概览只读展示）
        "latest_research": latest_research,  # 最新一版研究（概览决策卡）
        "plans_active_count": plans_active,
        "plans_total_count": plans_total,
        "logic_total_count": logic_total,
        "invalidate_total_count": invalidate_total,
        "group_rows": cov_row["group_rows"],
        "bill_rows": cov_row["bill_rows"],
    }


@router.get("/value-monitors/{id_}/groups")
def list_value_monitor_groups(
    id_: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    category: str | None = Query(None),
    symbol: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """对应交易代码分页（id 倒序）：分类精确、合约模糊。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return {
        "items": service.vm_map.groups_of(
            db, vm.symbol, page_size, (page - 1) * page_size, category, symbol
        ),
        "total": service.vm_map.count_groups(db, vm.symbol, category, symbol),
        "page": page,
        "page_size": page_size,
    }


@router.get("/value-monitors/{id_}/trades")
def list_value_monitor_trades(
    id_: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    plan_id: int | None = Query(None),
    account: str | None = Query(None),
    symbol: str | None = Query(None),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """标的页成交分页（含「想法」与关联剧本）。融资融券不在其中（规则一）。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    offset = (page - 1) * page_size
    return {
        "items": service.vm_map.trades_of(
            db, vm.symbol, page_size, plan_id, account, symbol, offset, category
        ),
        "total": service.vm_map.count_trades(db, vm.symbol, plan_id, account, symbol, category),
        "page": page,
        "page_size": page_size,
    }


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


# ---------------------------------------------------------------- 剧本
@router.get("/value-monitors/{id_}/plans", response_model=List[VmPlanOut])
def list_value_monitor_plans(id_: int, db: Session = Depends(get_db)):
    """该标的的剧本（含派生状态与有效触发价）。"""
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
    """按 seq upsert 一条剧本（同标的同 seq 覆盖）。"""
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
        raise HTTPException(status_code=404, detail=f"没有 seq={seq} 的剧本")
    return {"status": "success", "message": "已删除"}


# ---------------------------------------------------------------- 标的复盘
@router.get("/value-monitors/{id_}/reviews", response_model=List[VmReviewOut])
def list_value_monitor_reviews(id_: int, db: Session = Depends(get_db)):
    """该标的的全部复盘（按落笔日期倒序），同一研究版本不限条数。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.vm_review.list_by_symbol(db, vm.symbol)


@router.post("/value-monitors/{id_}/reviews", response_model=VmReviewOut)
def create_value_monitor_review(
    id_: int, payload: VmReviewSave, db: Session = Depends(get_db)
):
    """新增一条复盘；review_date 由后端填当天，锚定研究版本。"""
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        row = service.vm_review.create(db, vm.symbol, payload.research_id,
                                      payload.content)
        db.commit()
        db.refresh(row)
        return row
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.put("/reviews/{review_id}", response_model=VmReviewOut)
def update_vm_review(review_id: int, payload: VmReviewSave,
                     db: Session = Depends(get_db)):
    """修改复盘正文与所属研究版本；落笔日期保持首次写入值不变。"""
    try:
        row = service.vm_review.update(db, review_id, payload.research_id,
                                       payload.content)
        if row is None:
            raise HTTPException(status_code=404, detail=f"复盘不存在：{review_id}")
        db.commit()
        db.refresh(row)
        return row
    except HTTPException:
        db.rollback()
        raise
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.delete("/reviews/{review_id}")
def delete_vm_review(review_id: int, db: Session = Depends(get_db)):
    ok = service.vm_review.delete(db, review_id)
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail=f"复盘不存在：{review_id}")
    return {"status": "success", "message": "已删除"}


@router.get("/value-monitors/{id_}/unplanned")
def list_value_monitor_unplanned(id_: int, db: Session = Depends(get_db)):
    """无剧本成交：成交时间早于该标的任何剧本建立时间。

    这是"先斩后奏 / 事后补剧本"的唯一可算证据，文本永远算不出来。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    rows = db.execute(text(service.vm_plan.unplanned_bills_sql(vm.symbol)),
                      {"symbol": vm.symbol}).mappings().all()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


# ---------------------------------------------------------------- 逻辑 / 证伪
def _append_state_log(db: Session, subject_type: str, subject_id: int, symbol: str,
                      from_state: Optional[str], to_state: str,
                      note: Optional[str] = None) -> None:
    """追加一条状态变更审计日志（append-only，复盘用）。

    仅在主表 state/status **真正变化**时调用（值变了才写，保存同值不写）。
    首次登记（新建）时 from_state=None，表示「历史从今天起完整」。
    """
    db.add(VmStateLog(
        subject_type=subject_type,
        subject_id=subject_id,
        symbol=symbol,
        from_state=from_state,
        to_state=to_state,
        changed_at=datetime.now(),
        note=note,
    ))


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
    from_state = None if row is None else row.state
    try:
        if row is None:
            row = VmLogic(symbol=vm.symbol, seq=payload.seq)
            db.add(row)
            db.flush()  # 拿 row.id，供状态日志使用
        elif row.state != payload.state:
            row.state_at = _dt.now()
        row.content = payload.content
        row.pricing = payload.pricing
        row.state = payload.state
        row.evidence = payload.evidence
        # 状态历史化：仅 state 真变时追加（新建首次登记 from_state=NULL）
        if from_state != payload.state:
            _append_state_log(db, VmStateLog.LOGIC, row.id, vm.symbol,
                              from_state, payload.state)
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
    # P2-2 批量判定候选触发（只读，不改 status）
    from server_fast.app.irs.service.vm_today import judge_list
    judged_map = judge_list(db, vm.symbol)
    out = []
    for r in rows:
        d = VmInvalidateOut.model_validate(r).model_dump()
        # 判据是否可量化：必须出现"比较符 + 数字"或"数字 + %"，只有数字不算
        d["quantified"] = bool(re.search(r"[><≥≤=]\s*\d|\d+\s*%", r.signal or ""))
        # P2-2 自动判定候选触发结果（可算 → candidate true/false；不可算 → null）
        d["auto_judged"] = judged_map.get(r.id)
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
    from_status = None if row is None else row.status
    try:
        if row is None:
            row = VmInvalidate(symbol=vm.symbol, seq=payload.seq)
            db.add(row)
            db.flush()  # 拿 row.id，供状态日志使用
        for f in ("proposition", "signal", "logic_id", "source", "action",
                  "judge", "judge_expr", "status", "triggered_at"):
            setattr(row, f, getattr(payload, f))
        # 状态历史化：仅 status 真变时追加（新建首次登记 from_state=NULL）
        if from_status != payload.status:
            _append_state_log(db, VmStateLog.INVALIDATE, row.id, vm.symbol,
                              from_status, payload.status)
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


@router.get("/value-monitors/{id_}/state-log", response_model=List[VmStateLogOut])
def list_value_monitor_state_log(id_: int, db: Session = Depends(get_db)):
    """某标的状态演化时间轴（逻辑 state + 证伪 status 的 append-only 日志）。

    按 changed_at 正序回放「完好 → 减弱 → 破坏」「未触发 → 已触发」的完整路径。
    只读消费，供复盘视图（研究 Tab）使用；主表仍只存当前态，历史靠本表回放。
    """
    vm = db.get(ValueMonitor, id_)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return (db.query(VmStateLog)
            .filter(VmStateLog.symbol == vm.symbol)
            .order_by(VmStateLog.changed_at.asc(), VmStateLog.id.asc())
            .all())


# ---------------------------------------------------------------- 目标仓位（P3-2）
@router.get("/targets", response_model=List[VmTargetOut])
def list_targets(db: Session = Depends(get_db)):
    """全部标的的目标仓位（决策层）。漂移 = 当前 − 目标由前端今日页派生。"""
    return db.query(VmTarget).order_by(VmTarget.symbol.asc()).all()


@router.put("/targets")
def save_target(payload: VmTargetSave, db: Session = Depends(get_db)):
    """按 symbol upsert 一个目标仓位（占总资产比例 %）。target_pct=None 表示未设定。"""
    if not (payload.symbol or "").strip():
        raise HTTPException(status_code=400, detail="标的代码不能为空")
    row = (db.query(VmTarget).filter(VmTarget.symbol == payload.symbol).one_or_none())
    action = "created" if row is None else "updated"
    try:
        if row is None:
            row = VmTarget(symbol=payload.symbol)
            db.add(row)
        row.target_pct = payload.target_pct
        row.note = payload.note
        db.commit()
        return {"status": "success", "action": action, "id": row.id}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"目标仓位已存在：{payload.symbol}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.delete("/targets/{symbol}")
def delete_target(symbol: str, db: Session = Depends(get_db)):
    """删除一个标的的目标仓位。"""
    n = (db.query(VmTarget).filter(VmTarget.symbol == symbol)
         .delete(synchronize_session=False))
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail=f"没有标的 {symbol} 的目标仓位")
    return {"status": "success", "message": "已删除"}


@router.post("/value-monitors")
def create_value_monitor(
    payload: ValueMonitorCreate,
    db: Session = Depends(get_db),
):
    """新增估值监测记录。

    v3 起档位版本唯一入口是详情抽屉的「档位」Tab：
    - pp_* 有值 → 建主表 + save_version 建首版
    - pp_* 全空 → 只建主表行，档位后续去 Tab 里 save_version
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
        # 只有 pp_* 至少一个有值时才建首版档位
        pp_values = [payload.pp_el, payload.pp_l, payload.pp_m, payload.pp_h, payload.pp_eh]
        if any(v is not None for v in pp_values):
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
    cascade: bool = Query(False, description="true = 连同研究/档位/逻辑/证伪/剧本一起删"),
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


# =========================================================================
# POST 同步路由
# =========================================================================

@router.post("/sync/{target}")
def sync_data(target: str):
    """根据 target 触发对应同步逻辑。

    target 取值见 SYNC_MAP；service 函数内部自管理 session，无需注入 db。
    """
    funcs = SYNC_MAP.get(target)
    if funcs is None:
        raise HTTPException(status_code=400, detail=f"unknown target: {target}")
    return _run_sync_chain(target, funcs)
