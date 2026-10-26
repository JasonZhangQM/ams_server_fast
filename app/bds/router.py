# -*- coding: utf-8 -*-
"""bds 应用路由。

bds 模块只负责"数据管理"：数据同步 + 基础查询 + 下拉选项。
可视化分析查询已迁移至 irs 模块。

提供查询路由与同步路由：
- GET  /bds/trade-dates             查询交易日历
- GET  /bds/symbol-infos            查询证券信息
- GET  /bds/symbol-industries       行业下拉选项
- GET  /bds/index-histories         指数历史行情
- GET  /bds/index-codes             指数代码下拉
- GET  /bds/index-constituents      指数成分股
- GET  /bds/index-cum-returns       指数累计收益率
- GET  /bds/fund-balances           资产负债表
- GET  /bds/fund-incomes            利润表
- GET  /bds/fund-cashflows         现金流量表
- GET  /bds/finance-derivs          财务指标
- GET  /bds/daily-valuations        估值指标
- GET  /bds/daily-mktvalues         每日市值
- GET  /bds/economic-indicators     经济指标（基础查询，可视化分析用 irs）
- GET  /bds/economic-indicator-codes 经济指标代码下拉
- GET  /bds/gold-reserves           黄金储备
- GET  /bds/gold-reserve-countries  黄金储备国家下拉
- GET  /bds/daily-indicators        美债收益率指标
- GET  /bds/daily-indicator-codes   美债收益率代码下拉
- POST /bds/sync/{target}           数据同步（按 target 触发对应 service）
- POST /bds/sync/index-constituent  指数成分股同步/回填（异步任务，复用 bds_sync_task 轮询进度）
- POST /bds/sync/mark-synced        标记标的当日已完成批量同步（写 bds_symbol_info.sync_at）
"""
from datetime import date, datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel as PydanticModel
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, select, text

from server_fast.app.bds.models import (
    BdsSyncTask,
    DailyIndicator, 
    DailyMktvalue, 
    DailyValuation, 
    EconomicIndicator, 
    FinanceDeriv,
    FundBalance,
    FundCashflow, 
    FundIncome, 
    GoldReserve, 
    IndexConstituent, 
    IndexConstituentSync,
    IndexHistory, 
    OptionConfig,
    OptionMarket,
    SymbolInfo, 
    TradeDate,
)
from server_fast.app.bds.schemas import (
    BdsSyncTaskOut,
    BatchSyncRequest,
    DailyIndicatorOut,
    DailyMktvalueOut,
    DailyValuationOut,
    EconomicIndicatorOut,
    FinanceDerivOut,
    FundBalanceOut,
    FundCashflowOut,
    FundIncomeOut,
    GoldReserveOut,
    IndexConstituentOut,
    IndexConstituentSyncRequest,
    IndexHistoryOut,
    IndexValuationRow,
    IndexValuationSyncRequest,
    MacroSyncRequest,
    OptionConfigBackfillResult,
    OptionConfigCreate,
    OptionConfigOut,
    OptionConfigUpdate,
    OptionMarketOut,
    OptionSyncPlanOut,
    SymbolInfoOut,
    TradeDateOut,
)
from server_fast.app.bds.services import (
    fetch_realtime_index_prices,
    insert_trade_date_em_sql,
    refresh_table_stat,
    resolve_total,
    STAT_TABLES,
    upsert_all_gold_reserves_sql,
    upsert_all_daily_indicators_sql,
    upsert_daily_valuation_sql,
    upsert_daily_mktvalue_sql,
    upsert_economic_indicator_from_wscn_sql,
    upsert_finance_deriv_sql,
    upsert_fund_balance_sql,
    upsert_fund_cashflow_sql,
    upsert_fund_income_sql,
    upsert_gold_reserve_sql,
    upsert_index_constituent_sql,
    upsert_index_history_sql,
    sync_one_index,
    sync_symbol_info_all,
    upsert_symbol_info_excel_sql,
    upsert_symbol_info_gm_sql,
    upsert_daily_indicator_sql,
    plan_index_constituent_backfill,
)
from server_fast.app.bds.config import Config
from server_fast.app.bds.services import index_valuation
from server_fast.app.bds.services.market_data import option_market
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.common.db import SessionLocal, get_db
from server_fast.common.pagination import PageResponse

# 批量同步后台线程需要的依赖
import logging
import re
import threading
from server_fast.sync_runner import TABLES, resolve_symbols, run_tasks

logger = logging.getLogger("uvicorn.error")

# 模块级停止信号字典：{task_id: threading.Event}，供 POST /bds/stop/batch-sync 触发
_stop_events: dict[int, threading.Event] = {}

router = APIRouter(prefix="/bds", tags=["bds"])

# 列表接口 offset 上限（护栏，不改变分页语义）
#
# 背景：MySQL 的 OFFSET 分页必须先丢弃前 offset 行才能返回本页，代价 O(offset)。
# 两张 6M 级日频表（bds_daily_valuation / bds_daily_mktvalue）实测在 offset≈9万~10万
# 处执行计划从 `type=index key=k_dv_td` 退化为 `type=ALL + Using filesort`（扫 636 万行，
# 与 offset 大小无关），耗时从 2s 跳到 61s；点「最后一页」（offset=6,885,940）更需 142s
# （全表扫描 31s + filesort ~111s）。前端 axios 默认 10s 超时必然失败，且端点是同步 def，
# 客户端断开不取消服务端查询 ⇒ 每次点击白占一个连接与线程 142s，连点会冲掉
# innodb_buffer_pool_size(128MB) 并拖慢其他请求。
#
# ⚠️ 取值必须落在执行计划悬崖之前。实测（bds_daily_valuation，6,431,637 行，128MB buffer pool）：
#   offset=90,000   → type=index  key=k_dv_td            1.81s  ✅ 仍走索引
#   offset=95,000+  → type=ALL + Using filesort          92s    ❌ 全表扫描 + 完整排序
# 悬崖精确位置在 90,000~95,000 之间（9 万仍在索引上）。取 50,000：末页实测 0.6~1.3s（两表），
# 距悬崖留一倍余量，可达深度是 10,000 的 5 倍。曾试 100,000（崖后：末页 86.4s、前端 10s 必超时，
# 且被放行的正是必然失败的那一页）⇒ 已否，上限必须停在悬崖之前。
# 另：任一 symbol 的全部历史行数上限 = 交易日总数 8,742，故带 symbol 筛选的请求永不触顶。
MAX_LIST_OFFSET = 50_000


def _guard_list_offset(offset: int) -> None:
    """拦截过深的 offset（见 MAX_LIST_OFFSET 说明），避免 61~142s 的深翻页查询。"""
    if offset > MAX_LIST_OFFSET:
        raise HTTPException(
            status_code=400,
            detail=(
                f"offset 超出上限 {MAX_LIST_OFFSET}（当前 offset={offset}）。"
                "深翻页需全表扫描，耗时可达数分钟；请用「股票代码」或「交易起始日」"
                "筛选缩小范围后再翻页。"
            ),
        )


def _cleanup_zombies(db: Session, minutes: int = 5) -> int:
    """清理僵尸任务（running 状态超过指定分钟数未更新）。返回清理数量。"""
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(minutes=minutes)
    zombies = db.query(BdsSyncTask).filter(
        BdsSyncTask.status == "running",
        BdsSyncTask.update_time < cutoff,
    ).all()
    for z in zombies:
        z.status = "failed"
        z.errors = (z.errors or []) + [{"symbol": "", "table": "", "message": f"僵尸任务（running 超 {minutes} 分钟未更新，后端重启残留）"}]
        z.end_time = z.update_time
        print(f'[batch-sync] 清理僵尸 #{z.id} (update_time={z.update_time})')
    if zombies:
        db.commit()
    return len(zombies)


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


_GM_FATAL_CODE_RE = re.compile(r'"status"\s*:\s*(\d+)')
_GM_FATAL_HINTS = ('1001', '无法连接到终端服务', '终端服务', 'TimeoutError', '连接超时', '无法连接',
                   'gm 终端', '请检查 gm 终端')


def _is_gm_fatal(raw_error: str) -> bool:
    """判断 gm 错误是否为终端断连等致命错误（需立即中止批量同步）。"""
    if not raw_error:
        return False
    # 先匹配 gm 结构化错误里的 status 码
    m = _GM_FATAL_CODE_RE.search(raw_error)
    if m and m.group(1) in ('1001', '1002'):  # 1001=无法连接, 1002=连接超时
        return True
    # 再关键词兜底
    raw_lower = raw_error.lower()
    return any(h.lower() in raw_lower for h in _GM_FATAL_HINTS)


def _extract_gm_code(raw_error: str) -> int | None:
    """从 gm 原始错误字符串里提取 status 码。"""
    if not raw_error:
        return None
    m = _GM_FATAL_CODE_RE.search(raw_error)
    return int(m.group(1)) if m else None


