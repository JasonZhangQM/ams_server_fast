# -*- coding: utf-8 -*-
"""irs 业务公共辅助：日志器、通用 session 封装。

被 value_monitor / discount_monitor / option_monitor 子模块共享。
"""
import logging

# 复用 uvicorn 的 logger，输出到 stderr 不被缓冲
logger = logging.getLogger("uvicorn.error")


def _flush_and_commit(session, obj=None):
    """统一封装：add（如传入 obj）-> flush（触发事件钩子）-> commit。

    替代 Django 的 obj.save()。在事务块 (with session.begin()) 内不应调用此函数，
    因为 begin 会在退出时自动 commit。
    """
    if obj is not None:
        session.add(obj)
    session.flush()  # 触发 before_insert / before_update 事件钩子，计算衍生字段
    session.commit()
