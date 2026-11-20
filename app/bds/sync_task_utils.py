# -*- coding: utf-8 -*-
"""bds_sync_task 后台任务的跨模块共享机制。

bds 的批量同步 / 成分股 / 估值 / 一键同步线程，以及 bills 的交易明细导入线程
（scope='bills_import'），共用 bds_sync_task 任务表与停止端点
POST /bds/stop/batch-sync。停止信号与僵尸清理必须放在两处都能 import 的中立位置：
依赖方向是 bills → bds（规则 4.12，bds 只能被依赖），故放 bds 模块内。

2026-11-16 起另承载「跨模块同步段执行器注册表」：宏观一键同步的
underlying_hlc / underlying_quote / position_quote 三段由 irs / bills 实现，bds 不能
反向 import（规则 4.12）。改由上层在 router 加载期把执行函数注入这里，bds 只按名取用。
（underlying_hlc 于 2026-11-20 补入：此前年度高低收只挂在手动端点，不在同步链，
导致 y_low 长期冻结、出现「现价低于年内最低」的矛盾数据。）
"""
import threading
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from server_fast.app.bds.models import BdsSyncTask

# 模块级停止信号字典：{task_id: threading.Event}，供 POST /bds/stop/batch-sync 触发。
# 任何模块的后台线程启动前须注册自己的 Event，停止端点按 task_id 取用。
STOP_EVENTS: dict[int, threading.Event] = {}

# ---------------------------------------------------------------------------
# 跨模块同步段执行器（依赖倒置）
# ---------------------------------------------------------------------------
# key = SYNC_STAGE_ORDER 中的段名；value = 无参函数，返回 int 计数口径：
#   >0 成功（写入行数或完成步数）/ 0 无数据 / -1 失败
# 🔴 未注册时 bds._run_sync_item 必须显式报错并计 -1，禁止静默跳过：注册发生在
#    router 加载期，--reload / 单模块测试都可能让注册表为空。
STAGE_RUNNERS: dict[str, Callable[[], int]] = {}

# 段执行器回传的运行时上下文（steps 明细 / 异常原因）。全库一把互斥锁 ⇒ 同一时刻
# 只有一个同步任务在跑，单线程内顺序消费，无需加锁；每项完成后必须 pop 清空。
_STAGE_RUNTIME: dict[str, dict] = {}


def register_stage_runner(stage: str, fn: Callable[[], int]) -> None:
    """注册非 bds 域同步段的执行函数（由 irs / bills 的 router 在加载期调用）。"""
    STAGE_RUNNERS[stage] = fn


def get_stage_runner(stage: str) -> Callable[[], int] | None:
    """取执行器；未注册返回 None，调用方必须显式报错。"""
    return STAGE_RUNNERS.get(stage)


def set_stage_runtime(stage: str, **fields) -> None:
    """执行器回传 steps 明细或异常原因（按字段更新，不覆盖已有字段）。"""
    _STAGE_RUNTIME.setdefault(stage, {}).update(fields)


def pop_stage_runtime(stage: str) -> dict:
    """bds 线程在每项完成后取走并清空，避免污染下一次任务。"""
    return _STAGE_RUNTIME.pop(stage, {})


def cleanup_zombies(db: Session, minutes: int = 5) -> int:
    """清理僵尸任务（running 状态超过指定分钟数未更新）。返回清理数量。

    后端重启会丢失内存中的 STOP_EVENTS，DB 里残留的 running 任务会永久占住
    全局互斥；各启动端点在互斥检查前先调用本函数兜底。
    """
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
