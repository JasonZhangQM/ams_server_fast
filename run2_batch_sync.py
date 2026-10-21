# -*- coding: utf-8 -*-
"""批量同步 CLI 入口。

本 CLI 是批量同步的唯一入口（此前的 run2-批量同步.ipynb 已废弃，仓库内不再存在）。
核心逻辑（限速 / 重试 / 连续失败退避 / 当日标记 / 增量幂等）集中在 sync_runner 包，
后端后台线程（POST /bds/run/batch-sync）与本 CLI 共用同一套，避免维护两份同步逻辑。

依赖：
  1. 后端已启动：conda run -n fast311 uvicorn server_fast.main:app --host 0.0.0.0 --port 8008
  2. gm 终端已登录（否则同步接口返回 no_data / error）
  3. 当前工作目录为 server_fast/（sync_runner 包在此目录下）

退出码：
  0 = 全部 success / no_data
  1 = 有 error 残留

========================================================================
全部用法示例
========================================================================

--- 全市场 ---
  # 每天跑一次，跳过今天已同步的标的（靠 bds_symbol_info.sync_at 去重）
  python run2_batch_sync.py all --exclude-synced-since 2026-10-19

  # 跨夜续跑：上一轮跑到一半就断了，选「昨天」把昨天跑完的一并剔除，只补更早的
  python run2_batch_sync.py all --exclude-synced-since 2026-10-18

  # 全市场试跑前 10 只（验证接口通不通，不写 sync_at 避免被标记）
  python run2_batch_sync.py all --max 10 --no-mark-synced

--- 退市口径（--trading-status，默认 trading=仅在市）---
  作用范围：all / industry / index 三个 scope。
  ⚠️ explicit 手写清单**不受此参数约束**（填什么跑什么，见下）。

  # 默认：不扫退市股（推荐，与网页端「退市状态=仅在市」一致）
  python run2_batch_sync.py all --exclude-synced-since 2026-10-19

  # 全部：含 338 只已退市股（慎用，会比默认多 2,000+ 个任务）
  python run2_batch_sync.py all --trading-status all

  # 仅已退市：用于退市股历史数据回补（首次全量、真打 gm、耗时极长）
  #               勿配 --mark-synced（会把退市股计入「当日已同步」统计）
  python run2_batch_sync.py all --trading-status delisted --no-mark-synced --max 5

  # 手写清单不受退市口径约束：600001（邯郸钢铁）已退市，仍然照跑
  python run2_batch_sync.py explicit --symbols SHSE.600001

--- 指数成分股 ---
  # 沪深 300 全部成分 + 跳过当日已同步
  python run2_batch_sync.py index --code SHSE.000300 --exclude-synced-since 2026-10-19

  # 科创 50 前 5 只试跑
  python run2_batch_sync.py index --code SHSE.000688 --max 5

  # 深证成指前 20 只
  python run2_batch_sync.py index --code SZSE.399001 --max 20

--- 指定行业 ---
  # 银行行业全部
  python run2_batch_sync.py industry --name 银行

  # 银行 + 只跑财报 3 张表（跳过每日估值 / 市值）
  python run2_batch_sync.py industry --name 银行 --tables fund-balance fund-income fund-cashflow

--- 手写清单 ---
  # 茅台 + 平安银行 + 招商银行
  python run2_batch_sync.py explicit --symbols SHSE.600519 SZSE.000001 SZSE.000002

  # 单只强刷财报（调试用，不写 sync_at 避免被误标为已同步）
  python run2_batch_sync.py explicit --symbols SHSE.600519 --no-mark-synced

--- 失败后怎么重跑 ---
  直接原命令重跑 + 加 --exclude-synced-since <当天日期>：
  成功的 symbol 已被 sync_at 标记，会被自动跳过；
  error 的 symbol 没写 sync_at，下次自然被 resolve_symbols 挑出来重跑。
  若任务跨了夜（昨晚跑的），把日期选成昨天，才能把昨晚跑完的也跳过。

  python run2_batch_sync.py all --exclude-synced-since 2026-10-19

========================================================================
子命令与 scope 对照
========================================================================

子命令        | scope       | symbol 来源                                | 受 --trading-status 约束
------------ | ----------- | ------------------------------------------ | ----------------------
all          | all         | bds_symbol_info 全市场股票                  | 是
index        | index       | bds_index_constituents 成分股（--code 可多个 | 是
             |             | 传，多指数取成分**合集**）                   |
industry     | industry    | bds_symbol_info 按 industry 过滤            | 是
explicit     | explicit    | CLI 参数 --symbols 手写清单                 | **否**

========================================================================
同步状态三态（后端接口 body.status）
========================================================================

success  → 有新数据，写入数据库
no_data  → 无新数据（MAX(date)+1 起点以后没增量），或 gm 未启动
error    → 调用失败（HTTP 异常 / gm 锁接口 / 服务端抛异常）

========================================================================
增量 / 幂等安全约定（后端实现，这里只是调用方）
========================================================================

  - 财报：按 rpt_date 增量（MAX(rpt_date)+1），带 pub_date 保留策略（旧发布不覆盖新发布）
  - 每日估值 / 市值：按 trade_date 增量（MAX(trade_date)+1）
  - 中断后直接重跑整批即可：已完成的表返回 no_data，不重复写库

========================================================================
当日标记（MARK_SYNCED_WHEN_DONE 语义）
========================================================================

  - 一个 symbol 的全部目标表都返回 success / no_data 才算"当日已同步"
  - 标记写 bds_symbol_info.sync_at = 服务器本地当日
  - error 不标记（防止 gm 未启动时误标）
  - 下次用 --exclude-synced-since 2026-10-19 跳过
========================================================================
"""

