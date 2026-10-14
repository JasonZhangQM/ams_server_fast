# -*- coding: utf-8 -*-
"""批量执行器 + 结果汇总。

从 run2-批量同步.ipynb Cell 5-6 原样抽取，全局变量 RESULTS 改为参数传入/返回。
"""

import time
import random

import pandas as pd

from .config import SLEEP_MIN, SLEEP_MAX, FAIL_STREAK_LIMIT, FAIL_STREAK_SLEEP
from .sync import sync_one, mark_synced


def run_tasks(tasks, mark_synced_when_done=True):
    '''执行 (symbol, table) 任务列表，返回结果记录列表。

    按 symbol 分组推进：某个 symbol 的全部表都返回 success / no_data 时，
    调 /bds/sync/mark-synced 把 bds_symbol_info.sync_at 写为当日，
    下次运行即可用 EXCLUDE_SYNCED_TODAY=True 直接跳过。

    不做批次断点记录：中断后直接重跑整批即可，同步接口按增量起点
    MAX(date)+1 推进，已同步的表返回 no_data，幂等安全。
    '''
    results = []
    total = len(tasks)
    print(f'任务总数 {total}')
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
    for sym, tables in by_symbol.items():
        sym_statuses = []
        for table in tables:
            i += 1
            res = sync_one(sym, table)
            results.append({'symbol': sym, 'table': table, **res})
            sym_statuses.append(res['status'])

            if res['status'] in ('success', 'no_data'):
                fail_streak = 0
            else:
                fail_streak += 1

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

        # 该 symbol 全部表都非 error → 标记当日已同步
        if mark_synced_when_done and all(st in ('success', 'no_data') for st in sym_statuses):
            mk = mark_synced(sym)
            if mk['status'] == 'ok':
                marked += 1
            else:
                print(f'  [警告] {sym} 标记 sync_at 失败：{mk["message"][:60]}')

    print(f'完成，用时 {time.time() - t0:.0f}s | 已标记当日同步 {marked} 只')
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
