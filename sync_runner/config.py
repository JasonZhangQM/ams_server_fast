# -*- coding: utf-8 -*-
"""批量同步常量 / 全局 Session。

从原 run2-批量同步.ipynb Cell 2 原样抽取（该 notebook 已于 2026-10-26 删除），单行逻辑未改。
"""

import requests

# ===== 后端 =====
API_BASE = 'http://127.0.0.1:8008/api/v1'

# ===== 同步目标 =====
TABLES = [
    'fund-balance',     # 资产负债表
    'fund-income',      # 利润表
    'fund-cashflow',    # 现金流量表
    'finance-deriv',    # 财务指标
    'daily-valuation',  # 每日估值
    'daily-mktvalue',   # 每日市值
]

# ===== 历史行情（可选，不在默认 TABLES 里）=====
# 前端「历史行情」默认不勾选：个股首次全量可达数千行/只，全市场跑量级完全不同。
# 个股走后复权、指数走不复权，类型由后端按证券信息表判定。
HISTORY_TABLE = 'history_market'

# 恒不参与 sync_at 标记的表（用户 2026-11-06 拍板）
# sync_at 的语义是「财务四表 + 两张日频表已同步」，history_market 是长周期回填：
#   1) 单跑 history_market 不应污染标记（否则次日财务表会被误判「已同步」而跳过）；
#   2) history_market 自身跳过/失败也不应阻止同标的其他表打标记。
MARK_EXCLUDED_TABLES = frozenset({HISTORY_TABLE})

# ===== 限速：每次 HTTP 请求后随机停顿（秒），避免 gm 接口锁定 =====
SLEEP_MIN, SLEEP_MAX = 0.1, 1.0

# ===== 超时 / 重试 =====
# 单只单表超时（秒）。首次全量（1901 起点）可能超过 60s；客户端超时不会取消服务端任务，
# 立即重试会与服务端并发写同一 symbol。
REQUEST_TIMEOUT = 60
# history_market（个股从上市日全量取数）耗时更长：客户端等 180s，后端 gm 调用上限 150s。
# 两者必须留出余量，否则客户端先超时会重试，而慢请求仍在服务端跑，形成堆积。
HISTORY_TIMEOUT = 180
MAX_ATTEMPTS = 3          # 单只单表总尝试次数（1 次首发 + 2 次重试）
RETRY_BACKOFF = 5.0       # 退避基数（秒）：第 n 次重试前停 RETRY_BACKOFF*2**(n-1) 秒 + 0~1s 抖动 ⇒ 5s / 10s

# ===== 连续失败额外退避 =====
FAIL_STREAK_LIMIT = 5     # 连续失败次数阈值
FAIL_STREAK_SLEEP = 15.0  # 额外停顿秒数

# ===== gm 断连熔断（单笔 fatal 只算疑似，连续 N 笔才中止整批）=====
# 单笔 fatal 的判据是启发式的（gm 错误码 + 关键词），一次连接抖动也会命中；
# 连续判定以「任一 success / no_data 即清零」为准。
FATAL_STREAK_LIMIT = 3    # 连续 N 笔疑似 gm 断连才熔断整批
FATAL_STREAK_SLEEP = 10.0 # 未达阈值时的冷却时间（秒），给终端恢复机会

# ===== symbol 前缀白名单（过滤非股票标的）=====
MARKET_PREFIXES = ('SHSE.', 'SZSE.', 'BJSE.')

# ===== 全局 Session（连接复用）=====
SESSION = requests.Session()