def _build_sync_response(symbol: str, steps: dict, errors: dict | None = None,
                         skip_label: str = "报告期") -> dict:
    """根据 steps 中 symbol 对应的保存条数构建同步响应。

    - -2：跳过（库内已是最新<skip_label>，未调 gm）
    - -1：同步失败（gm 终端未启动或接口异常）
    - 0：无数据可导入
    - >0：成功导入 count 条

    skip_label: 仅影响 -2 分支文案的中缀 —— 日频两表（每日估值/市值）传「交易日」，
                报告期类端点走默认「报告期」以保持原文案不变。
    errors: dict 可选，service 返回的原始错误字典 {symbol: str(e)}。
            若 errors[symbol] 命中 gm 终端断连，response 会附加 fatal=True + gm_code 字段，
            调用方（sync_runner）应立即中止整个批量同步。
    """
    raw = (errors or {}).get(symbol, '') if errors else ''
    gm_code = _extract_gm_code(raw)
    fatal = _is_gm_fatal(raw)

    count = steps.get(symbol, 0)
    if count == -2:
        return {"status": "no_data", "message": f"已是最新{skip_label}，无需导入：{symbol}"}
    if count == -1:
        body: dict = {"status": "error"}
        if fatal:
            body.update(fatal=True, gm_code=gm_code,
                        message=f"gm 终端不可用（code={gm_code or '?'}）：{symbol}")
        else:
            body.update(message=f"同步失败：{symbol}，请检查 gm 终端是否启动")
        return body
    if count == 0:
        return {"status": "no_data", "message": f"无数据可导入：{symbol}，请检查 gm 终端是否启动"}
    return {"status": "success", "message": f"同步完成：{symbol}，更新 {count} 条", "steps": steps}


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
    request: Request,  # 仅用于护栏：拦截已删除的旧参数 exclude_synced_today
    symbol: Optional[str] = Query(None, description="代码精确匹配（对应 UK 最左前缀）"),
    industry: Optional[str] = Query(None, description="行业精确匹配（list_filter）"),
    market: Optional[str] = Query(None, description="市场精确匹配：上交所/深交所/北交所"),
    board: Optional[str] = Query(None, description="板块精确匹配：主板/创业板/科创板/北交所"),
    keyword: Optional[str] = Query(None, description="代码或名称任一模糊匹配（OR，用于远程搜索）"),
    exclude_synced_since: Optional[date] = Query(
        None,
        description="剔除 sync_at >= 该日期（服务器本地日）的标的，用于批量同步断点续跑；"
                    "不传=不跳过。例：2026-10-18 表示跳过 18 日起已同步的代码",
    ),
    sync_status: Optional[str] = Query(
        None,
        description="当日同步状态筛选：synced=仅今日已同步；unsynced=仅今日未同步（含从未同步）；不传=全部",
        pattern="^(synced|unsynced)$",
    ),
    trading_status: Optional[str] = Query(
        None,
        description="退市状态筛选：trading=仅在市（含已公告未退市）；delisted=仅已实际退市；不传=全部",
        pattern="^(trading|delisted)$",
    ),
    only_stock: bool = Query(
        False,
        description="为 True 时只返回 A 股个股（market 非空），剔除指数（4 沪指 + 创业板指）与美股/黄金",
    ),
    limit: int = Query(86, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询证券基本信息，支持可选过滤。

    过滤规则：
    - symbol          → == 精确匹配（对应 UK 最左前缀）
    - industry        → == 精确匹配
    - market / board  → == 精确匹配（取值域固定，前端用常量下拉）
    - keyword         → symbol/name 任一包含即命中（OR，远程搜索保留模糊）
    - exclude_synced_since → 剔除 sync_at >= 该日期 的标的（sync_at 为空或早于该日期者保留）
    - sync_status     → 当日同步状态：synced=仅今日已同步，unsynced=仅今日未同步，不传=全部（见下）
    - trading_status  → 退市状态筛选：trading=仅在市，delisted=仅已退市，不传=全部（见下）
    - only_stock      → 仅 A 股个股（market 非空）；指数/美股/黄金的 market 恒为 NULL，用于批量同步剔除指数
    """
    # 护栏：旧参数 exclude_synced_today 已于 2026-10-19 删除。
    # FastAPI 对未声明的 query 参数是静默忽略 ⇒ 不拦的话旧调用会悄悄退化成「不跳过」，
    # 把已同步的标的整轮重跑（每只 6 张表空转）。因此显式拒绝并提示改名。
    if "exclude_synced_today" in request.query_params:
        raise HTTPException(
            status_code=400,
            detail="参数 exclude_synced_today 已删除，请改用 exclude_synced_since=YYYY-MM-DD",
        )

    query = db.query(SymbolInfo)
    if symbol:
        query = query.filter(SymbolInfo.symbol == symbol)
    # 仅 A 股个股：market 由 symbol 前缀推导（services/symbol_info/market_board.py），
    # 指数（4 沪指 + SZSE.399006）与 SPX/NDX/NDX100/GC00Y 恒为 NULL ⇒ 非空即 A 股个股。
    if only_stock:
        query = query.filter(SymbolInfo.market.isnot(None))
    # 市场 / 板块 / 行业：精确匹配，传 '__EMPTY__' 时筛选 NULL 或空串
    if market == '__EMPTY__':
        query = query.filter(or_(SymbolInfo.market.is_(None), SymbolInfo.market == ''))
    elif market:
        query = query.filter(SymbolInfo.market == market)
    if board == '__EMPTY__':
        query = query.filter(or_(SymbolInfo.board.is_(None), SymbolInfo.board == ''))
    elif board:
        query = query.filter(SymbolInfo.board == board)
    if industry == '__EMPTY__':
        query = query.filter(or_(SymbolInfo.industry.is_(None), SymbolInfo.industry == ''))
    elif industry:
        query = query.filter(SymbolInfo.industry == industry)
    # 退市状态筛选（三态，无「默认排除退市」的隐式行为）
    # - trading  = 仅在市（delisted_date 为 NULL，或为未来日期 ⇒ 已公告未退市）
    # - delisted = 仅已实际退市（delisted_date 已到期）
    # - 不传     = 全部，不过滤
    today = date.today()
    if trading_status == 'delisted':
        query = query.filter(SymbolInfo.delisted_date.isnot(None), SymbolInfo.delisted_date <= today)
    elif trading_status == 'trading':
        query = query.filter(
            or_(SymbolInfo.delisted_date.is_(None), SymbolInfo.delisted_date > today)
        )
    if keyword:
        # 关键字对 symbol / name / sec_abbr 做 OR 模糊匹配，支持输入代码、全称、拼音缩写搜索
        query = query.filter(
            or_(
                SymbolInfo.symbol.contains(keyword),
                SymbolInfo.name.contains(keyword),
                SymbolInfo.sec_abbr.contains(keyword),
            )
        )
    # 当日同步状态筛选（三态，与 trading_status 同一风格）
    # - synced   = 仅今日已批量同步（sync_at == 今日）
    # - unsynced = 仅今日未同步（sync_at 为 NULL 或早于今日）
    # - 不传     = 全部，不过滤
    if sync_status == 'synced':
        query = query.filter(SymbolInfo.sync_at == today)
    elif sync_status == 'unsynced':
        query = query.filter(or_(SymbolInfo.sync_at.is_(None), SymbolInfo.sync_at != today))
    if exclude_synced_since:
        # 只保留「从未标记」或「标记日期早于所选日期」的标的；sync_at 为未来值也按已同步剔除
        query = query.filter(
            or_(SymbolInfo.sync_at.is_(None), SymbolInfo.sync_at < exclude_synced_since)
        )
    total = query.count()  # 应用所有过滤条件后的总记录数
    items = query.offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/symbol-industries")
def list_symbol_industries(db: Session = Depends(get_db)):
    """返回证券信息表中去重后的行业列表。

    数据源为 bds_symbol_info 表的 industry 字段（DISTINCT 去重，排除 NULL 与空串），
    按行业名称升序排序，供前端行业筛选下拉使用。无需分页。
    """
    rows = (
        db.query(SymbolInfo.industry)
        .filter(SymbolInfo.industry.isnot(None), SymbolInfo.industry != "")
        .distinct()
        .order_by(SymbolInfo.industry.asc())
        .all()
    )
    industries = [r[0] for r in rows]
    return {"industries": industries}


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
    """同步证券信息：东财 Excel + gm 串行执行（前端一个「同步证券信息」按钮）。

    返回 steps 数组，前端 executeSync 会拼成「同步成功：东财Excel:更新N条; gm证券信息:更新M条」。
    任一通道失败 ⇒ 500（detail 指明通道；已成功通道的写入仍生效，端点幂等、可重跑）。
    """
    try:
        return sync_symbol_info_all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync/mark-synced")
def mark_synced(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    db: Session = Depends(get_db),
):
    """标记某标的「当日已完成批量同步」：写 bds_symbol_info.sync_at = 服务器本地当日。

    由批量同步 notebook 在该 symbol 的全部目标表都返回 success / no_data 之后调用。
    幂等：重复调用只把 sync_at 刷新为当日，不回写其他字段。

    ⚠️ 本端点只写 sync_at 一个字段，与 upsert_symbol_info_excel_sql 的
    ON DUPLICATE KEY UPDATE 子句不重叠，故两者互不覆盖。

    返回值说明：
    - status: ok / error
    - symbol: 标的代码
    - sync_at: 写入的日期（YYYY-MM-DD）
    - updated: 受影响行数（0 表示库中无该 symbol）
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空", "updated": 0}
    today = date.today()
    updated = (
        db.query(SymbolInfo)
        .filter(SymbolInfo.symbol == symbol)
        .update({"sync_at": today}, synchronize_session=False)
    )
    db.commit()
    if updated == 0:
        return {"status": "error", "message": f"未找到标的：{symbol}", "updated": 0}
    return {
        "status": "ok",
        "symbol": symbol,
        "sync_at": today.strftime("%Y-%m-%d"),
        "updated": updated,
    }


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
        steps, _errors = upsert_index_history_sql()
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
    total = query.count()
    items = query.order_by(IndexHistory.trade_date.desc()).offset(offset).limit(limit).all()
    # 附加 sec_name 字段（从 Config.INDEX_CODE 查找，不存数据库）
    items_dict = []
    for item in items:
        d = item.to_dict()
        d["sec_name"] = Config.INDEX_CODE.get(item.symbol, {}).get("sec_name")
        items_dict.append(d)
    return {"items": items_dict, "total": total, "limit": limit, "offset": offset}


@router.get("/index-codes")
def list_index_codes():
    """返回应用配置的指数代码列表。

    数据源为 Config.INDEX_CODE 字典，确保前端下拉选项与后端指数配置
    保持一致，无需依赖数据库查询。每项包含：
    - code（指数代码）、sec_name（指数名称）
    - gm_supported：gm 是否支持该指数（`data_source='yfinance'` 的如 SP500 为 False）
      ⇒ 前端成分股同步的勾选项据此过滤，不把不支持 gm 的指数下发给回填任务。
    """
    index_codes = [
        {
            "code": code,
            "sec_name": info["sec_name"],
            "gm_supported": info.get("data_source") != "yfinance",
        }
        for code, info in Config.INDEX_CODE.items()
    ]
    return {"index_codes": index_codes}


@router.get("/index-constituents", response_model=PageResponse[IndexConstituentOut])
def list_index_constituents(
    index_code: Optional[str] = Query(default=None, description="指数代码精确匹配"),
    symbol: Optional[str] = Query(default=None, description="成分股代码精确匹配"),
    trade_date: Optional[date] = Query(default=None, description="交易日期 YYYY-MM-DD"),
    dedup: bool = Query(default=False, description="按成分股代码去重，每只保留交易日期最新的一条"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询指数成分股，支持指数代码精确匹配、成分股代码精确匹配和具体交易日期筛选。

    - dedup=False（默认）：返回全部符合条件明细，按 trade_date 降序（同日按 id 升序，翻页稳定）。
    - dedup=True：按成分股代码（symbol）去重，每只仅保留交易日期最新的一条；
      若同一交易日该股票同属多个指数，取 index_code 最小的那条（tie-break 确定性，每 symbol 恰一行）。
      total 为去重后的行数（COUNT(DISTINCT symbol)）。

    响应中每条记录附加 sec_name 字段（从 Config.INDEX_CODE 查找）。
    """
    # 公共过滤条件（两个分支共用）
    filters = []
    if index_code:
        filters.append(IndexConstituent.index_code == index_code)  # index_code 精确匹配
    if symbol:
        filters.append(IndexConstituent.symbol == symbol)  # symbol 精确匹配
    if trade_date:
        filters.append(IndexConstituent.trade_date == trade_date)  # 具体交易日期精确匹配

    result_items = []

    if dedup:
        # 去重分支：窗口函数按 symbol 分区、trade_date 降序取 rn=1
        # tie-break：同日跨多指数时取 index_code 最小的那条；id 兜底保证顺序确定
        rn = func.row_number().over(
            partition_by=IndexConstituent.symbol,
            order_by=(
                IndexConstituent.trade_date.desc(),
                IndexConstituent.index_code.asc(),
                IndexConstituent.id.asc(),
            ),
        ).label("rn")
        ranked = (
            select(
                IndexConstituent.index_code,
                IndexConstituent.symbol,
                IndexConstituent.weight,
                IndexConstituent.trade_date,
                rn,
            )
            .where(*filters)
            .subquery()
        )
        rows = db.execute(
            select(ranked)
            .where(ranked.c.rn == 1)
            .order_by(ranked.c.trade_date.desc(), ranked.c.index_code.asc(), ranked.c.symbol.asc())
            .offset(offset)
            .limit(limit)
        ).all()
        # 去重后 total = distinct symbol 数（口径 A 下与去重后的行数严格相等）
        total = db.query(func.count(func.distinct(IndexConstituent.symbol))).filter(*filters).scalar() or 0
        for row in rows:
            item_dict = {
                "index_code": row.index_code,
                "symbol": row.symbol,
                "weight": row.weight,
                "trade_date": row.trade_date.strftime("%Y-%m-%d") if row.trade_date else None,
            }
            # 附加 sec_name 字段（从 Config.INDEX_CODE 查找，不存数据库）
            item_dict["sec_name"] = Config.INDEX_CODE.get(row.index_code, {}).get("sec_name")
            result_items.append(item_dict)
    else:
        query = db.query(IndexConstituent)
        if filters:
            query = query.filter(*filters)
        # 按 trade_date 降序；同日期行按 id 升序兜底，避免顺序不稳导致翻页重复/漏行
        query = query.order_by(IndexConstituent.trade_date.desc(), IndexConstituent.id.asc())
        total = query.count()
        items = query.offset(offset).limit(limit).all()
        # 附加 sec_name 字段（从 Config.INDEX_CODE 查找，不存数据库）
        for item in items:
            item_dict = item.to_dict()
            item_dict["sec_name"] = Config.INDEX_CODE.get(item.index_code, {}).get("sec_name")
            result_items.append(item_dict)

    return {"items": result_items, "total": total, "limit": limit, "offset": offset}


@router.post("/sync/index-constituent", response_model=BdsSyncTaskOut)
def sync_index_constituent(req: IndexConstituentSyncRequest):
    """启动指数成分股同步/回填（后台线程异步执行），返回 task 供前端轮询进度。

    请求体（JSON）：
    - index_codes: 必填，本次要处理的指数代码清单（前端页面勾选）。后端硬校验：
      空清单 / 未知代码 / `data_source='yfinance'`（如 SP500）→ 400，不静默跳过。
    - start_date: 可选，回填起点 YYYY-MM-DD。不传则各指数从**核查水位 `checked_to`+1**
      增量（水位未初始化退回库内最新 trade_date+1，库中无该指数数据则从 listed_date 起）；
      传了则从该日回填到今天——传早于当前水位的日期属「历史补录」，水位不回退，
      若补出新的名单变化会在任务 errors 里提示「需重算该区间指数估值」。

    口径：变更键为成分股名单（symbol 集合），只在"名单变化"的交易日留存快照，不存每日权重；
    已存交易日自动跳过（断点续跑安全）。

    停止：POST /bds/stop/batch-sync?task_id=<id>。
    ⚠️ 与 /run/batch-sync 共用 bds_sync_task 与停止信号 ⇒ 二者全局互斥（有一方 running 则 409）。
    """
    db = next(get_db())
    try:
        # 1. 参数校验（先于建任务，避免非法入参在任务表里留下垃圾记录）
        try:
            plan = plan_index_constituent_backfill(req.index_codes, req.start_date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 2. 清理僵尸任务 + 互斥检查
        _cleanup_zombies(db, minutes=5)
        running = db.query(BdsSyncTask).filter(BdsSyncTask.status == "running").first()
        if running:
            raise HTTPException(status_code=409, detail=f"已有进行中的任务 #{running.id}，请等待完成后再启动")

        # 3. 建任务（scope=index_backfill，与批量同步的 scope 区分开）
        task = BdsSyncTask(
            scope="index_backfill",
            scope_params={
                "index_codes": [p["code"] for p in plan["per_index"]],
                "start_date": req.start_date.isoformat() if req.start_date else None,
            },
            tables=["index-constituent"],
            status="running",
            total_count=plan["total"],
            done_count=0,
            success_count=0,
            no_data_count=0,
            error_count=0,
            progress=0,
            errors=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # 4. 注册停止信号并启动后台线程（daemon=True：进程退出时自动终止）
        _stop_events[task.id] = threading.Event()
        t = threading.Thread(
            target=_run_constituent_thread,
            args=(task.id, [p["code"] for p in plan["per_index"]], req.start_date),
            daemon=True,
        )
        t.start()

        return task

    finally:
        db.close()


@router.get("/index-cum-returns")
def list_index_cum_returns(
    start_date: Optional[str] = Query(
        default=None, description="起始日期 YYYY-MM-DD，默认当前日期前10年"
    ),
    symbols: Optional[List[str]] = Query(
        default=None, description="限定指数代码（可多值）；不传返回 Config.INDEX_CODE 全部"
    ),
    db: Session = Depends(get_db),
):
    """查询指数累计收益率。

    逻辑说明：
    1. 查询 bds_index_history 表中 trade_date >= start_date 且 symbol 在
       Config.INDEX_CODE 字典键中的全部记录。symbols 多值参数可进一步子集过滤。
    2. 用 pandas 透视数据：行索引为 trade_date（升序），列名为通过
       Config.INDEX_CODE[symbol]['sec_name'] 映射的指数名称，值为 close 收盘价。
    3. 累计收益率计算：(df/df.iloc[0]-1)*100，保留两位小数。
    4. 空数据时返回 {"trade_dates": [], "series": {}}。
    5. 非空时构造响应：trade_dates 为日期字符串列表（YYYY-MM-DD），
       series 为 {指数名称: 累计收益率列表}，NaN 转 null。
    """
    # 默认起始日：当前日期前 10 年
    if start_date is None:
        start_date = (date.today() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")

    # symbol 白名单：symbols 有传就只看传入的，但必须在 INDEX_CODE 内
    allowed = list(Config.INDEX_CODE.keys())
    if symbols:
        allowed = [s for s in symbols if s in Config.INDEX_CODE]
        if not allowed:
            # 过滤后无合法 symbol，返回空结构
            return {
                "trade_dates": [],
                "series": {},
                "max_drawdown": {},
                "realtime_source": None,
            }

    # 查询满足条件的全部指数历史记录
    rows = (
        db.query(IndexHistory)
        .filter(
            IndexHistory.trade_date >= start_date,
            IndexHistory.symbol.in_(allowed),
        )
        .all()
    )

    # 空数据直接返回空结构
    if not rows:
        return {
            "trade_dates": [],
            "series": {},
            "max_drawdown": {},
            "realtime_source": None,
        }

    # 构造 DataFrame 并透视：行=trade_date，列=symbol，值=close
    df = pd.DataFrame(
        [
            {
                "trade_date": r.trade_date,
                "symbol": r.symbol,
                "close": float(r.close) if r.close is not None else np.nan,
            }
            for r in rows
        ]
    )
    df = df.pivot(index="trade_date", columns="symbol", values="close")
    # 按 trade_date 升序排序（在 rename 前排序，便于后续以 symbol 列名拼接实时行情）
    df = df.sort_index(ascending=True)

    # 优化：按列检查每个指数在今日是否有数据，仅对缺失的指数调用实时接口
    # 场景：部分指数历史数据已更新到今日（如 A 股已收盘入库），另一部分未更新（如 SP500 时差延迟）
    today = date.today()
    if today in df.index:
        # 历史 index 含今日：检查每列在今日是否有非 NaN 值
        today_row = df.loc[today]
        missing_symbols = [sym for sym in df.columns if pd.isna(today_row[sym])]
    else:
        # 历史 index 不含今日：所有指数都缺今日数据
        missing_symbols = list(df.columns)

    if missing_symbols:
        # 仅为缺失的指数调用实时接口，避免不必要的网络开销
        realtime_prices, realtime_source = fetch_realtime_index_prices(missing_symbols)
    else:
        # 所有指数今日数据均已入库，无需调用实时接口
        realtime_prices, realtime_source = {}, None

    # 拼接当日实时行情：此时 df 列名仍为 symbol，可直接用 realtime_prices 匹配
    if realtime_prices:
        # 构造今日实时行：每个 symbol 列填入实时价（缺失填 NaN）
        today_row = pd.Series(
            {sym: realtime_prices.get(sym, np.nan) for sym in df.columns},
            name=today,
        )
        # 历史末尾日期早于今日，必然走新增行分支（覆盖分支仅防御性保留）
        if today in df.index:
            for sym, price in realtime_prices.items():
                if sym in df.columns:
                    df.loc[today, sym] = price
        else:
            df = pd.concat([df, pd.DataFrame([today_row])])
            df = df.sort_index(ascending=True)

    # 列名由 symbol 映射为 sec_name（指数名称）
    df = df.rename(columns={k: v["sec_name"] for k, v in Config.INDEX_CODE.items()})

    # 累计净值：当日收盘价 / 首日收盘价
    cum = df / df.iloc[0]
    # 累计收益率：(累计净值 - 1) * 100，保留两位小数
    cum_df = ((cum - 1) * 100).round(2)

    # 最大回撤：当日累计净值 / 截至当日历史峰值 - 1，结果为非正值（≤0）
    running_max = cum.expanding().max()
    max_drawdown_df = ((cum / running_max - 1) * 100).round(2)

    # 日期字符串列表（YYYY-MM-DD 格式）
    trade_dates = [d.strftime("%Y-%m-%d") for d in cum_df.index]

    # 构造 series：{指数名称: 累计收益率列表}，NaN 转 None（JSON 序列化为 null）
    series = {
        col_name: [None if pd.isna(v) else float(v) for v in cum_df[col_name]]
        for col_name in cum_df.columns
    }

    # 构造 max_drawdown：{指数名称: 最大回撤列表}，NaN 转 None
    max_drawdown = {
        col_name: [None if pd.isna(v) else float(v) for v in max_drawdown_df[col_name]]
        for col_name in max_drawdown_df.columns
    }

    return {
        "trade_dates": trade_dates,
        "series": series,
        "max_drawdown": max_drawdown,
        "realtime_source": realtime_source,
    }


@router.post("/sync/fund-balance")
def sync_fund_balance(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步资产负债表数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps, errors = upsert_fund_balance_sql([symbol], force=force)
    return _build_sync_response(symbol, steps, errors)


def _query_fund_reports(model, db: Session, symbol, rpt_type, start_date, limit, offset):
    """通用财务报表查询（FundBalance/FundIncome/FundCashflow/FinanceDeriv 共用）。

    过滤规则：symbol 精确匹配、rpt_type 精确匹配、rpt_date 起始日。
    排序规则：rpt_date 降序，同 rpt_date 按 pub_date 降序。

    分页采用「先取 id 再回表」两步：
      1) 只查主键（索引覆盖扫描，不读 20+ 个字段），排序由
         k_*_rpt (rpt_date, pub_date) / k_*_type_rpt 索引支撑，offset 再深也不回表；
      2) 按主键回表取整行，再按第 1 步的 id 顺序还原。

    排序末尾补 id DESC 是**必要的**，不只是锦上添花：(rpt_date, pub_date) 存在大量
    并列（同一报告期同一披露日有数千行），只按这两列排序时并列块内的先后由执行计划
    决定 —— 加索引前走 filesort、加索引后走 backward index scan，两者给出的并列序
    不同，实测 offset=50,000 时返回的是两个完全不同的 20 行集合。补 id 后成为全序，
    「第 N 页」有唯一定义，翻页不会重复或漏行；且 InnoDB 二级索引末尾隐含主键，
    (rpt_date, pub_date) 索引的物理序正是 (rpt_date, pub_date, id)，加 id 不引入
    filesort（实测 1.5 ms，深 offset 38 ms）。

    为什么不回表后再 ORDER BY 一次：二次排序会丢掉第 1 步确定的顺序，也会破坏上一条
    说的全序保证。
    """
    _guard_list_offset(offset)
    query = db.query(model)
    if symbol:
        query = query.filter(model.symbol == symbol)
    if rpt_type is not None:
        query = query.filter(model.rpt_type == rpt_type)
    if start_date:
        query = query.filter(model.rpt_date >= start_date)
    total = query.count()

    id_rows = (
        query.with_entities(model.id)
        .order_by(model.rpt_date.desc(), model.pub_date.desc(), model.id.desc())
        .offset(offset).limit(limit).all()
    )
    ids = [r[0] for r in id_rows]

    by_id = {}
    if ids:
        by_id = {item.id: item for item in db.query(model).filter(model.id.in_(ids)).all()}
    items = [by_id[i] for i in ids if i in by_id]

    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/fund-balances", response_model=PageResponse[FundBalanceOut])
def list_fund_balances(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询资产负债表数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FundBalance, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/fund-income")
def sync_fund_income(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步利润表数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps, errors = upsert_fund_income_sql([symbol], force=force)
    return _build_sync_response(symbol, steps, errors)


@router.get("/fund-incomes", response_model=PageResponse[FundIncomeOut])
def list_fund_incomes(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询利润表数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FundIncome, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/fund-cashflow")
def sync_fund_cashflow(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步现金流量表数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps, errors = upsert_fund_cashflow_sql([symbol], force=force)
    return _build_sync_response(symbol, steps, errors)


@router.get("/fund-cashflows", response_model=PageResponse[FundCashflowOut])
def list_fund_cashflows(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询现金流量表数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FundCashflow, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/finance-deriv")
def sync_finance_deriv(
    symbol: str = Query(..., description="股票代码，精确匹配单个标的"),
    force: bool = Query(False, description="True=跳过「已是最新报告期」判断，强制按增量同步"),
):
    """同步财务指标数据，接收单个股票代码，获取并入库。

    默认先比对库内该标的 MAX(rpt_date) 与 bds_symbol_info.latest_rpt_date，
    已追平则判定数据已最新，不调 gm 直接返回 no_data；force=True 可绕过该判断。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps, errors = upsert_finance_deriv_sql([symbol], force=force)
    return _build_sync_response(symbol, steps, errors)


@router.get("/finance-derivs", response_model=PageResponse[FinanceDerivOut])
def list_finance_derivs(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    rpt_type: Optional[int] = Query(default=None, description="报表类型 1/6/9/12"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询财务指标数据，支持代码精确匹配、报表类型和报告日期起始日筛选。"""
    return _query_fund_reports(FinanceDeriv, db, symbol, rpt_type, start_date, limit, offset)


@router.post("/sync/daily-valuation")
def sync_daily_valuation(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步估值指标数据，接收单个股票代码，获取并入库。

    目标交易日取「交易日且已收盘(>=15:00)的当日，否则前一交易日」；
    库内已追平则跳过、返回 no_data（未调 gm）。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps, errors = upsert_daily_valuation_sql([symbol])
    return _build_sync_response(symbol, steps, errors, skip_label="交易日")


@router.get("/daily-valuations", response_model=PageResponse[DailyValuationOut])
def list_daily_valuations(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    start_date: Optional[date] = Query(default=None, description="交易日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询估值指标数据，支持代码精确匹配和交易日期范围筛选。

    排序规则：trade_date 降序。
    symbol 使用精确匹配（走 uk(symbol, trade_date) 复合索引最左前缀，~3ms），
    原 LIKE '%xxx%' 前导通配无法走 B+Tree 索引，120 万行下需全表扫描 ~7s。

    ⚠️ 无筛选时 total 不数全表（643 万行 COUNT(*) 需 5~8s，占首屏 87%），改读
    bds_table_stat 缓存（批量同步收尾重算，见 services/table_stat.py）；缓存缺失/过旧
    则回落 information_schema 估算并置 total_is_estimated=true。有筛选走精确 COUNT(*)。
    ⚠️ offset 受 MAX_LIST_OFFSET 护栏限制（深翻页会退化为全表扫描 + filesort）。
    """
    _guard_list_offset(offset)
    query = db.query(DailyValuation)
    if symbol:
        query = query.filter(DailyValuation.symbol == symbol)
    if start_date:
        query = query.filter(DailyValuation.trade_date >= start_date)
    total, total_estimated = resolve_total(db, query, DailyValuation, bool(symbol or start_date))
    items = (
        query.order_by(DailyValuation.trade_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {
        "items": [item.to_dict() for item in items],
        "total": total,
        "total_is_estimated": total_estimated,
        "limit": limit,
        "offset": offset,
    }


@router.post("/sync/daily-mktvalue")
def sync_daily_mktvalue(symbol: str = Query(..., description="股票代码，精确匹配单个标的")):
    """同步每日市值数据，接收单个股票代码，获取并入库。

    目标交易日取「交易日且已收盘(>=15:00)的当日，否则前一交易日」；
    库内已追平则跳过、返回 no_data（未调 gm）。
    """
    if not symbol:
        return {"status": "error", "message": "symbol 不能为空"}
    steps, errors = upsert_daily_mktvalue_sql([symbol])
    return _build_sync_response(symbol, steps, errors, skip_label="交易日")


@router.get("/daily-mktvalues", response_model=PageResponse[DailyMktvalueOut])
def list_daily_mktvalues(
    symbol: Optional[str] = Query(default=None, description="股票代码精确匹配"),
    start_date: Optional[date] = Query(default=None, description="交易日期起始日"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询每日市值数据，支持代码精确匹配和交易日期范围筛选。

    排序规则：trade_date 降序。
    symbol 使用精确匹配（走 uk(symbol, trade_date) 复合索引最左前缀，~3ms）。

    无筛选时 total 同 /daily-valuations：读 bds_table_stat 缓存，不数全表。
    ⚠️ offset 同受 MAX_LIST_OFFSET 护栏限制。
    """
    _guard_list_offset(offset)
    query = db.query(DailyMktvalue)
    if symbol:
        query = query.filter(DailyMktvalue.symbol == symbol)
    if start_date:
        query = query.filter(DailyMktvalue.trade_date >= start_date)
    total, total_estimated = resolve_total(db, query, DailyMktvalue, bool(symbol or start_date))
    items = (
        query.order_by(DailyMktvalue.trade_date.desc())
        .offset(offset).limit(limit).all()
    )
    return {
        "items": [item.to_dict() for item in items],
        "total": total,
        "total_is_estimated": total_estimated,
        "limit": limit,
        "offset": offset,
    }


@router.post("/refresh-table-stat")
def refresh_table_stat_endpoint(
    tables: Optional[List[str]] = Query(
        None, description="同步目标名列表（如 daily-valuation）；留空=全部登记表"
    ),
    db: Session = Depends(get_db),
):
    """手工重算列表页总数缓存（bds_table_stat）。

    正常由批量同步收尾自动刷新；本端点用于手工校准，例如批量同步没跑而数据已变，
    或想让「共 N 条」立刻变准（2026-10-16 建立的 L1-b 计数表）。

    ⚠️ 一次全表精确 COUNT(*)：600 万行级每表 5~8s，串行执行。
    返回 {表名: 精确行数}，如 {"bds_daily_valuation": 6442891}。
    """
    if tables:
        unknown = [t for t in tables if t not in STAT_TABLES]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"未知同步目标名：{unknown}，可选：{list(STAT_TABLES)}",
            )
    return {"status": "ok", "tables": refresh_table_stat(db, targets=tables)}


@router.get("/economic-indicators", response_model=PageResponse[EconomicIndicatorOut])
def list_economic_indicators(
    indicator_code: Optional[str] = Query(default=None, description="指标代码精确匹配"),
    category: Optional[str] = Query(default=None, description="类别精确匹配"),
    country: Optional[str] = Query(default=None, description="国别精确匹配"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    end_date: Optional[date] = Query(default=None, description="报告日期结束日"),
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询经济指标数据，支持指标代码/类别/国别精确匹配和报告日期范围筛选。

    排序规则：report_date 降序，同 report_date 按 indicator_code 升序。
    """
    query = db.query(EconomicIndicator)
    # indicator_code 精确匹配
    if indicator_code:
        query = query.filter(EconomicIndicator.indicator_code == indicator_code)
    # category 精确匹配
    if category:
        query = query.filter(EconomicIndicator.category == category)
    # country 精确匹配
    if country:
        query = query.filter(EconomicIndicator.country == country)
    # 报告日期范围过滤
    if start_date:
        query = query.filter(EconomicIndicator.report_date >= start_date)
    if end_date:
        query = query.filter(EconomicIndicator.report_date <= end_date)
    total = query.count()
    items = (
        query.order_by(EconomicIndicator.report_date.desc(), EconomicIndicator.indicator_code.asc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/economic-indicator-codes")
def list_economic_indicator_codes():
    """返回经济指标配置列表（数据源 Config.ECONOMIC_INDICATORS，无数据库查询）。

    每项包含 indicator_code、indicator_name、category、country、unit、frequency，
    供前端下拉选项使用。
    """
    indicators = [
        {
            "indicator_code": code,
            "indicator_name": info["name"],
            "indicator_short_name": info["short_name"],
            "category": info["category"],
            "country": info["country"],
            "unit": info["unit"],
            "frequency": info["frequency"],
        }
        for code, info in Config.ECONOMIC_INDICATORS.items()
    ]
    return indicators


@router.post("/sync/economic-indicator-wscn")
def sync_economic_indicator_wscn():
    """通过华尔街见闻日历接口同步经济指标数据。

    数据源：https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas
    覆盖 Config.WSCN_INDICATOR_MAP 中 11 个可映射的美国指标，
    补充 FRED 缺失的 forecast/importance/revised/pub_date 字段。

    返回值说明：
    - status: success/no_data/error
    - message: 同步结果描述信息
    - count: 插入/更新条数（-1 表示失败）
    """
    count = upsert_economic_indicator_from_wscn_sql()
    if count == -1:
        return {"status": "error", "message": "wscn 同步失败，请查看后端日志", "count": count}
    if count == 0:
        return {"status": "no_data", "message": "wscn 无新数据可导入", "count": count}
    return {"status": "success", "message": f"wscn 同步完成，更新 {count} 条", "count": count}


@router.get("/gold-reserve-countries")
def list_gold_reserve_countries():
    """返回央行黄金储备主要持有国列表。

    数据源为 Config.GOLD_RESERVE_COUNTRIES 字典，确保前端下拉选项与后端配置
    保持一致，无需依赖数据库查询。每项包含 country_code、country_name、imf_code。
    """
    countries = [
        {"country_code": code, "country_name": info["country_name"], "imf_code": info["imf_code"]}
        for code, info in Config.GOLD_RESERVE_COUNTRIES.items()
    ]
    return {"countries": countries}


@router.get("/gold-reserves", response_model=PageResponse[GoldReserveOut])
def list_gold_reserves(
    country_code: Optional[List[str]] = Query(None, description="国家代码多选精确匹配"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询黄金储备数据，支持国家多选和日期范围筛选。

    按 rpt_date 降序、country_code 升序排序。
    """
    query = db.query(GoldReserve)
    if country_code:
        query = query.filter(GoldReserve.country_code.in_(country_code))
    if start_date:
        query = query.filter(GoldReserve.rpt_date >= start_date)
    if end_date:
        query = query.filter(GoldReserve.rpt_date <= end_date)
    total = query.count()
    items = query.order_by(GoldReserve.rpt_date.desc(), GoldReserve.country_code.asc()).offset(offset).limit(limit).all()
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/gold-reserve")
def sync_gold_reserve(country_code: str = Query(..., description="国家代码，精确匹配单个国家")):
    """同步单个国家央行黄金储备数据。

    返回值说明：
    - status: success/no_data/error
    - country_code: 同步的国家代码
    - count: 更新条数（-1 表示失败）
    - message: 同步结果说明，失败时含代理配置指引
    """
    if country_code not in Config.GOLD_RESERVE_COUNTRIES:
        raise HTTPException(status_code=400, detail=f"未知国家代码：{country_code}")
    try:
        count = upsert_gold_reserve_sql(country_code)
        if count == -1:
            # 同步失败：通常是 IMF SDMX API 不可达（DNS/SSL），提示用户配置代理
            return {
                "status": "error",
                "country_code": country_code,
                "count": -1,
                "message": (
                    f"同步失败：{country_code}。IMF SDMX API 不可达，"
                    "请在 server_fast/.env 中配置 HTTPS_PROXY 后重试"
                ),
            }
        if count == 0:
            return {"status": "no_data", "country_code": country_code, "count": 0, "message": f"无数据可导入：{country_code}"}
        return {"status": "success", "country_code": country_code, "count": count, "message": f"同步完成：{country_code}，更新 {count} 条"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync/gold-reserves-all")
def sync_gold_reserves_all():
    """全量同步所有国家央行黄金储备数据。

    单国家失败不中断，返回各国同步结果字典。
    若全部失败，通常为网络无法访问 IMF API，需在 .env 配置 HTTPS_PROXY。
    """
    try:
        steps = upsert_all_gold_reserves_sql()
        # 全部失败时附加提示
        all_failed = all(v == -1 for v in steps.values())
        message = "全量同步完成"
        if all_failed:
            message = "全部国家同步失败，IMF SDMX API 不可达，请在 server_fast/.env 中配置 HTTPS_PROXY"
        return {"status": "ok", "steps": steps, "message": message}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/daily-indicator-codes")
def list_daily_indicator_codes():
    """返回美债收益率指标配置列表（数据源 Config.DAILY_INDICATORS，无数据库查询）。

    每项包含 indicator_code、indicator_name、indicator_short_name、category、
    country、unit、frequency，供前端下拉选项使用。
    """
    indicators = [
        {
            "indicator_code": code,
            "indicator_name": info["name"],
            "indicator_short_name": info["short_name"],
            "category": info["category"],
            "country": info["country"],
            "unit": info["unit"],
            "frequency": info["frequency"],
        }
        for code, info in Config.DAILY_INDICATORS.items()
    ]
    return indicators


@router.get("/daily-indicators", response_model=PageResponse[DailyIndicatorOut])
def list_daily_indicators(
    indicator_code: Optional[List[str]] = Query(default=None, description="指标代码多选 IN 匹配"),
    start_date: Optional[date] = Query(default=None, description="报告日期起始日"),
    end_date: Optional[date] = Query(default=None, description="报告日期结束日"),
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """查询美债收益率指标数据，支持指标代码多选 IN、报告日期范围筛选与分页。

    排序规则：report_date 降序，同 report_date 按 indicator_code 升序。
    """
    query = db.query(DailyIndicator)
    # indicator_code 多选 IN 过滤
    if indicator_code:
        query = query.filter(DailyIndicator.indicator_code.in_(indicator_code))
    # 报告日期范围过滤
    if start_date:
        query = query.filter(DailyIndicator.report_date >= start_date)
    if end_date:
        query = query.filter(DailyIndicator.report_date <= end_date)
    total = query.count()
    items = (
        query.order_by(DailyIndicator.report_date.desc(), DailyIndicator.indicator_code.asc())
        .offset(offset).limit(limit).all()
    )
    return {"items": [item.to_dict() for item in items], "total": total, "limit": limit, "offset": offset}


@router.post("/sync/daily-indicator")
def sync_daily_indicator(indicator_code: str = Query(..., description="指标代码，精确匹配单个指标")):
    """同步单个日频经济指标数据。

    返回值说明：
    - status: success/no_data/error
    - message: 同步结果描述信息
    - indicator_code: 指标代码
    - count: 插入/更新条数（-1 表示失败）
    """
    if not indicator_code:
        return {"status": "error", "message": "indicator_code 不能为空",
                "indicator_code": indicator_code, "count": -1}
    if indicator_code not in Config.DAILY_INDICATORS:
        return {"status": "error", "message": f"未知收益率指标代码：{indicator_code}",
                "indicator_code": indicator_code, "count": -1}
    count = upsert_daily_indicator_sql(indicator_code)
    if count == -1:
        return {"status": "error", "message": f"同步失败：{indicator_code}",
                "indicator_code": indicator_code, "count": -1}
    if count == 0:
        return {"status": "no_data", "message": f"无数据可导入：{indicator_code}",
                "indicator_code": indicator_code, "count": 0}
    return {"status": "success", "message": f"同步完成：{indicator_code}，更新 {count} 条",
            "indicator_code": indicator_code, "count": count}


@router.post("/sync/daily-indicators-all")
def sync_daily_indicators_all():
    """全量同步所有美债收益率指标数据。

    遍历 Config.DAILY_INDICATORS 全部指标（当前 6 个，含 2 个信用利差），逐个调用同步函数。
    单指标失败不中断，返回 steps 包含每个指标的结果。

    返回值说明：
    - status: success
    - message: 同步结果描述信息
    - steps: {indicator_code: count, ...} 各指标同步条数字典
    """
    try:
        steps = upsert_all_daily_indicators_sql()
        return {"status": "success", "message": "美债收益率指标全量同步完成", "steps": steps}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# 一键同步（基础数据 + 宏观数据）：七段固定顺序执行，复用 bds_sync_task 轮询进度
# 方案：docs/基础数据并入同步卡片方案_2026-10-22.md
#       docs/宏观同步合并一键顺序执行方案_2026-10-21.md（初版，仅宏观三段）
#
# 与各单段端点（/sync/trade-date、/sync/symbol-info、/sync/index-history、
# /sync/economic-indicator-wscn、/sync/daily-indicators-all、/sync/gold-reserves-all）
# 的关系：那些端点**原样保留**（各自列表页仍在用），这里只是把它们内部的
# **单项函数**按固定顺序再串一遍，并逐项写进度。
#
# ⚠️ scope 字符串仍是 "macro_sync"（名字已不准，但不能改）：历史任务 #144/#145/#148
#    都靠它被前端卡片接管，改了历史任务就再也刷不出来。
# ============================================================================

# 固定执行顺序：基础在前（下游依赖）+ 快 → 慢
# 实测耗时（2026-10-22 盘前）：trade_date 0.17s / symbol_info 8.5s / index_history 0.22s
#                             option_market 3 组约 5s（2026-10-26 实测，6 合约）
#                             wscn 约 15s / daily 约 13s / gold 20 国约 5.5min（每国 16.4s）
SYNC_STAGE_ORDER = ["trade_date", "symbol_info", "index_history", "option_market", "wscn", "daily", "gold"]
SYNC_STAGE_NAMES = {
    "trade_date": "交易日历",
    "symbol_info": "证券信息",
    "index_history": "指数行情",
    "option_market": "期权行情",
    "wscn": "经济指标（华尔街见闻）",
    "daily": "美债收益率（FRED）",
    "gold": "黄金储备（IMF）",
}

# 前端勾选分组用：基础数据（证券/行情/日历） vs 宏观数据
SYNC_STAGE_GROUPS = {
    "trade_date": "basic",
    "symbol_info": "basic",
    "index_history": "basic",
    "option_market": "basic",
    "wscn": "macro",
    "daily": "macro",
    "gold": "macro",
}


def _sync_stage_items(stage: str) -> list:
    """该段的可计数条目 [(key, 显示名)]。

    - trade_date：原子操作（一年只在跨年时真写库），整体 1 项
    - symbol_info：东财 Excel / gm 两条通道各 1 项（与 sync_symbol_info_all 的步骤划分一致）
    - index_history：按 Config.INDEX_CODE 逐指数
    - option_market：按「品种 × 到期月」逐组（= 一次接口请求），与 /option-market/sync 的分组口径一致；
      清单为空时给一个 `__no_config__` 伪项，让失败明细能说清原因，而不是 0 项静默通过
    - wscn：service 内部按自然月分批（首次全量 141 批起）**不可切分** ⇒ 整体 1 项
    - daily / gold：按配置逐项，与前端 store 列出的条目同源
    """
    if stage == "trade_date":
        return [("trade_date", "交易日历")]
    if stage == "symbol_info":
        return [("excel", "东财Excel"), ("gm", "gm证券信息")]
    if stage == "index_history":
        return [(code, info.get("sec_name") or code) for code, info in Config.INDEX_CODE.items()]
    if stage == "option_market":
        groups = option_market.build_sync_plan()["groups"]
        if not groups:
            return [("__no_config__", "未配置合约")]
        return [
            (f"{g['option_name']}|{g['end_month']}", f"{g['option_name']}/{g['end_month'] or 'ALL'}")
            for g in groups
        ]
    if stage == "wscn":
        return [("wscn", "全部指标")]
    if stage == "daily":
        return [(code, info.get("short_name") or code) for code, info in Config.DAILY_INDICATORS.items()]
    if stage == "gold":
        return [(code, info.get("country_name") or code) for code, info in Config.GOLD_RESERVE_COUNTRIES.items()]
    raise ValueError(f"未知同步段: {stage}")


def _run_sync_item(stage: str, key: str) -> int:
    """执行单项，返回条数（-1=失败）。

    各 service 单项函数内部已 try/except（宏观三个返回 -1，index_history 的
    sync_one_index 同样返回 -1）；这里再兜一层：任何意外异常都只算该项失败，
    不得中断整段或整个任务。

    ⚠️ symbol_info 刻意**不走** sync_symbol_info_all()：它的语义是「任一通道失败即抛」，
    放进一键任务会把东财已写入的五千余条一起算失败。这里两条通道各自计项、各自兜异常。
    """
    try:
        if stage == "trade_date":
            return insert_trade_date_em_sql()
        if stage == "symbol_info":
            if key == "excel":
                return upsert_symbol_info_excel_sql()
            return upsert_symbol_info_gm_sql()
        if stage == "index_history":
            info = Config.INDEX_CODE.get(key)
            if not info:
                return -1
            return sync_one_index(key, info)
        if stage == "option_market":
            if key == "__no_config__":
                return -1
            option_name, _, end_month = key.partition("|")
            return option_market.sync_one_group(option_name, end_month)
        if stage == "wscn":
            return upsert_economic_indicator_from_wscn_sql()
        if stage == "daily":
            return upsert_daily_indicator_sql(key)
        if stage == "gold":
            return upsert_gold_reserve_sql(key)
        return -1
    except Exception as e:
        print(f"[sync] {stage}/{key} 异常: {type(e).__name__}: {e}")
        return -1


def _run_sync_thread(task_id: int, stages: list):
    """后台线程：按 stages 固定顺序逐项执行，每完成一项写一次进度。

    与 HTTP 请求完全隔离（自建 SessionLocal）；进度更新另开短 session 并每项 commit，
    既避免长事务，也刷新 update_time 防止 5 分钟僵尸清理把长任务误标 failed。
    """
    db = SessionLocal()
    task = db.query(BdsSyncTask).get(task_id)
    errors = []
    try:
        task.status = "running"
        task.start_time = datetime.now()
        db.commit()

        stop_event = _stop_events.get(task_id)
        plan = [(s, _sync_stage_items(s)) for s in stages]
        total = sum(len(items) for _, items in plan)

        # 每段的统计包：result.stages 直接给前端渲染段摘要表
        stats = {}
        for s, items in plan:
            stats[s] = {"key": s, "name": SYNC_STAGE_NAMES[s], "group": SYNC_STAGE_GROUPS[s],
                        "total": len(items), "done": 0, "ok": 0, "no_data": 0, "error": 0, "rows": 0}

        done = ok = no_data = err = 0

        def flush(current_stage):
            s = SessionLocal()
            try:
                t = s.query(BdsSyncTask).get(task_id)
                t.done_count = done
                t.success_count = ok
                t.no_data_count = no_data
                t.error_count = err
                t.progress = round(done / total * 100, 2) if total else 100
                t.result = {"stages": [stats[k] for k in stages], "stage": current_stage}
                s.commit()
            finally:
                s.close()

        flush(stages[0])

        for stage, items in plan:
            if stop_event and stop_event.is_set():
                break
            for key, _name in items:
                if stop_event and stop_event.is_set():
                    break
                count = _run_sync_item(stage, key)
                done += 1
                st = stats[stage]
                st["done"] += 1
                if count > 0:
                    ok += 1
                    st["ok"] += 1
                    st["rows"] += count
                elif count == 0:
                    no_data += 1
                    st["no_data"] += 1
                else:
                    err += 1
                    st["error"] += 1
                    # option_market 的伪项给明确原因，别让「清单为空」看起来像接口挂了
                    msg = ("期权同步清单为空，请先在「期权同步清单」里配置要同步的合约"
                           if key == "__no_config__" else "取数失败（count=-1，详见后端日志）")
                    errors.append({
                        "symbol": key,
                        "table": SYNC_STAGE_NAMES[stage],
                        "message": msg,
                    })
                print(f"[sync #{task_id}] {stage}/{key} -> {count} ({done}/{total})")
                flush(stage)

        stopped = bool(stop_event and stop_event.is_set())
        task.status = "stopped" if stopped else ("failed" if err else "done")
        task.done_count = done
        task.success_count = ok
        task.no_data_count = no_data
        task.error_count = err
        task.progress = round(done / total * 100, 2) if total else 100
        task.errors = errors or None
        task.result = {"stages": [stats[k] for k in stages], "stage": None, "stopped": stopped}
        task.end_time = datetime.now()
        db.commit()
        print(f"[sync #{task_id}] {task.status}: {done}/{total} 成功={ok} 无数据={no_data} 失败={err}")
    except Exception as e:
        errors.append({"symbol": "", "table": "", "message": f"线程异常: {type(e).__name__}: {e}"})
        task.status = "failed"
        task.errors = errors
        task.end_time = datetime.now()
        db.commit()
        print(f"[sync #{task_id}] 失败: {e}")
    finally:
        _stop_events.pop(task_id, None)
        db.close()


@router.post("/sync/macro", response_model=BdsSyncTaskOut)
def sync_macro(req: MacroSyncRequest):
    """一键同步（后台线程异步执行，返回 task 供轮询进度）。

    请求体：`{"stages": ["symbol_info", "index_history", "wscn", "daily"]}`，只跑传入的段。
    **执行顺序固定 trade_date → symbol_info → index_history → wscn → daily → gold**，
    不按入参数组顺序（基础数据是下游依赖，且整体快 → 慢，中途停止的代价最小）。

    进度：`total_count` = 各段条目数之和（trade_date 1 + symbol_info 2 + index_history 每指数 1
    + wscn 1 + daily 每指标 1 + gold 每国 1），每完成一项 `done_count` +1。
    `result.stages` 存各段 total/done/ok/no_data/error/rows（含 group 供前端分组），
    `result.stage` 存当前段 —— wscn 首轮全量时进度条会停在该段，靠它显示"正在跑哪一段"。

    计数口径：`count>0`→success / `count==0`→no_data / `count==-1`→error（同时写 errors 明细）。

    停止：`POST /bds/stop/batch-sync?task_id=<id>`，项间生效（当前项是 HTTP 请求，最坏再等一国约 16s）。
    ⚠️ 与 /run/batch-sync、/sync/index-constituent 共用 bds_sync_task ⇒ **全局互斥**（有 running 则 409）。
    """
    db = next(get_db())
    try:
        picked = set(req.stages or [])
        unknown = picked - set(SYNC_STAGE_ORDER)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"非法 stages: {sorted(unknown)}（应为 {SYNC_STAGE_ORDER} 的子集）",
            )
        # 固定顺序，忽略入参数组顺序
        stages = [s for s in SYNC_STAGE_ORDER if s in picked]
        if not stages:
            raise HTTPException(status_code=400, detail="stages 不能为空（至少选一段）")

        # 清理僵尸任务 + 互斥检查（与批量同步/成分股同步同一口径）
        _cleanup_zombies(db, minutes=5)
        running = db.query(BdsSyncTask).filter(BdsSyncTask.status == "running").first()
        if running:
            raise HTTPException(status_code=409, detail=f"已有进行中的任务 #{running.id}，请等待完成后再启动")

        total = sum(len(_sync_stage_items(s)) for s in stages)
        task = BdsSyncTask(
            scope="macro_sync",
            scope_params={"stages": stages},
            tables=stages,
            mark_synced=0,
            trading_status="trading",
            max_symbols=0,
            status="pending",
            total_count=total,
            done_count=0,
            success_count=0,
            no_data_count=0,
            error_count=0,
            progress=0,
            errors=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # 注册停止信号（复用 /stop/batch-sync）
        _stop_events[task.id] = threading.Event()
        threading.Thread(target=_run_sync_thread, args=(task.id, stages), daemon=True).start()
        return task
    finally:
        db.close()



# ============================================================================
# 批量同步：后台线程异步执行，进度实时写 bds_sync_task 表
# ============================================================================

def _run_batch_sync_thread(task_id: int, req: BatchSyncRequest):
    """后台线程：执行批量同步并实时写进度到 DB。

    与 HTTP 调用完全隔离：本线程内部创建独立 SessionLocal，
    不复用请求的 db 依赖。异常时将 task 状态置为 failed 并记录错误。
    """
    db = SessionLocal()
    task = db.query(BdsSyncTask).get(task_id)
    tables = req.tables or TABLES
    errors = []

    try:
        # 1. 解析 symbol 列表
        syms = resolve_symbols(
            req.scope,
            explicit=req.symbols,
            industry=req.industry,
            index_code=req.index_codes,  # 多选清单；resolve_symbols 兼容 str / list，list 时取成分合集
            max_symbols=req.max_symbols,
            exclude_synced_since=req.exclude_synced_since,
            trading_status=req.trading_status,
        )

        tasks = [(s, t) for s in syms for t in tables]
        total_count = len(tasks)
        print(f'[batch-sync #{task_id}] 任务总数 {total_count} (symbols={len(syms)}, tables={len(tables)})')

        # 2. 更新 task 状态为 running
        task.status = "running"
        task.total_count = total_count
        task.start_time = date.today()  # DB 列是 DateTime，但这里只需要日期精度
        db.commit()

        # 3. 定义进度回调
        def on_progress(symbol, table, status, done_count, total):
            # 每次回调都开独立 session，避免长事务
            s = SessionLocal()
            try:
                t = s.query(BdsSyncTask).get(task_id)
                t.done_count = done_count
                if status == "success":
                    t.success_count += 1
                elif status == "no_data":
                    t.no_data_count += 1
                else:
                    t.error_count += 1
                    t.errors = (t.errors or []) + [{"symbol": symbol, "table": table, "message": ""}]
                t.progress = round(done_count / total * 100, 2) if total else 100
                s.commit()
            finally:
                s.close()

        # 4. 执行同步（复用 sync_runner，限速 + 重试 + 连续失败退避 + 当日标记 全在 run_tasks 内）
        stop_event = _stop_events.get(task_id)
        results = run_tasks(
            tasks,
            mark_synced_when_done=req.mark_synced,
            on_progress=on_progress,
            stop_event=stop_event,
        )

        # 5. 收集失败明细（error + fatal 都收）
        #    终态只看 fatal_escalated：零星 fatal 属「疑似断连但未熔断」，归入 done + errors，
        #    对应动作是「只补失败项」；只有连续 N 笔触发的熔断才是 failed（修终端整体重跑）。
        fatal_hit = False
        for r in results:
            if r["status"] == "fatal":
                if r.get("fatal_escalated"):
                    fatal_hit = True
                    errors.append({"symbol": r["symbol"], "table": r["table"],
                                   "message": f"[致命] gm 终端不可用，已熔断中止：{r.get('message', '')}"})
                else:
                    errors.append({"symbol": r["symbol"], "table": r["table"],
                                   "message": f"[gm 异常] 疑似终端断连，未达熔断阈值：{r.get('message', '')}"})
            elif r["status"] == "error":
                errors.append({"symbol": r["symbol"], "table": r["table"], "message": r.get("message", "")})

        # 6. 结束终态判断（优先级：gm 断连 > 用户停止 > 正常完成）
        if fatal_hit:
            task.status = "failed"
        elif stop_event is not None and stop_event.is_set():
            task.status = "stopped"
        else:
            task.status = "done"
        task.errors = errors or None
        task.end_time = date.today()
        db.commit()
        print(f'[batch-sync #{task_id}] {task.status}：success={task.success_count}, no_data={task.no_data_count}, error={task.error_count}')

        # 7. 重算列表页总数缓存（bds_table_stat）：best-effort，失败不影响已成型的任务终态。
        #    只重算本次批量同步覆盖到的表（两张 6M 日频表各 5~8s，跑批内可忽略）。
        #    放在终态 commit 之后，前端轮询不会被这段耗时拖住。
        try:
            refreshed = refresh_table_stat(db, targets=tables)
            if refreshed:
                print(f'[batch-sync #{task_id}] 已重算总数缓存: {refreshed}')
        except Exception as e:
            db.rollback()
            print(f'[batch-sync #{task_id}] 重算总数缓存失败（不影响任务）: {type(e).__name__}: {e}')

    except Exception as e:
        # 兜底：线程异常也要把 task 状态置为 failed
        errors.append({"symbol": "", "table": "", "message": f"线程异常: {type(e).__name__}: {e}"})
        task.status = "failed"
        task.errors = errors
        task.end_time = date.today()
        db.commit()
        print(f'[batch-sync #{task_id}] 失败: {e}')
    finally:
        # 清理停止信号，避免内存泄漏
        _stop_events.pop(task_id, None)


# ============================================================================
# 指数成分股同步 / 历史回填：后台线程异步执行，进度实时写 bds_sync_task 表
# ============================================================================

def _run_constituent_thread(task_id: int, index_codes: list, start_date):
    """后台线程：执行指数成分股同步/回填并实时写进度到 DB。

    与 HTTP 调用完全隔离：线程内部自建 SessionLocal，不复用请求 db。
    复用 bds_sync_task 与 _stop_events ⇒ 与批量同步全局互斥。
    """
    db = SessionLocal()
    task = db.query(BdsSyncTask).get(task_id)
    errors = []

    try:
        task.status = "running"
        task.start_time = datetime.now()
        db.commit()

        # 进度回调：每次开独立 session，避免长事务；每轮 commit 刷新 update_time，
        # 防止 5 分钟僵尸清理把仍在跑的长任务误标为 failed
        def on_progress(index_code, trade_date, done_count, total, counts):
            s = SessionLocal()
            try:
                t = s.query(BdsSyncTask).get(task_id)
                t.done_count = done_count
                t.progress = round(done_count / total * 100, 2) if total else 100
                # 计数必须逐日实时落库：全量回填要跑一两个小时，若只在结束时写，
                # 前端看到的会是「已完成 N 天」而 saved/skipped/失败 全是 0，像卡死。
                t.success_count = counts.get("saved", 0)
                t.skipped_count = counts.get("skipped", 0)
                t.dirty_count = counts.get("dirty", 0)
                t.no_data_count = counts.get("no_data", 0)
                t.error_count = counts.get("failed", 0)
                s.commit()
            finally:
                s.close()

        stop_event = _stop_events.get(task_id)
        counters = upsert_index_constituent_sql(
            index_codes,
            start_date=start_date,
            on_progress=on_progress,
            stop_event=stop_event,
        )

        # 计数映射：saved=留存快照数（success）、skipped=名单未变/已存跳过、
        # dirty=被拒存的伪快照日、no_data=gm 空返回、failed=失败、aborted=提前中止的指数个数
        task.success_count = sum(c.get("saved", 0) for c in counters.values())
        task.skipped_count = sum(c.get("skipped", 0) for c in counters.values())
        task.dirty_count = sum(c.get("dirty", 0) for c in counters.values())
        task.no_data_count = sum(c.get("no_data", 0) for c in counters.values())
        task.error_count = sum(c.get("failed", 0) for c in counters.values())
        task.aborted_count = sum(1 for c in counters.values() if c.get("aborted"))
        gm_fatal_hit = False
        for code, c in counters.items():
            if c.get("gm_fatal"):
                gm_fatal_hit = True
                errors.append({
                    "symbol": code,
                    "table": "index-constituent",
                    "message": "[致命] gm 终端不可用，回填已中止",
                })
            elif c.get("aborted"):
                errors.append({
                    "symbol": code,
                    "table": "index-constituent",
                    "message": "提前中止（被停止，或连续调用失败超限）",
                })
            # 历史补录改动了成分 ⇒ 该区间的指数估值必须重算，必须显式提示（不能靠记性）
            if c.get("reval_from"):
                errors.append({
                    "symbol": code,
                    "table": "index-valuation",
                    "message": (f"历史成分已变更（{c['reval_from']} ~ {c['reval_to']}，"
                                f"新增 {c.get('saved', 0)} 个快照），需重算该区间的指数估值"
                                f"（批量同步页 →「指数估值重算」卡片，起止日期填该区间）"),
                })

        # 终态：gm 断连 > 用户停止 > 全部 error 无 success > 正常完成
        if gm_fatal_hit:
            task.status = "failed"
        elif stop_event is not None and stop_event.is_set():
            task.status = "stopped"
        elif task.error_count and not task.success_count:
            task.status = "failed"
        else:
            task.status = "done"
        task.errors = errors or None
        task.end_time = datetime.now()
        db.commit()
        print(f'[index-constituent #{task_id}] {task.status}：saved={task.success_count}, '
              f'skipped={task.skipped_count}, dirty={task.dirty_count}, '
              f'no_data={task.no_data_count}, failed={task.error_count}, '
              f'aborted={task.aborted_count}')

    except Exception as e:
        # 兜底：线程异常也要把 task 状态置为 failed
        errors.append({"symbol": "", "table": "index-constituent",
                       "message": f"线程异常: {type(e).__name__}: {e}"})
        task.status = "failed"
        task.errors = errors
        task.end_time = datetime.now()
        db.commit()
        print(f'[index-constituent #{task_id}] 失败: {e}')
    finally:
        _stop_events.pop(task_id, None)
        db.close()


def _run_valuation_thread(task_id: int, plan: dict, start_date=None,
                          end_date=None, with_median: bool = True):
    """后台线程：逐指数重算指数估值，实时写进度，结束时把审计结果写进 task.result。

    与 `_run_constituent_thread` 同构：自建 SessionLocal、复用 `_stop_events`、
    每轮 commit 刷新 `update_time`（防 5 分钟僵尸清理把长任务误判为 failed）。

    **为什么逐指数调用而不是一次传全部 codes**：每个指数要用自己的增量起点
    （`plan["per_index"][code]["start_date"]`）—— 否则落后的指数补不上中间的洞
    （见 `index_valuation.resolve_incremental_start`）。

    计数口径：`done_count` = 已完成快照区间数；`success_count` = 已算交易日数
    （= upsert 行数，**含覆盖旧行**，不等于"新增行数"）。
    覆盖率被门禁拦下**不算失败**，写进 `result.gated` 由前端标黄。
    """
    db = SessionLocal()
    task = db.query(BdsSyncTask).get(task_id)
    errors = []
    per_index, gated, aborted_codes = {}, {}, []
    acc = {"done": 0, "rows": 0}
    total_ranges = int(plan.get("total_ranges") or 0)

    try:
        task.status = "running"
        task.start_time = datetime.now()
        db.commit()

        def on_stats(code, snap, rows_this, done_this, total_this):
            """每个快照区间算完调一次；跨指数累加靠 acc（已完结指数的基数）。"""
            s = SessionLocal()
            try:
                t = s.query(BdsSyncTask).get(task_id)
                d = acc["done"] + done_this
                t.done_count = d
                t.success_count = acc["rows"] + rows_this
                t.progress = round(d / total_ranges * 100, 2) if total_ranges else 100
                s.commit()
            finally:
                s.close()

        stop_event = _stop_events.get(task_id)

        for item in plan["per_index"]:
            code = item["code"]
            if stop_event is not None and stop_event.is_set():
                # 停止后剩余指数**从未开始**：标 skipped 而不是 aborted。
                # 否则「中止指数 5」会让人以为 5 个都跑了一半（实际只有 1 个被打断）。
                per_index[code] = {"rows": 0, "ranges": 0, "aborted": False, "skipped": True,
                                   "gated": [], "start_date": item.get("start_date")}
                continue

            # 起点一律取 plan 的结果（它已按「两端都空/只传起点/只传终点/都传」分好流），
            # 这里再判一次 start_date 会与 plan 分叉
            this_start = (date.fromisoformat(item["start_date"])
                          if item.get("start_date") else None)
            try:
                res = index_valuation.backfill_index_valuation(
                    [code], start_date=this_start, end_date=end_date,
                    with_median=with_median, stop_event=stop_event, on_stats=on_stats)
                r = res.get(code) or {}
            except Exception as ex:      # 单指数失败不拖垮其余指数
                logger.exception("指数估值重算失败 %s", code)
                errors.append({"symbol": code, "table": "index-valuation",
                               "message": f"{type(ex).__name__}: {ex}"})
                per_index[code] = {"rows": 0, "ranges": 0, "aborted": False, "gated": [],
                                   "start_date": item.get("start_date"), "error": str(ex)}
                continue

            if r.get("gated"):
                gated[code] = r["gated"]
            if r.get("aborted"):
                aborted_codes.append(code)
                errors.append({"symbol": code, "table": "index-valuation",
                               "message": "提前中止（被停止）"})
            per_index[code] = {
                "rows": r.get("rows", 0),
                "ranges": r.get("ranges", 0),
                "aborted": bool(r.get("aborted")),
                "gated": r.get("gated") or [],
                "start_date": this_start.isoformat() if this_start else None,
                "audit": r.get("audit"),
            }
            acc["done"] += r.get("ranges", 0)
            acc["rows"] += r.get("rows", 0)

        # 终态：用户停止 > 全无产出且有失败 > 正常完成
        stopped = stop_event is not None and stop_event.is_set()
        if stopped:
            task.status = "stopped"
        elif errors and acc["rows"] == 0:
            task.status = "failed"
        else:
            task.status = "done"

        task.done_count = acc["done"]
        task.success_count = acc["rows"]
        task.progress = round(acc["done"] / total_ranges * 100, 2) if total_ranges else 100
        task.aborted_count = len(aborted_codes)
        task.error_count = len(errors)
        gated_total = sum(len(v) for v in gated.values())
        task.no_data_count = gated_total       # 复用该列：覆盖率不足而未出数的交易日数
        task.errors = errors or None
        # 结构化结果：审计（每指数 compared/changed/max_pct/samples）、门禁明细、落后指数
        task.result = {
            "per_index": per_index,
            "gated": gated,
            "gated_total": gated_total,
            "lagging": plan.get("lagging") or {},
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "latest_trade_date": plan.get("latest_trade_date"),
            "used_calendar": plan.get("used_calendar"),
            "total_ranges": total_ranges,
            "rows_total": acc["rows"],
        }
        task.end_time = datetime.now()
        db.commit()
        print(f'[index-valuation #{task_id}] {task.status}：区间 {acc["done"]}/{total_ranges}, '
              f'已算 {acc["rows"]} 天, 门禁 {gated_total}, 中止 {len(aborted_codes)}, '
              f'失败 {len(errors)}')

    except Exception as e:
        # 兜底：线程异常也要把 task 状态置为 failed
        errors.append({"symbol": "", "table": "index-valuation",
                       "message": f"线程异常: {type(e).__name__}: {e}"})
        task.status = "failed"
        task.errors = errors
        task.end_time = datetime.now()
        db.commit()
        print(f'[index-valuation #{task_id}] 失败: {e}')
    finally:
        _stop_events.pop(task_id, None)
        db.close()


@router.post("/run/batch-sync", response_model=BdsSyncTaskOut)
def run_batch_sync(req: BatchSyncRequest):
    """启动批量同步任务（后台线程异步执行）。

    启动前检查是否已有 status='running' 的任务：有则返回 409，防止重复启动。
    立即返回 task 对象（status=pending），前端可轮询 GET /sync-tasks/{id} 查看进度。
    """
    db = next(get_db())  # 获取请求 session（用于查 + 建 task，线程内另起 session）
    try:
        # 清理僵尸任务（running 超 5 分钟未更新 = 后端崩溃残留）
        _cleanup_zombies(db, minutes=5)

        running = db.query(BdsSyncTask).filter(BdsSyncTask.status == "running").first()
        if running:
            raise HTTPException(status_code=409, detail=f"已有进行中的任务 #{running.id}，请等待完成后再启动")

        # scope 参数校验
        if req.scope not in ("all", "index", "industry", "explicit"):
            raise HTTPException(status_code=400, detail=f"非法 scope: {req.scope}")
        if req.scope == "index":
            # 指数多选：index_codes 为准，index_code 单值写法兼容折成列表；
            # 归一化后写回 req.index_codes ⇒ 后台线程只认这一个字段（不再读 index_code）
            codes = [c.strip() for c in (req.index_codes or []) if c and c.strip()]
            if not codes and req.index_code and req.index_code.strip():
                codes = [req.index_code.strip()]
            seen, uniq = set(), []
            for c in codes:
                if c not in seen:
                    seen.add(c)
                    uniq.append(c)
            if not uniq:
                raise HTTPException(status_code=400, detail="scope=index 时 index_codes 必填（至少选一个指数）")
            req.index_codes = uniq
        if req.scope == "industry" and not req.industry:
            raise HTTPException(status_code=400, detail="scope=industry 时 industry 必填")
        if req.scope == "explicit" and not req.symbols:
            raise HTTPException(status_code=400, detail="scope=explicit 时 symbols 必填")
        # 退市口径校验（schema 未加 pattern ⇒ 由这里统一返 400，与 scope 的校验方式保持一致）
        if req.trading_status not in ("trading", "delisted", "all"):
            raise HTTPException(
                status_code=400,
                detail=f"非法 trading_status: {req.trading_status}（应为 trading/delisted/all）",
            )

        # 组装 scope_params
        scope_params = {}
        if req.scope == "index":
            # 多选：存清单而不是单值（前端「运行中回填」按 index_codes 还原勾选）
            scope_params["index_codes"] = req.index_codes
        elif req.scope == "industry":
            scope_params["industry_name"] = req.industry
        elif req.scope == "explicit":
            scope_params["symbols"] = req.symbols

        task = BdsSyncTask(
            scope=req.scope,
            scope_params=scope_params,
            tables=req.tables or TABLES,
            mark_synced=1 if req.mark_synced else 0,
            exclude_synced_since=req.exclude_synced_since,
            trading_status=req.trading_status,
            max_symbols=req.max_symbols,
            status="pending",
            total_count=0,
            done_count=0,
            success_count=0,
            no_data_count=0,
            error_count=0,
            progress=0,
            errors=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # 注册停止信号（后台线程从 _stop_events[task.id] 取；/stop/batch-sync 端点 set）
        _stop_events[task.id] = threading.Event()

        # 启动后台线程（daemon=True：进程退出时自动终止）
        t = threading.Thread(target=_run_batch_sync_thread, args=(task.id, req), daemon=True)
        t.start()

        return task

    finally:
        db.close()


@router.get("/sync-tasks", response_model=List[BdsSyncTaskOut])
def list_sync_tasks(limit: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)):
    """查询批量同步任务列表。顺带清理僵尸任务（running 超 5 分钟未更新）。"""
    _cleanup_zombies(db, minutes=5)
    items = db.query(BdsSyncTask).order_by(BdsSyncTask.create_time.desc()).limit(limit).all()
    return items


@router.get("/sync-tasks/latest", response_model=BdsSyncTaskOut)
def get_latest_sync_task(db: Session = Depends(get_db)):
    """查询最新一条批量同步任务（供前端轮询进度）。顺带清理僵尸任务。"""
    _cleanup_zombies(db, minutes=5)
    task = db.query(BdsSyncTask).order_by(BdsSyncTask.create_time.desc()).first()
    if not task:
        raise HTTPException(status_code=404, detail="暂无同步任务记录")
    return task


@router.get("/sync-tasks/{task_id}", response_model=BdsSyncTaskOut)
def get_sync_task(task_id: int, db: Session = Depends(get_db)):
    """查询单条批量同步任务详情（含 progress / errors）。顺带清理僵尸任务。"""
    _cleanup_zombies(db, minutes=5)
    task = db.query(BdsSyncTask).get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 #{task_id} 不存在")
    return task


@router.post("/stop/batch-sync")
def stop_batch_sync(task_id: int = Query(..., description="要停止的任务 ID"), db: Session = Depends(get_db)):
    """停止正在运行的批量同步任务。

    原理：对该任务对应的 threading.Event.set()。后台线程每次 sync_one 返回后检查，
    发现 set 就跳出循环，不再启动下一次 sync_one。正在执行的 HTTP 请求无法中断，
    等它自然返回后就停。任务状态最终会被置为 stopped。

    - pending（还没开始跑）：Event.set() 后线程进入 run_tasks 会立即停止，状态=stopped
    - running（正在跑）：等当前 symbol×table 的 sync_one 返回后停止
    - done/failed/stopped：返回 400，任务已结束，无需停止
    - 僵尸任务（后端重启残留）：Event 不存在但 DB 里 status=running → 直接置为 stopped
    """
    ev = _stop_events.get(task_id)
    if ev is None:
        # Event 不存在：查 DB 判断是"任务不存在"还是"后端重启残留的僵尸任务"
        task = db.query(BdsSyncTask).get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务 #{task_id} 不存在")
        if task.status in ("done", "failed", "stopped"):
            return {"status": "already_done",
                    "message": f"任务 #{task_id} 已结束（{task.status}），无需停止"}
        # 僵尸任务：DB 里还是 running/pending，但内存 Event 已丢失（后端重启残留）
        # 无法真的停止了，直接标记为 stopped 让前端不再显示运行中
        task.status = "stopped"
        task.end_time = task.end_time or datetime.now()
        db.commit()
        print(f'[batch-sync] 僵尸任务 #{task_id}：Event 已丢失，直接置为 stopped')
        return {"status": "stopped", "message": f"任务 #{task_id} 是后端重启残留的僵尸任务，已标记为已停止"}

    if ev.is_set():
        return {"status": "already_stopped", "message": f"任务 #{task_id} 已发出停止信号"}

    ev.set()
    print(f'[batch-sync] 已发送停止信号 task #{task_id}')
    return {"status": "stopping", "message": f"任务 #{task_id} 停止信号已发出，正在等待当前请求返回..."}

# ============================================================================
# 指数分位（P0-1）与成分核查水位（P0-0）
# 方案：docs/指数分位方案_2026-10-20.md
# ============================================================================

@router.get("/index-valuations")
def list_index_valuations(
    start_date: Optional[str] = Query(default=None, description="起始日期 YYYY-MM-DD，默认近 10 年"),
    end_date: Optional[str] = Query(default=None, description="结束日期 YYYY-MM-DD，默认今天"),
    metric: str = Query(default="pe_ttm",
                        description="指标: pe_ttm / pb_mrq / dy_ttm / pe_median / ps_ttm / pcf_ttm_oper"),
    index_codes: Optional[List[str]] = Query(default=None, description="指数代码（可多值），不传=默认 5 个宽基"),
):
    """指数加权估值日序列（画图用）。

    结构对齐 `/index-cum-returns`：`trade_dates` + `series`，前端可复用图表逻辑。
    返回里另带 `last_change_dates`（成分名单末次变化日）——它和 `trade_dates` 无关，
    是「用的成分是哪天确定的」，页面标注新鲜度用。
    """
    try:
        s = date.fromisoformat(start_date) if start_date else None
        e = date.fromisoformat(end_date) if end_date else None
        return index_valuation.get_series(index_codes, s, e, metric)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))


@router.get("/index-valuations/paged", response_model=PageResponse[IndexValuationRow])
def list_index_valuations_paged(
    index_codes: Optional[List[str]] = Query(
        default=None, description="指数代码（可多值，如 ?index_codes=A&index_codes=B），不传=默认 5 个宽基"),
    start_date: Optional[date] = Query(default=None, description="起始日期 YYYY-MM-DD"),
    end_date: Optional[date] = Query(default=None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """指数估值原始数据（行式分页），供 `/bds/index-valuation` 表格页使用。

    与 `/index-valuations` 的区别：那个是 pivot（`trade_dates[]` + `series{}`）给图表画线，
    中间量全被压掉；这里**一行 = 一个指数的一天**，保留 `n_total`/`n_valid`/`n_loss`/
    `snapshot_date`/`calc_at`，可以核对「这个 PE 是怎么算出来的、用的是哪天确认的成分」。

    排序 `trade_date DESC, index_code ASC`：首页固定是「最新交易日 × 5 个指数」，
    与 `/index-valuation-percentiles` 的当前值对得上。
    """
    _guard_list_offset(offset)
    try:
        return index_valuation.list_paged(
            index_codes, start_date, end_date, limit=limit, offset=offset)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))


@router.get("/index-valuation-percentiles")
def list_index_valuation_percentiles(
    index_codes: Optional[List[str]] = Query(default=None, description="指数代码（可多值），不传=默认 5 个宽基"),
    metric: str = Query(default="pe_ttm",
                        description="指标: pe_ttm / pb_mrq / dy_ttm / pe_median / ps_ttm / pcf_ttm_oper"),
):
    """指数分位汇总（卡片/表格用）。

    **分位不落库，实时算**：窗口每天滑动、当前值每天新增，存下来当天有效次日即过期。
    窗口口径唯一：自然日回溯（3/5/10 年 = 1095/1825/3650 天），锚点是各指数最后一个有值的交易日。

    陈旧度：每项带 `as_of`（当前值所属交易日）、`is_stale`、`stale_days`，与顶层
    `latest_trade_date`（个股估值源末日）比较 —— 估值序列没跟上时必须让页面知道
    "这是 T-N 的值"，不能当最新值展示。
    另带 `constituent_stale`：成分核查水位是否落后于估值序列末日（落后则分位可信度打折）。
    """
    try:
        return index_valuation.get_percentiles(index_codes, metric)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))


@router.post("/sync/index-valuation", response_model=BdsSyncTaskOut)
def sync_index_valuation(req: IndexValuationSyncRequest):
    """启动指数估值重算（后台任务），立即返回 task，进度用 GET /sync-tasks/{id} 轮询。

    **入口唯一**：原 `/run/index-valuation-backfill` 已删除 —— 它和本接口只差
    「强制传 start_date」3 行代码，任务化后这个保护没有意义（增量用 `days`、
    区间重建用 `start_date`，同一次调用里按传参分流）。
    全历史重建（36 分钟）仍走命令行 `scripts/backfill_index_valuation.py`，HTTP 必超时。

    区间语义见 `IndexValuationSyncRequest`（four 种组合）；`start_date` / `end_date`
    都是**闭区间**，`end_date` 当天必算（内部 `+1 天` 转成 SQL 的左闭右开）。

    停止：POST /bds/stop/batch-sync?task_id=<id>（在当前快照区间结束后生效）。

    ⚠️ 顺序：应先跑 `/sync/index-constituent` 增量，否则会用旧成分算新日期。
    ⚠️ 与批量同步、成分同步共用 bds_sync_task 与停止信号 ⇒ 全局互斥（有 running 则 409）。
    这是要的：估值读 `bds_daily_valuation`，批量同步正在写它时必须不能并发。
    """
    db = next(get_db())
    try:
        # 1. 参数校验（先于建任务，避免非法入参在任务表里留下垃圾记录）
        today = date.today()
        if req.start_date and req.start_date < date(2005, 1, 1):
            raise HTTPException(status_code=400,
                                detail="start_date 不能早于 2005-01-01（该表最早数据起点）")
        if req.end_date is not None:
            if req.end_date > today:
                raise HTTPException(status_code=400,
                                    detail=f"end_date 不能晚于今天（{today}）：未来日期无数据，跑了只会空转")
            if req.start_date and req.end_date < req.start_date:
                raise HTTPException(status_code=400, detail="end_date 不能早于 start_date")
        try:
            plan = index_valuation.plan_index_valuation_backfill(
                req.index_codes, req.start_date, req.end_date, req.days)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        if plan["total_ranges"] == 0:
            raise HTTPException(
                status_code=400,
                detail="该区间内没有可算的交易日（各指数最早成分快照晚于 end_date？）")

        # 2. 僵尸清理 + 互斥检查
        _cleanup_zombies(db, minutes=5)
        running = db.query(BdsSyncTask).filter(BdsSyncTask.status == "running").first()
        if running:
            raise HTTPException(status_code=409,
                                detail=f"已有进行中的任务 #{running.id}，请等待完成后再启动")

        # 3. 建任务（scope=index_valuation，与 index_backfill 区分开）
        task = BdsSyncTask(
            scope="index_valuation",
            scope_params={
                "index_codes": [p["code"] for p in plan["per_index"]],
                "start_date": req.start_date.isoformat() if req.start_date else None,
                "end_date": req.end_date.isoformat() if req.end_date else None,
                # days 只在「两端都不传」的纯增量模式下参与计算（指定任一端点时
                # 起点由 start_date 或最早快照决定）。此处写 null 而非原值，
                # 否则审计时会把 days=5 误读成「本次重算了 5 个交易日」
                # —— 实际只传 end_date 时会从最早快照算起（如 #140 实算 110 行）。
                "days": req.days if (req.start_date is None and req.end_date is None) else None,
                "with_median": req.with_median,
            },
            tables=["index-valuation"],
            status="running",
            total_count=plan["total_ranges"],
            done_count=0,
            success_count=0,
            no_data_count=0,
            error_count=0,
            progress=0,
            errors=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # 4. 注册停止信号并启动后台线程
        _stop_events[task.id] = threading.Event()
        threading.Thread(
            target=_run_valuation_thread,
            args=(task.id, plan, req.start_date, req.end_date, req.with_median),
            daemon=True,
        ).start()

        return task

    finally:
        db.close()


@router.get("/index-constituent-sync-status")
def get_index_constituent_sync_status(db: Session = Depends(get_db)):
    """各指数成分核查水位（只读，实时聚合）。

    用途：① 批量同步页「成分」卡片顶部摘要；② 估值页成分新鲜度标注；
    ③ 脏数据体检——`current_rows` 为 0 但 `checked_to` 有值，说明成分行被手工删过
    （如 SHSE.000001），此时水位行也应当一起删。
    """
    counts = dict(
        db.query(IndexConstituent.index_code, func.count(IndexConstituent.id))
        .group_by(IndexConstituent.index_code)
        .all()
    )
    rows = db.query(IndexConstituentSync).order_by(IndexConstituentSync.index_code).all()

    items = []
    for r in rows:
        info = Config.INDEX_CODE.get(r.index_code, {})
        items.append({
            "index_code": r.index_code,
            "sec_name": info.get("sec_name"),
            "checked_to": r.checked_to.isoformat() if r.checked_to else None,
            "last_change_date": r.last_change_date.isoformat() if r.last_change_date else None,
            "constituent_count": r.constituent_count,
            "gap_days": r.gap_days,
            "first_gap": r.first_gap.isoformat() if r.first_gap else None,
            "no_data_days": r.no_data_days,
            "dirty_days": r.dirty_days,
            "first_dirty": r.first_dirty.isoformat() if r.first_dirty else None,
            "last_check_at": r.last_check_at.strftime("%Y-%m-%d %H:%M:%S") if r.last_check_at else None,
            "current_rows": counts.get(r.index_code, 0),
        })
    return {"items": items, "total": len(items)}


# ============================================================================
# 期权行情（bds_option_market）与同步清单（bds_option_config）
#
# 需求：① 单独建 bds_option_market 存行情；② bds 增加期权行情查询页；
#      ③ 搜索区一键同步「配置的全部期权」。
#
# 关于「搜索区放同步按钮」与 bds 既有惯例（数据页不放写操作）的张力：
# 这是**搜索区的批量任务按钮**，不针对某一行、走 bds_sync_task 任务化、有审计（写 result），
# 性质与 batch-sync 页的卡片一致；行内仍然一个写按钮都没有 ⇒ 不违背铁律 4.8 的本意。
# ============================================================================

# 品种名 → und_key / 标的（当配置里留空时的兜底来源）
_OPT_UND_KEY = {
    "SHSE.000300": "csi300",
    "SHSE.510500": "csi500etf",
    "SHFE.au": "au",
}


def _canon_underlying(u: Optional[str]) -> Optional[str]:
    """标的代码归一到 irs 口径。

    ⚠️ 两套交易所代码并存：账单里是 `SHEX.au`（SHEX=上海期货交易所的券商写法），
    irs 配置里是 `SHFE.au`（行情口径）。它们的国库号相同、字符串不同，
    不做归一会导致「按标的找品种名」永远找不到黄金。
    """
    if not u:
        return u
    head, _, tail = u.partition(".")
    if head.upper() in ("SHEX", "SHFE"):
        return f"SHFE.{tail}"
    return u


def _opt_meta_by_name(option_name: str) -> dict:
    """按品种名取 irs 配置里的元信息（标的/乘数/到期规则）。"""
    for item in IrsCfg.OPTIONS_MARCH:
        if item["option_name"] == option_name:
            return item
    raise HTTPException(
        status_code=400,
        detail=f"未知期权品种「{option_name}」，须是 IrsCfg.OPTIONS_MARCH 里的 option_name"
               f"（{'、'.join(i['option_name'] for i in IrsCfg.OPTIONS_MARCH)}）")


@router.get("/option-market", response_model=PageResponse[OptionMarketOut])
def list_option_market(
    und_key: Optional[str] = Query(default=None, description="情景分组键: csi300/csi500etf/au"),
    option_name: Optional[str] = Query(default=None, description="品种名（经配置表换算成 und_key 集合）"),
    end_month: Optional[str] = Query(default=None, description="到期年月 YYYYMM（按 delisted_date 所在月筛）"),
    option_type: Optional[str] = Query(default=None, description="call / put"),
    symbol: Optional[str] = Query(default=None, description="合约代码，模糊匹配"),
    trade_date: Optional[date] = Query(default=None, description="行情日期；不传=最新交易日全量"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """分页查询期权行情。

    默认只返回**最新交易日**的行：这张表按日累积，不传日期全量返回会把历史快照也铺出来，
    用户看盘时反而找不到当天数据。

    ⚠️ 排序必须显式给出：服务端 offset/limit 分页缺少 ORDER BY 时 MySQL 不保证顺序，
    会出现翻页跳行/重复行（`/bills/bills` 踩过）。
    """
    _guard_list_offset(offset)
    query = db.query(OptionMarket)

    if und_key:
        query = query.filter(OptionMarket.und_key == und_key)
    if option_name:
        # 品种名经配置表映射成 und_key 集合，避免前端硬编码映射
        und_keys = [
            _OPT_UND_KEY[m["underlying_symbol"]]
            for m in IrsCfg.OPTIONS_MARCH
            if m["option_name"] == option_name and m["underlying_symbol"] in _OPT_UND_KEY
        ]
        if not und_keys:
            raise HTTPException(status_code=400, detail=f"未知品种：{option_name}")
        query = query.filter(OptionMarket.und_key.in_(und_keys))
    if option_type:
        query = query.filter(OptionMarket.option_type == option_type)
    if symbol:
        query = query.filter(OptionMarket.symbol.contains(symbol))
    if end_month:
        # delisted_date 落在该月 ⇒ 该到期月的合约
        try:
            y, m = int(end_month[:4]), int(end_month[4:6])
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail=f"end_month 格式应为 YYYYMM：{end_month}")
        start = date(y, m, 1)
        end = date(y + (m == 12), (m % 12) + 1, 1)
        query = query.filter(OptionMarket.delisted_date >= start,
                             OptionMarket.delisted_date < end)

    if trade_date is None:
        latest = db.query(func.max(OptionMarket.trade_date)).scalar()
        if latest is not None:
            query = query.filter(OptionMarket.trade_date == latest)

    total = query.count()
    items = (
        query.order_by(OptionMarket.und_key.asc(), OptionMarket.delisted_date.asc(),
                       OptionMarket.price_strike.asc(), OptionMarket.option_type.asc())
        .offset(offset).limit(limit).all()
    )
    return {
        "items": [i.to_dict() for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/option-market/latest-date")
def get_option_market_latest_date(db: Session = Depends(get_db)):
    """最新行情日期 + 各品种在该日的行数（页面默认值与新鲜度提示）。"""
    latest = db.query(func.max(OptionMarket.trade_date)).scalar()
    if latest is None:
        return {"latest_trade_date": None, "by_und": {}, "sync_time": None}
    rows = (
        db.query(OptionMarket.und_key, func.count(OptionMarket.id),
                 func.max(OptionMarket.sync_time))
        .filter(OptionMarket.trade_date == latest)
        .group_by(OptionMarket.und_key).all()
    )
    return {
        "latest_trade_date": latest.isoformat(),
        "by_und": {r[0]: r[1] for r in rows},
        "sync_time": max((r[2] for r in rows if r[2]), default=None),
    }


@router.get("/option-market/filters")
def get_option_market_filters(db: Session = Depends(get_db)):
    """下拉选项：品种 / 到期月 / 分组键（来自配置表 + 行情表现有值）。"""
    cfg_names = [r[0] for r in db.query(OptionConfig.option_name).distinct().all()]
    if not cfg_names:
        cfg_names = [m["option_name"] for m in IrsCfg.OPTIONS_MARCH]
    dms = [r[0] for r in db.query(OptionMarket.delisted_date).distinct().all() if r[0]]
    end_months = sorted({d.strftime("%Y%m") for d in dms})
    return {
        "option_names": sorted(cfg_names),
        "end_months": end_months,
        "und_keys": sorted(set(_OPT_UND_KEY.values())),
    }


@router.get("/option-market/config-count", response_model=OptionSyncPlanOut)
def get_option_market_config_count():
    """同步前置确认：启用几张合约、几个品种×到期月组合（= 几次接口请求）。"""
    return option_market.build_sync_plan()


@router.post("/option-market/sync", response_model=BdsSyncTaskOut)
def sync_option_market_now():
    """一键同步「配置的全部期权」（后台线程 + 任务化 + 全局互斥）。

    - 与批量同步 / 成分同步 / 估值重算**共用** `bds_sync_task` 与 `_stop_events`：
      同时跑两批会打满 akshare 请求，且 `_cleanup_zombies` 机制可直接复用。
    - 立即返回 task（status=running），前端轮询 `GET /bds/sync-tasks/{id}`。
    - `total_count` 写**组数**而不是合约张数：实际耗时在接口请求上，
      进度条必须能显示「第几组/共几组」，按行数报进度会长时间停在 0%。
    """
    db = next(get_db())
    try:
        plan = option_market.build_sync_plan()
        if plan["symbol_count"] == 0:
            raise HTTPException(status_code=400,
                                detail="配置清单为空，请先在「期权同步清单」里配置要同步的合约")

        _cleanup_zombies(db, minutes=5)
        running = db.query(BdsSyncTask).filter(BdsSyncTask.status == "running").first()
        if running:
            raise HTTPException(status_code=409,
                                detail=f"已有进行中的任务 #{running.id}，请等待完成后再启动")

        task = BdsSyncTask(
            scope="option_market",          # 12 字符，列宽 String(16) 之内
            scope_params={"groups": plan["groups"]},
            tables=["option-market"],
            status="running",
            total_count=plan["group_count"],
            done_count=0,
            success_count=0,
            no_data_count=0,
            error_count=0,
            progress=0,
            errors=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        _stop_events[task.id] = threading.Event()
        threading.Thread(target=_run_option_market_thread,
                         args=(task.id, plan["group_count"]), daemon=True).start()
        return task
    finally:
        db.close()


def _run_option_market_thread(task_id: int, group_total: int):
    """后台线程：执行期权行情同步，逐组回写进度，收尾写 result。"""
    db = SessionLocal()
    task = db.query(BdsSyncTask).filter(BdsSyncTask.id == task_id).first()
    if task is None:
        db.close()
        return
    stop_event = _stop_events.get(task_id)

    def on_progress(done: int, total: int, label: str):
        """按「组」报进度；顺带检查停止信号，避免停不下来。"""
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError(f"用户停止（已完成 {done}/{total} 组，最后一组：{label}）")
        task.done_count = done
        task.progress = round(done / total * 100, 2) if total else 100
        db.commit()

    try:
        # start_time 必须自己写：本线程不走 _run_sync_thread 的初始化，漏写会让审计表出现
        # 「有 end_time 没有 start_time」的半截记录（历史 #167/#168/#169 三例）
        task.start_time = datetime.now()
        db.commit()

        stats = option_market.sync_option_market(on_progress=on_progress)
        errors = stats.pop("errors", []) or []
        stopped = stop_event is not None and stop_event.is_set()

        task.status = "stopped" if stopped else ("failed" if stats["group_ok"] == 0 else "done")
        task.done_count = group_total
        task.progress = 100
        # 口径：本任务 success_count = **写入行数**（与 macro_sync 任务里「项数」不同，
        # 但与该任务 result.stages[].rows 同义）——前端文案统一写「行」
        task.success_count = stats["row_insert"] + stats["row_update"]
        task.error_count = len(errors)
        # 跳过行数只留在 result.row_skip，**不再借用 no_data_count**：
        # 该列在 macro_sync / index_valuation 里分别表示「无数据项数」「未出数交易日数」，
        # 同一列三种语义会让前端把「跳过 6 行」读成「6 项无数据」
        task.no_data_count = 0
        task.errors = errors or None
        task.result = stats
        task.end_time = datetime.now()
        db.commit()
        print(f"[option-market #{task_id}] {task.status}："
              f"组 {stats['group_ok']}/{stats['group_total']}，"
              f"新增 {stats['row_insert']}、更新 {stats['row_update']}、跳过 {stats['row_skip']}")
    except Exception as e:  # noqa: BLE001
        task.status = "failed"
        task.errors = (task.errors or []) + [
            {"symbol": "", "table": "option-market",
             "message": f"线程异常: {type(e).__name__}: {e}"}]
        task.end_time = datetime.now()
        db.commit()
        print(f"[option-market #{task_id}] 失败: {e}")
    finally:
        _stop_events.pop(task_id, None)
        db.close()


# ---- 同步清单 CRUD ----

@router.get("/option-config", response_model=List[OptionConfigOut])
def list_option_config(
    is_enabled: Optional[int] = Query(default=None, description="1=仅启用 0=仅停用"),
    db: Session = Depends(get_db),
):
    """同步清单（按品种+到期月+行权价排序，便于核对）。"""
    query = db.query(OptionConfig)
    if is_enabled is not None:
        query = query.filter(OptionConfig.is_enabled == is_enabled)
    rows = query.order_by(OptionConfig.option_name.asc(),
                          OptionConfig.end_month.asc(),
                          OptionConfig.symbol.asc()).all()
    return rows


@router.post("/option-config", response_model=OptionConfigOut)
def create_option_config(req: OptionConfigCreate, db: Session = Depends(get_db)):
    """新增同步配置。

    symbol 会做归一化（去 `CFFEX.`/`SHEX.` 账单前缀、去 ETF 的标的数字前缀），
    所以粘贴账单代码也能直接存成交易所标准码。
    """
    meta = _opt_meta_by_name(req.option_name)
    underlying = _canon_underlying(req.underlying_symbol) or meta["underlying_symbol"]
    und_key = req.und_key or _OPT_UND_KEY.get(underlying)
    if not und_key:
        raise HTTPException(status_code=400,
                            detail=f"无法从标的 {underlying} 推出 und_key，请显式指定（csi300/csi500etf/au）")
    sym = option_market.normalize_symbol(req.symbol)
    if not sym:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    is_commodity = meta["option_type"] == "商品期权"
    end_month = req.end_month or None
    if is_commodity:
        end_month = "ALL"          # 接口一次返回全部到期月，填实际月份会造成误解
    elif not end_month:
        raise HTTPException(status_code=400,
                            detail=f"{req.option_name} 必须指定 end_month（接口按到期月拉取）")
    if end_month and end_month != "ALL" and (len(end_month) != 6 or not end_month.isdigit()):
        raise HTTPException(status_code=400, detail=f"end_month 格式应为 YYYYMM：{end_month}")

    exists = db.query(OptionConfig).filter(OptionConfig.symbol == sym).first()
    if exists:
        raise HTTPException(status_code=400, detail=f"该合约已在清单里：{sym}")

    row = OptionConfig(
        symbol=sym,
        broker_symbol=req.broker_symbol,
        underlying_symbol=underlying,
        und_key=und_key,
        option_name=req.option_name,
        end_month=end_month,
        is_enabled=req.is_enabled,
        source="manual",
        note=req.note,
        # 规格字段：可留空（观察型合约只看行情），同步时会自动回填。
        # margin_type 是品种属性，没显式传就按品种派生，省一次手工填写。
        cp=req.cp.upper() if req.cp else None,
        strike=req.strike,
        multiplier=req.multiplier,
        margin_type=req.margin_type or option_market.margin_type_of(req.option_name),
        expiry_date=req.expiry_date,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/option-config/{cfg_id}", response_model=OptionConfigOut)
def update_option_config(cfg_id: int, req: OptionConfigUpdate, db: Session = Depends(get_db)):
    """改人工列（启用开关 / 备注 / 合约规格）。身份字段（symbol / 标的 / 到期月）不可改。

    ⚠️ **一律 None 视为「不改」**，绝不做全字段覆盖 —— 前端只改一个字段就只发一个，
    否则会把没传的列清空（这是 PUT 全字段覆盖踩过的坑）。
    规格字段可人工修正：历史上 `au2612P840` 到期日确实手填错过（11-23，实为 11-24）。
    """
    row = db.query(OptionConfig).filter(OptionConfig.id == cfg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"配置不存在：{cfg_id}")
    for field in ("is_enabled", "note", "strike", "multiplier",
                  "margin_type", "expiry_date"):
        value = getattr(req, field)
        if value is not None:
            setattr(row, field, value)
    if req.cp is not None:
        row.cp = req.cp.upper()
    db.commit()
    db.refresh(row)
    return row


@router.delete("/option-config/{cfg_id}")
def delete_option_config(cfg_id: int, db: Session = Depends(get_db)):
    """删除配置。**不删行情**——行情是事实数据，删配置只影响后续同步范围。"""
    row = db.query(OptionConfig).filter(OptionConfig.id == cfg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"配置不存在：{cfg_id}")
    db.delete(row)
    db.commit()
    return {"status": "ok", "message": f"已删除配置 {row.symbol}（行情历史保留）"}


@router.post("/option-config/backfill-spec", response_model=OptionConfigBackfillResult)
def backfill_option_config_spec(db: Session = Depends(get_db)):
    """从行情快照补全 config 中为空的合约规格（兜底路径）。

    ⚠️ **原 `POST /option-config/import-holding` 已随合并下线**（2026-10-26）：
    它的唯一职责是把 `bds_option_contract`（合约字典）的行搬进同步清单，而合并后
    字典就是清册本身，不存在"搬"这个动作。需要恢复"从持仓自动建行"时必须另找
    持仓源——`bills_group` 属跨应用依赖，已明确不走；正确方向是从行情接口选合约。

    规格字段允许为空（观察型合约只看行情、不算风险），补全有两条路径：

    ① **自动**：同步时 `_sync_group` 拿到接口原文后顺手回填（常态，无需触发本端点）；
    ② **手工**：本端点从 `bds_option_market` 最新快照补（兜底场景：config 行是
       后来用 SQL 或 API 加的，而行情早已同步过）。

    **只填空值、绝不覆盖已有值** —— 已有值可能是人工修正过的（`au2612P840` 到期日
    曾被误填 `11-23`，权威算法给 `11-24`）。`margin_type` 不来自行情，按品种名派生。
    """
    filled, skipped, no_data = 0, 0, []

    # 一次取全部合约的最新快照：按 (symbol, trade_date) 升序遍历，后写覆盖前写
    latest: dict[str, OptionMarket] = {}
    for m in db.query(OptionMarket).order_by(
            OptionMarket.symbol.asc(), OptionMarket.trade_date.asc()).all():
        latest[m.symbol] = m

    for cfg in db.query(OptionConfig).all():
        gaps = [f for f in ("cp", "strike", "multiplier", "expiry_date")
                if getattr(cfg, f) is None]
        need_mt = cfg.margin_type is None
        if not gaps and not need_mt:
            skipped += 1
            continue

        changed = False
        if gaps:
            m = latest.get(cfg.symbol)
            if m is None:
                no_data.append({"symbol": cfg.symbol,
                                "reason": "无行情快照，请先执行一次行情同步"})
                continue
            if "cp" in gaps and m.option_type:
                cfg.cp = "C" if str(m.option_type).lower().startswith("c") else "P"
                changed = True
            if "strike" in gaps and m.price_strike is not None:
                cfg.strike = m.price_strike
                changed = True
            if "multiplier" in gaps and m.multiplier is not None:
                cfg.multiplier = m.multiplier
                changed = True
            if "expiry_date" in gaps and m.delisted_date is not None:
                cfg.expiry_date = m.delisted_date
                changed = True

        if need_mt:
            mt = option_market.margin_type_of(cfg.option_name)
            if mt:
                cfg.margin_type = mt
                changed = True

        if changed:
            filled += 1
        else:
            no_data.append({"symbol": cfg.symbol, "reason": "快照缺必要字段，无法补全"})

    db.commit()
    return {"filled": filled, "skipped": skipped, "no_data": no_data}