import argparse
import sys
from datetime import date

# sync_runner 是批量同步公共包，config / api / sync / executor 四个模块
# 全部通过 __init__.py re-export，这里按需引入需要直接调用的符号
from sync_runner import (
    TABLES,          # 默认同步 6 张表（fund-balance / fund-income / fund-cashflow / finance-deriv / daily-valuation / daily-mktvalue）
    resolve_symbols, # 按 scope 解析 symbol 列表（支持 all / explicit / industry / index + 当日去重）
    run_tasks,       # 执行 (symbol, table) 任务列表，带限速 + 连续失败退避 + 当日标记
    summarize,       # 打印 status 分布 / pivot / 失败明细；返回 bool 表示是否有 error
)


def build_parser():
    """构建顶层 argparse。

    4 个子命令：all / index / industry / explicit → 普通同步。
    """
    p = argparse.ArgumentParser(description='批量同步 bds 单只接口')
    sub = p.add_subparsers(dest='scope', required=True)

    def _common(sp):
        sp.add_argument('--tables', nargs='+', default=None,
                        help='覆盖默认表列表（默认全跑 6 张）。例：--tables fund-balance fund-income')
        sp.add_argument('--trading-status', choices=['trading', 'delisted', 'all'], default='trading',
                        help='退市口径: trading=仅在市(默认) / delisted=仅已退市 / all=全部。'
                             '仅作用于 all/industry/index；explicit 手写清单不受此参数约束')
        sp.add_argument('--max', type=int, default=0,
                        help='只取前 N 只 symbol（0=不限）。试跑用，避免一次性打太多 gm 请求')
        sp.add_argument('--exclude-synced-since', default=None, metavar='YYYY-MM-DD',
                        help='剔除 sync_at >= 该日期 的标的（含该日）；不传=不跳过。'
                             '选今天=只跑今日未同步，选昨天=接着昨天进度续跑（跨夜续跑）')
        sp.add_argument('--no-mark-synced', action='store_true',
                        help='一个 symbol 全部表跑完后不写 sync_at 标记（调试用，防止误标导致下次被跳过）')

    pa = sub.add_parser('all', help='全市场 bds_symbol_info 股票')
    _common(pa)

    pi = sub.add_parser('index', help='指数成分股（多个 --code 取成分合集）')
    pi.add_argument('--code', nargs='+', required=True,
                    help='指数代码，可多个（空格分隔）。例：--code SHSE.000300 SZSE.399006 '
                         '⇒ 各取自己最新快照的成分股求合集再同步')
    _common(pi)

    pind = sub.add_parser('industry', help='指定行业')
    pind.add_argument('--name', required=True, help='行业名，如 银行')
    _common(pind)

    pe = sub.add_parser('explicit', help='手写清单')
    pe.add_argument('--symbols', nargs='+', required=True, help='symbol 列表，空格分隔')
    _common(pe)

    return p


def main():
    """CLI 主入口。parse_args → resolve_symbols → run_tasks → summarize → 退出码。

    返回值直接作为 sys.exit 入参：
      0 = 全部 success / no_data
      1 = 有 error 残留
    """
    args = build_parser().parse_args()

    # 日期参数校验：argparse 的 type= 报错信息不够明确，这里手写提示
    since = None
    if args.exclude_synced_since:
        try:
            since = date.fromisoformat(args.exclude_synced_since)
        except ValueError:
            print(f'ERROR: --exclude-synced-since 格式应为 YYYY-MM-DD，收到: {args.exclude_synced_since}')
            sys.exit(2)

    syms = resolve_symbols(
        args.scope,
        explicit=getattr(args, 'symbols', None),
        industry=getattr(args, 'name', ''),
        index_code=getattr(args, 'code', ''),
        max_symbols=args.max,
        exclude_synced_since=since,
        trading_status=args.trading_status,
    )

    # --tables 传了就用，没传就用 sync_runner.TABLES 默认值（6 张表全跑）
    table_list = args.tables or TABLES
    print('待同步表:', table_list)

    # 笛卡尔积生成任务列表，保持同一 symbol 的多张表连续（便于整只完成后写 sync_at）
    tasks = [(s, t) for s in syms for t in table_list]

    # --no-mark-synced 时不写 sync_at 标记，防止误标
    results = run_tasks(tasks, mark_synced_when_done=not args.no_mark_synced)

    # summarize 打印状态分布 + pivot + 失败明细；返回是否有 error
    has_err = summarize(results)
    return 1 if has_err else 0


if __name__ == '__main__':
    sys.exit(main())
