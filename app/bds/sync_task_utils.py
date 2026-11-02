# -*- coding: utf-8 -*-
"""bds_sync_task 后台任务的跨模块共享机制。

bds 的批量同步 / 成分股 / 估值 / 一键同步线程，以及 bills 的交易明细导入线程
（scope='bills_import'），共用 bds_sync_task 任务表与停止端点
POST /bds/stop/batch-sync。停止信号与僵尸清理必须放在两处都能 import 的中立位置：
依赖方向是 bills → bds（规则 4.12，bds 只能被依赖），故放 bds 模块内。
"""
import threading
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from server_fast.app.bds.models import BdsSyncTask

# 模块级停止信号字典：{task_id: threading.Event}，供 POST /bds/stop/batch-sync 触发。
# 任何模块的后台线程启动前须注册自己的 Event，停止端点按 task_id 取用。
STOP_EVENTS: dict[int, threading.Event] = {}


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
