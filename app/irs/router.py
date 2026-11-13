# -*- coding: utf-8 -*-
"""irs 应用路由。

提供查询路由与同步路由：
- GET  /irs/underlying-monitors              标的监测
- GET  /irs/discounts-monitor           贴水监测
- POST /irs/sync/{target}               按 target 触发对应 service 函数链（4 种 target）
"""
from datetime import date, datetime
import logging
import re
import threading
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server_fast.app.irs import service
from server_fast.app.bds.config import Config as BdsCfg
from server_fast.app.bds.models import BdsSyncTask
from server_fast.app.bds.schemas import BdsSyncTaskOut
from server_fast.app.bds.sync_task_utils import STOP_EVENTS, cleanup_zombies
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    DiscountMonitor,
    UnderlyingMonitor,
    UnderlyingInvalidate,
    UnderlyingLogic,
    UnderlyingPlan,
    UnderlyingResearch,
    UnderlyingReview,
    UnderlyingStateLog,
    UnderlyingTarget,
    UnderlyingVersion,
)
from server_fast.app.irs.schemas import (
    DiscountMonitorOut,
    UnderlyingMonitorCreate,
    UnderlyingMonitorOut,
    UnderlyingMonitorUpdate,
    UnderlyingInvalidateOut,
    UnderlyingInvalidateSave,
    UnderlyingLogicOut,
    UnderlyingLogicSave,
    UnderlyingPlanOut,
    UnderlyingPlanSave,
    UnderlyingResearchOut,
    UnderlyingResearchSave,
    UnderlyingReviewOut,
    UnderlyingReviewSave,
    UnderlyingVersionOut,
    UnderlyingVersionSave,
    UnderlyingStateLogOut,
    UnderlyingTargetOut,
    UnderlyingTargetSave,
    RiskBoardOut,
    SimulateIn,
    SimulateLevelsIn,
)
from server_fast.common.db import get_db, SessionLocal
from server_fast.common.pagination import PageResponse

router = APIRouter(prefix="/irs", tags=["irs"])
logger = logging.getLogger("uvicorn.error")


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
    "underlying-monitor-hlc": [service.update_underlying_monitor_hlc_sql],
    "underlying-monitor":     [service.update_underlying_monitor_em_orm],
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

def _enrich_underlying_monitors(db: Session, items: list) -> list:
    """给标的监测列表补 v3 派生字段（8 行数据，逐项一次 IN 查询，成本为零）。

    必须在 response_model 里声明这些字段，否则会被 Pydantic 裁掉（铁律 4.10）。
    """
    if not items:
        return items
    symbols = [it["symbol"] for it in items]
    today = date.today()

    # 版本聚合：当前生效版日期 / 版本数 / 最早的未来版
    ver_rows = (db.query(UnderlyingVersion.symbol,
                         func.count(UnderlyingVersion.id).label("n"),
                         func.max(UnderlyingVersion.effective_date).label("max_d"))
                .filter(UnderlyingVersion.symbol.in_(symbols), UnderlyingVersion.effective_date <= today)
                .group_by(UnderlyingVersion.symbol).all())
    cur_date = {r.symbol: r.max_d for r in ver_rows}
    ver_count = {}
    for sym, n, _ in (db.query(UnderlyingVersion.symbol, func.count(UnderlyingVersion.id), func.max(UnderlyingVersion.effective_date))
                      .filter(UnderlyingVersion.symbol.in_(symbols)).group_by(UnderlyingVersion.symbol).all()):
        ver_count[sym] = n
    pend = {}
    for sym, d in (db.query(UnderlyingVersion.symbol, func.min(UnderlyingVersion.effective_date))
                   .filter(UnderlyingVersion.symbol.in_(symbols), UnderlyingVersion.effective_date > today)
                   .group_by(UnderlyingVersion.symbol).all()):
        pend[sym] = d

    # 最新研究（<=today）
    research = service.um_research.latest_map(db, symbols)

    # 剧本计数（状态在 Python 侧派生：8 个标的、剧本行数很少）
    plan_active: Dict[str, int] = {}
    for p in db.query(UnderlyingPlan).filter(UnderlyingPlan.underlying_symbol.in_(symbols)).all():
        if not p.is_cancelled:
            plan_active[p.underlying_symbol] = plan_active.get(p.underlying_symbol, 0) + 1

    # 指数估值分位（P0-4/P2-1 纠偏，失败返回空——分位是纠偏不是列表主链）
    try:
        pct_map = service.um_today.percentiles()
    except Exception:
        pct_map = {}

    # 标的项下持仓风险敞口合计（与 /irs/simulate subject_exposure 同源、同一 SQL 口径）
    exposure_map = service.um_map.subject_exposure_map(db)

    for it in items:
        s = it["symbol"]
        it["risk_exposure"] = exposure_map.get(s)
        it["pp_effective_date"] = cur_date.get(s)
        it["pp_version_count"] = ver_count.get(s, 0)
        it["pp_pending_date"] = pend.get(s)
        r = research.get(s) or {}
        it["research_date"] = r.get("research_date")
        it["research_conclusion"] = r.get("conclusion")
        it["next_review"] = r.get("next_review")
        it["plan_active"] = plan_active.get(s, 0)
        # 当前价所在档位带（纯派生，口径唯一在 service，不在前端各算一套）
        it["zone"] = service.um_version.compute_zone(
            {f: it.get(f) for f in ("pp_el", "pp_l", "pp_m", "pp_h", "pp_eh")},
            it.get("price"))
        # 档位对照的 PE/PB 分位（bds 只读，非指数标的为 None）
        it["pe_pct"] = pct_map.get((s, "pe_ttm"))
        it["pb_pct"] = pct_map.get((s, "pb_mrq"))
    return items


