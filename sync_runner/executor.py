# -*- coding: utf-8 -*-
"""批量执行器 + 结果汇总。

从 run2-批量同步.ipynb Cell 5-6 原样抽取，全局变量 RESULTS 改为参数传入/返回。
"""

import time
import random

import pandas as pd

from .config import SLEEP_MIN, SLEEP_MAX, FAIL_STREAK_LIMIT, FAIL_STREAK_SLEEP
from .sync import sync_one, mark_synced


def run_tasks(tasks, mark_synced_when_done=True, on_progress=None, stop_event=None):
    '''执行 (symbol, table) 任务列表，返回结果记录列表。

    on_progress: 可选回调，签名 fn(symbol, table, status, done_count, total_count)
    stop_event: 可选 threading.Event，set 后提前中止（最后一次 sync_one 返回后退出循环）
    '''
    results = []
    total = len(tasks)
    if total == 0:
        print('无任务。')
        return results

    # 按 symbol 归并，保证同一标的的多张表连续执行（便于整只完成后打标记）
    by_symbol = {}
    for sym, table in tasks:
        by_symbol.setdefault(sym, []).append(table)

    t0 = time.time()
    fail_streak = 0
    i = 0
    marked = 0
    stopped = False
    for sym, tables in by_symbol.items():
        sym_statuses = []
        for table in tables:
            i += 1
            res = sync_one(sym, table)
            results.append({'symbol': sym, 'table': table, **res})
            sym_statuses.append(res['status'])

            # 实时进度回调（供后端后台线程写 DB）
            if on_progress is not None:
                try:
                    on_progress(sym, table, res['status'], i, total)
                except Exception as exc:
                    # 回调异常不影响主流程（DB 挂了也要继续同步）
                    print(f'  [警告] on_progress 回调异常: {exc}')

            if res['status'] in ('success', 'no_data'):
                fail_streak = 0
            else:
                fail_streak += 1

            # 检查停止信号（正在执行的 sync_one 无法中断，等它返回后跳出）
            if stop_event is not None and stop_event.is_set():
                stopped = True
                print(f'[{i}/{total}] 收到停止信号，中止后续任务')
                break

            # 按状态分档限速：no_data 是后端预判跳过（纯 DB 查询，未调外部 API），
            # 无需 1~3s 限速，仅短暂让出 CPU 即可；success / error 保留完整限速。
            if res['status'] == 'no_data':
                time.sleep(0.1)
            else:
                pause = random.uniform(SLEEP_MIN, SLEEP_MAX)
                if fail_streak >= FAIL_STREAK_LIMIT:
                    pause += FAIL_STREAK_SLEEP
                    print(f'  连续失败 {fail_streak} 次，额外停顿 {FAIL_STREAK_SLEEP:.0f}s')
                    fail_streak = 0
                time.sleep(pause)

            if i % 10 == 0 or i == total:
                elapsed = time.time() - t0
                eta = elapsed / i * (total - i)
                print(f'[{i}/{total}] {sym} {table:15s} -> {res["status"]:7s} | '
                      f'已用 {elapsed:.0f}s 预计剩余 {eta:.0f}s | {res["message"][:40]}')

        if stopped:
            break

        # 该 symbol 全部表都非 error → 标记当日已同步
        if mark_synced_when_done and all(st in ('success', 'no_data') for st in sym_statuses):
            mk = mark_synced(sym)
            if mk['status'] == 'ok':
                marked += 1
            else:
                print(f'  [警告] {sym} 标记 sync_at 失败：{mk["message"][:60]}')

    prefix = '（已停止）' if stopped else '完成'
    print(f'{prefix}，用时 {time.time() - t0:.0f}s | 已标记当日同步 {marked} 只 | 本次处理 {len(results)}/{total}')
    return results


def summarize(results):
    '''打印结果分布 / pivot / 失败明细。

    返回 bool：True=有 error，False=全部 success/no_data。
    '''
    df = pd.DataFrame(results)
    print('本次执行条数:', len(df))
    has_err = False
    if not df.empty:
        print('\n状态分布：')
        print(df['status'].value_counts().to_string())

        print('\n各表 x 状态：')
        pivot = (df.pivot_table(index='table', columns='status', values='symbol', aggfunc='count')
                   .fillna(0).astype(int))
        print(pivot.to_string())

        err = df[df['status'] == 'error']
        if not err.empty:
            has_err = True
            print(f'\n失败明细（前 20，共 {len(err)}）：')
            print(err.head(20).to_string())
    return has_err
