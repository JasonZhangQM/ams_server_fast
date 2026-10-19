# -*- coding: utf-8 -*-
"""批量同步常量 / 全局 Session。

从 run2-批量同步.ipynb Cell 2 原样抽取，单行逻辑未改。
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

# ===== 限速：每次 HTTP 请求后随机停顿（秒），避免 gm 接口锁定 =====
SLEEP_MIN, SLEEP_MAX = 0.1, 1.0

# ===== 超时 / 重试 =====
REQUEST_TIMEOUT = 60      # 单只单表超时（秒）。首次全量（1901 起点）可能超过 60s；
                          # 客户端超时不会取消服务端任务，立即重试会与服务端并发写同一 symbol
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