@router.get("/plans", response_model=List[UnderlyingPlanOut])
def list_all_plans(db: Session = Depends(get_db)):
    """全量剧本（跨标的），供前端归因表 plan_id → 剧本名/标的映射。

    bills 侧归因只返回裸 plan_id（bills 不依赖 irs，铁律 4.0），
    名字在这里补；前端把两边只读结果拼起来，不在后端新增跨模块调用。
    """
    return service.um_plan.list_all(db)


@router.get("/um-budget")
def get_um_budget(
    symbol: str = Query(..., description="标的代码，如 SHSE.000300 / GC00Y / SHSE.510300"),
    account: Optional[str] = Query(None, description="账户；不传则取该品种组有保证金占用的账户"),
):
    """标的是否还有加仓额度（改造 4）。

    额度唯一实现在 `bills/services/budget.py`，本端点只做「标的 → 品种组」导航。
    与风险看板走同一套 `load_context/run/evaluate`，但**是另一次计算**——
    要看同一时点的数就回看板，不要拿这里的数去和半小时前的看板对比。
    """
    return service.um_budget.feasibility(symbol, account)


@router.get("/um-market-stats")
def get_um_market_stats(
    symbol: str = Query(..., description="标的代码（= irs_underlying_monitor.symbol）"),
    range_: str = Query("10y", alias="range", pattern="^(1y|3y|5y|10y|ytd|all)$",
                        description="区间：默认 10 年；ytd=年初至今，all=全史"),
    metric: str = Query("pe_ttm", pattern="^(pe_ttm|pb_mrq|ps_ttm|dy_ttm)$",
                        description="估值指标，默认 PE(TTM)"),
    db: Session = Depends(get_db),
):
    """标的档案抽屉「估值与走势」Tab（2026-11-08）。

    一次返回四块：最新估值分位（`irs_um_percentile` 唯一入口）、估值原始序列、
    累计收益率、回撤。口径全在后端算，前端零计算、不补实时价。
    无估值/行情的标的（GC00Y / SPX）返回 coverage=false + 主表字段降级，不猜价。

    ⚠️ 关联键一律用 symbol：主表 id 会漂移。
    """
    return service.um_market_stats.build(symbol, range_, metric, db)


