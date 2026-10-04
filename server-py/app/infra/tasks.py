# server-py/app/infra/tasks.py
"""后台任务：创建 + 保命（强引用）+ 失败可见。

为什么必须单独有个东西管这件事：asyncio.create_task() 的返回值如果没人接住，
事件循环**只持弱引用** —— 任务可能在跑完之前就被垃圾回收掉，而且**不会有任何报错**。

2026-10-04 实测的坑：用户画像的异步抽取时灵时不灵 —— 先说"我叫小米"，右侧画像面板
有时有名字、有时没有，日志干干净净。原因是抽取任务被 GC 掉，不是模型没抽出来；
排查时最贵的就是这种"静默消失"。

所以，凡是"不阻塞本次响应"的后台任务都走 spawn()：
1. 任务对象存进模块级集合（强引用），跑完再移除；
2. 挂 done 回调，把未捕获异常写进日志 —— 后台任务静默失败是最贵的失败。
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine

from app.core.logger import logger

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str = "background") -> asyncio.Task:
    """起一个后台任务并保证它不会中途被回收；失败会留下日志。"""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if t.cancelled():
            return
        err = t.exception()
        if err is not None:
            logger.warn("background task failed", {"name": name, "error": str(err)[:300]})

    task.add_done_callback(_done)
    return task


def pending() -> int:
    """还有几个后台任务在跑（诊断/测试用）。"""
    return len(_tasks)