@router.get("/underlying-monitors", response_model=PageResponse[UnderlyingMonitorOut])
def list_underlying_monitors(
    symbol: Optional[str] = Query(None, description="代码精确匹配"),
    theme: Optional[List[str]] = Query(None, description="主题多选精确匹配（主题只在标的层维护）"),
    research_state: Optional[str] = Query(
        default=None, pattern="^(none|due)$",
        description="研究状态：none=还没做过研究；due=最新一版研究的复核日已到期"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """标的监测（对应 UnderlyingMonitor 独立表，支持代码精确/主题多选/研究状态）。

    返回体除主表全字段外，还带 v3 派生：当前生效档位日期、版本数、待生效日期、
    最新研究结论/复核日、生效剧本数、标的项下持仓风险敞口合计。

    `theme` 走多值直通（铁律 4.5：`arrayFormat:'repeat'`），不降级成前端过滤。
    排序在 SQL 层按「标的项下风险敞口合计」倒序（NULL 最后、symbol 兜底），
    发生在 LIMIT/OFFSET 之前，前端不再二次排序。
    """
    query = db.query(UnderlyingMonitor)
    if symbol:
        query = query.filter(UnderlyingMonitor.symbol == symbol)
    if theme:
        query = query.filter(UnderlyingMonitor.theme.in_(theme))
    if research_state == "none":
        # 「还没做过研究」比「研究过期」更常见，这是档案空白的可操作清单
        query = query.filter(~UnderlyingMonitor.symbol.in_(db.query(UnderlyingResearch.symbol)))
    elif research_state == "due":
        # 每个标的最新一版（<=today）的 next_review 已到期
        latest = (db.query(UnderlyingResearch.symbol.label("symbol"),
                           func.max(UnderlyingResearch.research_date).label("d"))
                  .filter(UnderlyingResearch.research_date <= date.today())
                  .group_by(UnderlyingResearch.symbol).subquery())
        query = (query.join(latest, latest.c.symbol == UnderlyingMonitor.symbol)
                 .join(UnderlyingResearch,
                       (UnderlyingResearch.symbol == latest.c.symbol)
                       & (UnderlyingResearch.research_date == latest.c.d))
                 .filter(UnderlyingResearch.next_review.isnot(None),
                         UnderlyingResearch.next_review <= date.today()))
    total = query.count()
    # 相关子查询取每个标的的敞口合计：非 NULL 优先 → 敞口倒序 → symbol 兜底（结果确定）。
    # 必须写在 SQL 的 ORDER BY 里，不能在 Python 里对当页 items 排（分页会截断大敞口标的）。
    exp = service.um_map.subject_exposure_expr()
    order = text(f"({exp}) IS NULL, ({exp}) DESC, irs_underlying_monitor.symbol ASC")
    items = query.order_by(order).offset(offset).limit(limit).all()
    dicts = _enrich_underlying_monitors(db, [item.to_dict() for item in items])
    return {"items": dicts, "total": total, "limit": limit, "offset": offset}


@router.get("/underlying-monitors/coverage")
def underlying_monitor_coverage(db: Session = Depends(get_db)):
    """归属覆盖率：每个标的的合约行数与成交笔数（融资融券不计）。"""
    rows = service.um_map.coverage(db)
    total_bills = sum(r["bill_rows"] or 0 for r in rows)
    return {"items": rows, "total_bills": total_bills}


@router.get("/underlying-monitors/unmapped")
def underlying_monitor_unmapped(
    limit: int = Query(300, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """未归属的合约行（排除融资融券——它本来就不归属，不是漏配）。"""
    return {
        "items": service.um_map.unmapped(db, limit),
        "count": service.um_map.unmapped_count(db),
    }


@router.get("/underlying-monitors/{id_}/detail")
def get_underlying_monitor_detail(id_: int, db: Session = Depends(get_db)):
    """详情抽屉首屏（概览最小集）。

    2026-10-30 瘦身：只返回概览 Tab 用到的数据 + 轻量计数。
    各 Tab 数据改为切到时懒加载（versions/research/logic/invalidate/plans/trades/groups）。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    sym = um.symbol
    # 当前生效档位（概览五档 + 剧本 trigger_ref 解析都要用）
    cur_ver = service.um_version.effective_version(db, sym)
    pp = {"pp_el": cur_ver.pp_el, "pp_l": cur_ver.pp_l, "pp_m": cur_ver.pp_m,
          "pp_h": cur_ver.pp_h, "pp_eh": cur_ver.pp_eh} if cur_ver else {}
    # 轻量计数：概览只关心"有多少"，不关心"每一行"
    plans_total = db.query(UnderlyingPlan).filter(UnderlyingPlan.underlying_symbol == sym).count()
    plans_active = db.query(UnderlyingPlan).filter(UnderlyingPlan.underlying_symbol == sym, UnderlyingPlan.is_cancelled == False).count()
    logic_total = db.query(UnderlyingLogic).filter(UnderlyingLogic.symbol == sym).count()
    invalidate_total = db.query(UnderlyingInvalidate).filter(UnderlyingInvalidate.symbol == sym).count()
    # 归属覆盖（groups + bill_rows，供概览底部文字提示）
    cov = service.um_map.coverage(db)
    cov_row = next((r for r in cov if r["underlying_symbol"] == sym), {"group_rows": 0, "bill_rows": 0})
    # 剧本前 10 条摘要（概览「剧本」板块只读展示，够用）
    plans_preview = service.um_plan.list_by_symbol(db, sym, pp=pp)[:10]
    # 研究最新一版（概览决策卡只用这一条）
    latest_research = service.um_research.latest(db, sym)
    um_dict = um.to_dict()
    um_dict["zone"] = service.um_version.compute_zone(
        {f: getattr(um, f) for f in ("pp_el", "pp_l", "pp_m", "pp_h", "pp_eh")}, um.price)
    # 档位对照的 PE/PB 分位（bds 只读，非指数标的 None）
    try:
        pct_map = service.um_today.percentiles()
        um_dict["pe_pct"] = pct_map.get((sym, "pe_ttm"))
        um_dict["pb_pct"] = pct_map.get((sym, "pb_mrq"))
    except Exception:
        um_dict["pe_pct"] = None
        um_dict["pb_pct"] = None
    return {
        "um": um_dict,
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


@router.get("/underlying-monitors/{id_}/level-scenario")
def get_underlying_monitor_level_scenario(
    id_: int,
    horizon: str = Query("now", pattern="^(now|expiry)$",
                         description="时点口径：now=即期 BS / expiry=到期内在价值"),
    iv_mult: float = Query(1.0, ge=0.5, le=3.0, description="IV 倍数（首版固定 1.0）"),
    db: Session = Depends(get_db),
):
    """标的到档情景（只读现算，不落库）。

    五档并列、假设到价：只把该标的项下持仓平移到档位涨跌幅，其余持仓价格冻结
    但保证金仍计入账户分母。返回逐持仓Δ盈亏 / 逐档保证金、标的合计、
    账户与组合层风险指标、另一时点汇总、以及全部口径告警。

    与 /irs/risk-board 的区别：那边是「全组同跌」，这里是「单标的驱动」。
    行情陈旧 / 合约未认领 / 归属未命中等 ValueError 统一转 409。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        return service.um_level_scenario.build(db, um.symbol, horizon, iv_mult)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"到档情景计算失败：{str(e)}")


@router.get("/underlying-monitors/{id_}/groups")
def list_underlying_monitor_groups(
    id_: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    category: str | None = Query(None),
    symbol: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """对应交易代码分页（id 倒序）：分类精确、合约模糊。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return {
        "items": service.um_map.groups_of(
            db, um.symbol, page_size, (page - 1) * page_size, category, symbol
        ),
        "total": service.um_map.count_groups(db, um.symbol, category, symbol),
        "page": page,
        "page_size": page_size,
    }


@router.get("/underlying-monitors/{id_}/trades")
def list_underlying_monitor_trades(
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
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    offset = (page - 1) * page_size
    return {
        "items": service.um_map.trades_of(
            db, um.symbol, page_size, plan_id, account, symbol, offset, category
        ),
        "total": service.um_map.count_trades(db, um.symbol, plan_id, account, symbol, category),
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------- 档位版本
@router.get("/underlying-monitors/{id_}/versions", response_model=List[UnderlyingVersionOut])
def list_underlying_monitor_versions(id_: int, db: Session = Depends(get_db)):
    """该标的全部档位版本（日期倒序），每行带现算 pv_* / status / 与上一版差值。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.um_version.list_versions(db, um.symbol)


@router.post("/underlying-monitors/{id_}/versions")
def save_underlying_monitor_version(
    id_: int,
    payload: UnderlyingVersionSave,
    db: Session = Depends(get_db),
):
    """保存档位版本：选新日期=新建，选历史日期=改那一版。

    这是档位五档的**唯一写入口**：版本 upsert + 回写主表 pp_*（→ 钩子重算 pv_*）。
    返回 action=created|updated|unchanged，前端据此提示"将新建 X 日 / 将覆盖 X 日"。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        result = service.um_version.save_version(
            db, um.symbol, payload.effective_date or date.today(),
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


@router.delete("/underlying-monitors/{id_}/versions/{version_id}")
def delete_underlying_monitor_version(id_: int, version_id: int, db: Session = Depends(get_db)):
    """删除某版。删的是"某一版档位"，不是删标的；删完主表按剩余版本回写。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    ver = db.get(UnderlyingVersion, version_id)
    if ver is None or ver.symbol != um.symbol:
        raise HTTPException(status_code=404, detail=f"版本不存在：{version_id}")
    try:
        db.delete(ver)
        db.flush()
        service.um_version.refresh_current_pp(db, um.symbol)
        db.commit()
        return {"status": "success", "message": "已删除该版并回写当前档位"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")


# ---------------------------------------------------------------- 研究定稿
@router.get("/underlying-monitors/{id_}/research", response_model=List[UnderlyingResearchOut])
def list_underlying_monitor_research(id_: int, db: Session = Depends(get_db)):
    """该标的全部研究定稿（日期倒序）。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.um_research.list_by_symbol(db, um.symbol)


@router.put("/underlying-monitors/{id_}/research")
def save_underlying_monitor_research(
    id_: int,
    payload: UnderlyingResearchSave,
    db: Session = Depends(get_db),
):
    """按 (symbol, research_date) upsert 一版研究。

    选新日期=新建一版，选历史日期=改那一版（前端必须二次确认"将改写 X 日的结论"）。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        result = service.um_research.upsert(
            db, um.symbol, payload.research_date,
            payload.model_dump(exclude={"research_date"}, exclude_unset=True))
        db.commit()
        return {"status": "success", **result}
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.delete("/underlying-monitors/{id_}/research")
def delete_underlying_monitor_research(
    id_: int,
    research_date: date = Query(..., description="要删除的版本日期"),
    db: Session = Depends(get_db),
):
    """删除某一版研究（按日期定位，不是按 id）。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    ok = service.um_research.delete(db, um.symbol, research_date)
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail=f"该日期没有研究版本：{research_date}")
    return {"status": "success", "message": "已删除"}


# ---------------------------------------------------------------- 剧本
@router.get("/underlying-monitors/{id_}/plans", response_model=List[UnderlyingPlanOut])
def list_underlying_monitor_plans(id_: int, db: Session = Depends(get_db)):
    """该标的的剧本（含派生状态与有效触发价）。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.um_plan.list_by_symbol(db, um.symbol)


@router.put("/underlying-monitors/{id_}/plans")
def save_underlying_monitor_plan(
    id_: int,
    payload: UnderlyingPlanSave,
    db: Session = Depends(get_db),
):
    """按 seq upsert 一条剧本（同标的同 seq 覆盖）。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        result = service.um_plan.upsert(db, um.symbol, payload.model_dump())
        db.commit()
        return {"status": "success", **result}
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


@router.delete("/underlying-monitors/{id_}/plans/{seq}")
def delete_underlying_monitor_plan(id_: int, seq: int, db: Session = Depends(get_db)):
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    ok = service.um_plan.delete(db, um.symbol, seq)
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail=f"没有 seq={seq} 的剧本")
    return {"status": "success", "message": "已删除"}


# ---------------------------------------------------------------- 标的复盘
@router.get("/underlying-monitors/{id_}/reviews", response_model=List[UnderlyingReviewOut])
def list_underlying_monitor_reviews(id_: int, db: Session = Depends(get_db)):
    """该标的的全部复盘（按落笔日期倒序），同一研究版本不限条数。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return service.um_review.list_by_symbol(db, um.symbol)


@router.post("/underlying-monitors/{id_}/reviews", response_model=UnderlyingReviewOut)
def create_underlying_monitor_review(
    id_: int, payload: UnderlyingReviewSave, db: Session = Depends(get_db)
):
    """新增一条复盘；review_date 由后端填当天，锚定研究版本。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        row = service.um_review.create(db, um.symbol, payload.research_id,
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


@router.put("/reviews/{review_id}", response_model=UnderlyingReviewOut)
def update_um_review(review_id: int, payload: UnderlyingReviewSave,
                     db: Session = Depends(get_db)):
    """修改复盘正文与所属研究版本；落笔日期保持首次写入值不变。"""
    try:
        row = service.um_review.update(db, review_id, payload.research_id,
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
def delete_um_review(review_id: int, db: Session = Depends(get_db)):
    ok = service.um_review.delete(db, review_id)
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail=f"复盘不存在：{review_id}")
    return {"status": "success", "message": "已删除"}


@router.get("/underlying-monitors/{id_}/unplanned")
def list_underlying_monitor_unplanned(id_: int, db: Session = Depends(get_db)):
    """无剧本成交：成交时间早于该标的任何剧本建立时间。

    这是"先斩后奏 / 事后补剧本"的唯一可算证据，文本永远算不出来。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    rows = db.execute(text(service.um_plan.unplanned_bills_sql(um.symbol)),
                      {"symbol": um.symbol}).mappings().all()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


# ---------------------------------------------------------------- 逻辑 / 证伪
def _append_state_log(db: Session, subject_type: str, subject_id: int, symbol: str,
                      from_state: Optional[str], to_state: str,
                      note: Optional[str] = None) -> None:
    """追加一条状态变更审计日志（append-only，复盘用）。

    仅在主表 state/status **真正变化**时调用（值变了才写，保存同值不写）。
    首次登记（新建）时 from_state=None，表示「历史从今天起完整」。
    """
    db.add(UnderlyingStateLog(
        subject_type=subject_type,
        subject_id=subject_id,
        symbol=symbol,
        from_state=from_state,
        to_state=to_state,
        changed_at=datetime.now(),
        note=note,
    ))


@router.get("/underlying-monitors/{id_}/logic", response_model=List[UnderlyingLogicOut])
def list_underlying_monitor_logic(id_: int, db: Session = Depends(get_db)):
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return db.query(UnderlyingLogic).filter(UnderlyingLogic.symbol == um.symbol).order_by(UnderlyingLogic.seq).all()


@router.put("/underlying-monitors/{id_}/logic")
def save_underlying_monitor_logic(id_: int, payload: UnderlyingLogicSave, db: Session = Depends(get_db)):
    """按 seq upsert 一条投资逻辑；state 变化时才写 state_at（"这条逻辑哪天坏的"）。

    ⚠️ 本接口是**全字段覆盖**：content/pricing/state/evidence 全部无条件写入。
    行内只改 state 的调用方必须把该行其余字段一起回填，否则 content 会被写成空串。
    """
    from datetime import datetime as _dt
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    if not (payload.content or "").strip():
        raise HTTPException(status_code=400, detail="逻辑内容不能为空")
    row = (db.query(UnderlyingLogic).filter(UnderlyingLogic.symbol == um.symbol, UnderlyingLogic.seq == payload.seq)
           .one_or_none())
    action = "created" if row is None else "updated"
    from_state = None if row is None else row.state
    try:
        if row is None:
            row = UnderlyingLogic(symbol=um.symbol, seq=payload.seq)
            db.add(row)
        elif row.state != payload.state:
            row.state_at = _dt.now()
        # 先赋值 NOT NULL 字段再 flush：flush 会立即发 INSERT，
        # 若此时 content/state 仍为 None 将违反非空约束（2026-10-30 修复）
        row.content = payload.content
        row.pricing = payload.pricing
        row.state = payload.state
        row.evidence = payload.evidence
        db.flush()  # 拿 row.id，供状态日志使用
        # 状态历史化：仅 state 真变时追加（新建首次登记 from_state=NULL）
        if from_state != payload.state:
            _append_state_log(db, UnderlyingStateLog.LOGIC, row.id, um.symbol,
                              from_state, payload.state)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")
    # 纲领要求逻辑 2-4 条，但**不硬拦**：先登记、后精简是正常节奏，拦了就不登记了。
    # 判据用**条数**而不是 seq：seq 会有空洞（删过第 3 条后新建得 4），拿 seq 判断会误报。
    warning = None
    if action == "created":
        total = (db.query(func.count(UnderlyingLogic.id))
                 .filter(UnderlyingLogic.symbol == um.symbol).scalar() or 0)
        if total > 4:
            warning = f"本标的逻辑已有 {total} 条，超过纲领建议的 2-4 条，已保存（不拦截）"
    return {"status": "success", "action": action, "id": row.id, "warning": warning}


@router.delete("/underlying-monitors/{id_}/logic/{seq}")
def delete_underlying_monitor_logic(id_: int, seq: int, db: Session = Depends(get_db)):
    """删除一条投资逻辑（按 seq）。

    删前必须摘掉证伪对它的引用：`irs_um_invalidate.logic_id` 是裸 Integer，没有外键约束，
    直接删逻辑会留下悬空 id（"对应逻辑"指向一条不存在的记录，且没有任何报错）。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    row = (db.query(UnderlyingLogic).filter(UnderlyingLogic.symbol == um.symbol, UnderlyingLogic.seq == seq)
           .one_or_none())
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有 seq={seq} 的逻辑")
    try:
        unlinked = (db.query(UnderlyingInvalidate).filter(UnderlyingInvalidate.logic_id == row.id)
                    .update({UnderlyingInvalidate.logic_id: None}, synchronize_session=False))
        db.delete(row)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")
    return {"status": "success", "message": "已删除", "unlinked_invalidates": unlinked}


@router.get("/underlying-monitors/{id_}/invalidate", response_model=List[UnderlyingInvalidateOut])
def list_underlying_monitor_invalidate(id_: int, db: Session = Depends(get_db)):
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    rows = db.query(UnderlyingInvalidate).filter(UnderlyingInvalidate.symbol == um.symbol) \
        .order_by(UnderlyingInvalidate.seq).all()
    # P2-2 批量判定候选触发（只读，不改 status）
    from server_fast.app.irs.service.um_today import judge_list
    judged_map = judge_list(db, um.symbol)
    out = []
    for r in rows:
        d = UnderlyingInvalidateOut.model_validate(r).model_dump()
        # 判据是否可量化：必须出现"比较符 + 数字"或"数字 + %"，只有数字不算
        d["quantified"] = bool(re.search(r"[><≥≤=]\s*\d|\d+\s*%", r.signal or ""))
        # P2-2 自动判定候选触发结果（可算 → candidate true/false；不可算 → null）
        d["auto_judged"] = judged_map.get(r.id)
        out.append(d)
    return out


@router.put("/underlying-monitors/{id_}/invalidate")
def save_underlying_monitor_invalidate(id_: int, payload: UnderlyingInvalidateSave,
                                 db: Session = Depends(get_db)):
    """按 seq upsert 一条证伪条件（只登记，不自动计算触发）。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    row = (db.query(UnderlyingInvalidate)
           .filter(UnderlyingInvalidate.symbol == um.symbol, UnderlyingInvalidate.seq == payload.seq)
           .one_or_none())
    action = "created" if row is None else "updated"
    from_status = None if row is None else row.status
    try:
        if row is None:
            row = UnderlyingInvalidate(symbol=um.symbol, seq=payload.seq)
            db.add(row)
        # 先赋值 NOT NULL 字段再 flush：flush 会立即发 INSERT，
        # 若此时 proposition/signal 仍为 None 将违反非空约束（2026-10-30 修复）
        for f in ("proposition", "signal", "logic_id", "source", "action",
                  "judge", "judge_expr", "status", "triggered_at"):
            setattr(row, f, getattr(payload, f))
        db.flush()  # 拿 row.id，供状态日志使用
        # 状态历史化：仅 status 真变时追加（新建首次登记 from_state=NULL）
        if from_status != payload.status:
            _append_state_log(db, UnderlyingStateLog.INVALIDATE, row.id, um.symbol,
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


@router.delete("/underlying-monitors/{id_}/invalidate/{seq}")
def delete_underlying_monitor_invalidate(id_: int, seq: int, db: Session = Depends(get_db)):
    """删除一条证伪条件（按 seq）。"""
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    n = (db.query(UnderlyingInvalidate)
         .filter(UnderlyingInvalidate.symbol == um.symbol, UnderlyingInvalidate.seq == seq)
         .delete(synchronize_session=False))
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail=f"没有 seq={seq} 的证伪条件")
    return {"status": "success", "message": "已删除"}


@router.get("/underlying-monitors/{id_}/state-log", response_model=List[UnderlyingStateLogOut])
def list_underlying_monitor_state_log(id_: int, db: Session = Depends(get_db)):
    """某标的状态演化时间轴（逻辑 state + 证伪 status 的 append-only 日志）。

    按 changed_at 正序回放「完好 → 减弱 → 破坏」「未触发 → 已触发」的完整路径。
    只读消费，供复盘视图（研究 Tab）使用；主表仍只存当前态，历史靠本表回放。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    return (db.query(UnderlyingStateLog)
            .filter(UnderlyingStateLog.symbol == um.symbol)
            .order_by(UnderlyingStateLog.changed_at.asc(), UnderlyingStateLog.id.asc())
            .all())


# ---------------------------------------------------------------- 目标仓位（P3-2）
@router.get("/targets", response_model=List[UnderlyingTargetOut])
def list_targets(db: Session = Depends(get_db)):
    """全部标的的目标仓位（决策层）。漂移 = 当前 − 目标由前端今日页派生。"""
    return db.query(UnderlyingTarget).order_by(UnderlyingTarget.symbol.asc()).all()


@router.put("/targets")
def save_target(payload: UnderlyingTargetSave, db: Session = Depends(get_db)):
    """按 symbol upsert 一个目标仓位（占总资产比例 %）。target_pct=None 表示未设定。"""
    if not (payload.symbol or "").strip():
        raise HTTPException(status_code=400, detail="标的代码不能为空")
    row = (db.query(UnderlyingTarget).filter(UnderlyingTarget.symbol == payload.symbol).one_or_none())
    action = "created" if row is None else "updated"
    try:
        if row is None:
            row = UnderlyingTarget(symbol=payload.symbol)
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
    n = (db.query(UnderlyingTarget).filter(UnderlyingTarget.symbol == symbol)
         .delete(synchronize_session=False))
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail=f"没有标的 {symbol} 的目标仓位")
    return {"status": "success", "message": "已删除"}


@router.post("/underlying-monitors")
def create_underlying_monitor(
    payload: UnderlyingMonitorCreate,
    db: Session = Depends(get_db),
):
    """新增标的监测记录。

    v3 起档位版本唯一入口是详情抽屉的「档位」Tab：
    - pp_* 有值 → 建主表 + save_version 建首版
    - pp_* 全空 → 只建主表行，档位后续去 Tab 里 save_version
    symbol 重复时返回 HTTP 400。
    """
    try:
        um = UnderlyingMonitor(
            symbol=payload.symbol,
            name=payload.name,
            theme=payload.theme,
        )
        db.add(um)
        db.flush()
        # 只有 pp_* 至少一个有值时才建首版档位
        pp_values = [payload.pp_el, payload.pp_l, payload.pp_m, payload.pp_h, payload.pp_eh]
        if any(v is not None for v in pp_values):
            service.um_version.save_version(
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


@router.put("/underlying-monitors/{id_}")
def update_underlying_monitor(
    id_: int,
    payload: UnderlyingMonitorUpdate,
    db: Session = Depends(get_db),
):
    """修改标的监测记录。

    - **五档不直写主表**：任一带了 pp_* 就转发 save_version()（版本表 upsert + 回写主表），
      版本日期取 payload.effective_date（空=今天）。pp_* 全空表示"不改档位"。
    - name/theme 直写主表；行情四个字段 None 表示不修改（兜底编辑）。
    - symbol 不可改。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    try:
        um.name = payload.name
        if payload.theme is not None:
            um.theme = payload.theme
        if payload.py_close is not None:
            um.py_close = payload.py_close
        if payload.y_high is not None:
            um.y_high = payload.y_high
        if payload.y_low is not None:
            um.y_low = payload.y_low
        if payload.price is not None:
            um.price = payload.price
        db.flush()

        pp = {"pp_el": payload.pp_el, "pp_l": payload.pp_l, "pp_m": payload.pp_m,
              "pp_h": payload.pp_h, "pp_eh": payload.pp_eh}
        ver_result = None
        if any(v is not None for v in pp.values()):
            ver_result = service.um_version.save_version(
                db, um.symbol, payload.effective_date or date.today(), pp, None)
        db.commit()
        return {"status": "success", "message": "修改成功", "version": ver_result}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"修改失败：{str(e)}")


@router.delete("/underlying-monitors/{id_}")
def delete_underlying_monitor(
    id_: int,
    cascade: bool = Query(False, description="true = 连同研究/档位/逻辑/证伪/剧本一起删"),
    db: Session = Depends(get_db),
):
    """删除标的监测记录。

    外键是 RESTRICT：有子数据时返回 **409 + 各子表计数**，让"删标的"先看代价；
    传 ?cascade=true 才按序删子表再删主表（不删成交笔记——它挂 bill_id，与标的无关）。
    """
    um = db.get(UnderlyingMonitor, id_)
    if um is None:
        raise HTTPException(status_code=404, detail=f"记录不存在：{id_}")
    counts = {
        "version": db.query(func.count(UnderlyingVersion.id)).filter(UnderlyingVersion.symbol == um.symbol).scalar(),
        "research": db.query(func.count(UnderlyingResearch.id)).filter(UnderlyingResearch.symbol == um.symbol).scalar(),
        "logic": db.query(func.count(UnderlyingLogic.id)).filter(UnderlyingLogic.symbol == um.symbol).scalar(),
        "invalidate": db.query(func.count(UnderlyingInvalidate.id)).filter(UnderlyingInvalidate.symbol == um.symbol).scalar(),
        "plan": db.query(func.count(UnderlyingPlan.id)).filter(UnderlyingPlan.underlying_symbol == um.symbol).scalar(),
    }
    has_child = any(v for v in counts.values())
    if has_child and not cascade:
        raise HTTPException(status_code=409, detail={
            "message": f"标的 {um.symbol} 还有档案，确认后请带 ?cascade=true",
            "symbol": um.symbol, "counts": counts,
        })
    try:
        if has_child:
            for model, key in ((UnderlyingPlan, "underlying_symbol"), (UnderlyingInvalidate, "symbol"),
                               (UnderlyingLogic, "symbol"), (UnderlyingResearch, "symbol"),
                               (UnderlyingVersion, "symbol")):
                db.query(model).filter(getattr(model, key) == um.symbol).delete(synchronize_session=False)
            db.flush()
        db.delete(um)
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

# -------------------------------------------------------------------------
# 最新估值分位截面表（irs_um_percentile，2026-11-05）
# 🔴 必须注册在 `POST /sync/{target}` 之前：FastAPI 按注册顺序匹配，catch-all 在前
#    会把 /sync/um-percentile 吃掉并返回「unknown target」。
# -------------------------------------------------------------------------

def _run_um_pct_thread(task_id: int, total: int, stop_event: threading.Event):
    """后台线程：刷新最新分位截面表，结构化审计写进 task.result。

    与 `_run_valuation_pct_thread`（bds）同构：自建 SessionLocal、复用共享
    `STOP_EVENTS`（`POST /bds/stop/batch-sync` 可停）、每标的一次 commit 刷
    `update_time`（防 5 分钟僵尸清理误判）。审计只写 `result`，不写 `errors`（🔒4.8）。
    """
    db = SessionLocal()
    task = db.query(BdsSyncTask).get(task_id)
    try:
        task.status = "running"
        task.start_time = datetime.now()
        db.commit()

        def on_progress(done: int, total_: int):
            s = SessionLocal()
            try:
                t = s.query(BdsSyncTask).get(task_id)
                t.done_count = done
                t.progress = round(done / total_ * 100, 2) if total_ else 100
                s.commit()
            finally:
                s.close()

        result = service.um_percentile.refresh(
            on_progress=on_progress, should_stop=stop_event.is_set)

        stopped = stop_event.is_set()
        task.status = "stopped" if stopped else "done"
        # 与批量同步一致：停止时进度停在最后一次 on_progress 的位置，不伪装成 100%
        if not stopped:
            task.done_count = total
            task.progress = 100
        task.success_count = result["rows_upserted"]
        task.no_data_count = 0
        task.error_count = 0
        task.errors = None
        task.result = result
        task.end_time = datetime.now()
        db.commit()
        print(f'[um-pct-latest #{task_id}] {task.status}：'
              f'{result["rows_upserted"]} 行 / 删 {result["rows_deleted"]}，'
              f'过滤无数据 {result["excluded_no_data"]}，{result["duration_ms"]} ms')
    except Exception as e:
        logger.exception("最新分位刷新失败 #%s", task_id)
        task.status = "failed"
        task.errors = [{"symbol": "", "table": "irs-um-percentile",
                        "message": f"{type(e).__name__}: {e}"}]
        task.end_time = datetime.now()
        db.commit()
        print(f'[um-pct-latest #{task_id}] 失败: {e}')
    finally:
        STOP_EVENTS.pop(task_id, None)
        db.close()


@router.post("/sync/um-percentile", response_model=BdsSyncTaskOut)
def sync_um_percentile():
    """刷新 `irs_um_percentile`（最新估值分位，一标的一行）。

    **范围固定 = `irs_underlying_monitor` 全部标的**，无入参、无范围选择；上游两张
    估值表都查不到的标的（SPX 走 CAPE、GC00Y 无 PE）自动剔除并记 `excluded_no_data`。
    范围外（监测表已删）的行会被删除 —— 本表是监测表的纯镜像。

    口径与 `bds_valuation_percentile` 同源（同一个 `build_rows`），只取锚点那一行；
    全量重算（不做增量），6~8 标的约 2~4 s。

    ⚠️ 与所有同步任务共用 `bds_sync_task` 全库一把锁：任意任务 running 时返回 409。
    建议顺序：批量同步 → 成分增量 → 指数估值增量 → **本任务**（历史落盘可另行按需跑）。
    停止：`POST /bds/stop/batch-sync?task_id=<id>`。
    """
    db = next(get_db())
    try:
        # 1. 先校验范围，避免非法状态在任务表留垃圾记录
        symbols = service.um_percentile.monitor_symbols(db)
        if not symbols:
            raise HTTPException(status_code=400,
                                detail="标的监测表为空，无处可刷（先在 /irs/underlying-monitors 加标的）")

        # 2. 僵尸清理 + 全局互斥（与 bds 各同步端点同一把锁）
        cleanup_zombies(db, minutes=5)
        running = db.query(BdsSyncTask).filter(BdsSyncTask.status == "running").first()
        if running:
            raise HTTPException(status_code=409,
                                detail=f"已有进行中的任务 #{running.id}，请等待完成后再启动")

        # 3. 建任务（scope=um_pct_latest，13 字符 ≤ String(16)）
        task = BdsSyncTask(
            scope="um_pct_latest",
            scope_params={
                "subjects": symbols,
                "subject_count": len(symbols),
                "range_source": "irs_underlying_monitor",
            },
            tables=["irs-um-percentile"],
            status="running",
            total_count=len(symbols),
            done_count=0, success_count=0, no_data_count=0, error_count=0,
            progress=0, errors=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # 4. 注册停止信号 + 起 daemon 线程
        stop_event = threading.Event()
        STOP_EVENTS[task.id] = stop_event
        threading.Thread(
            target=_run_um_pct_thread,
            args=(task.id, len(symbols), stop_event),
            daemon=True,
        ).start()
        return task
    finally:
        db.close()


@router.get("/um-percentile/stats")
def um_percentile_stats():
    """最新分位表只读概览（行数 / calc_at / 各标的 as_of 与 pe、pb 10y 分位）。

    供批量同步卡片展示「表是否新鲜」；**不参与页面读取链路**，列表页/今日页只读
    `um_today.percentiles()`。
    """
    return service.um_percentile.stats()


@router.post("/sync/{target}")
def sync_data(target: str):
    """根据 target 触发对应同步逻辑。

    target 取值见 SYNC_MAP；service 函数内部自管理 session，无需注入 db。
    """
    funcs = SYNC_MAP.get(target)
    if funcs is None:
        raise HTTPException(status_code=400, detail=f"unknown target: {target}")
    return _run_sync_chain(target, funcs)


# =========================================================================
# 风险看板（L3）：实时只读 + 日终落库（2026-11-08 自 bills 模块整体迁入）
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
    try:
        return service.risk_scenario.compute(horizon=horizon, iv_mult=iv_mult,
                                             include_detail=detail)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"风险看板计算失败：{str(e)}")


@router.post("/simulate")
def simulate_workbench(body: SimulateIn, db: Session = Depends(get_db)):
    """标的推演：多标的假设价 → 组合风险/收益联动推演（只读，不落库）。

    overrides：{标的代码: 假设价}；空 dict = 全部现价（0% 基准）。
    非法标的/价格/上下文不可用 → 400；与风险看板、level-scenario 双锚点同口径
    （探针 scripts/_probe_um_simulate.py 验收）。
    """
    try:
        return service.um_simulate.build(db, body.overrides, body.horizon, body.iv_mult)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"推演计算失败：{str(e)}")


@router.post("/simulate/levels")
def simulate_level_pressure(body: SimulateLevelsIn, db: Session = Depends(get_db)):
    """五档档位压力（只读、不落库）：全部标的同步到同一档，一次返回五档结果。

    档位价取主表 pp_*，与标的监测 /risk 左栏档位条同源；
    每档给组合 / 分账户 / 逐仓三层读数，供压力表、逐张合约、保证金结构图共用。
    非法时点 / 上下文不可用 → 400；引擎异常 → 500。
    """
    try:
        return service.um_simulate.build_levels(db, body.horizon, body.iv_mult)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"档位压力计算失败：{str(e)}")


@router.post("/run/risk-board")
def run_risk_board():
    """日终重算：停用过期合约 → 计算 → UPSERT irs_scenario_grid。

    同步执行（无独立任务表；/bills/sync/group 末段另有任务化的 `_persist_risk_grid`），
    前端按钮需防重复点击。
    """
    try:
        out = service.risk_scenario.run_and_persist()
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
