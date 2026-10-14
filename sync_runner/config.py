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
SLEEP_MIN, SLEEP_MAX = 1.0, 3.0

# ===== 超时 / 重试 =====
REQUEST_TIMEOUT = 60      # 单只单表超时（秒）。首次全量（1901 起点）可能超过 60s；
                          # 客户端超时不会取消服务端任务，立即重试会与服务端并发写同一 symbol
MAX_RETRY = 2             # 单只单表失败重试次数
RETRY_BACKOFF = 5.0       # 重试间隔基数（第 n 次重试前停 RETRY_BACKOFF*n 秒）

# ===== 连续失败额外退避 =====
FAIL_STREAK_LIMIT = 5     # 连续失败次数阈值
FAIL_STREAK_SLEEP = 15.0  # 额外停顿秒数

# ===== symbol 前缀白名单（过滤非股票标的）=====
MARKET_PREFIXES = ('SHSE.', 'SZSE.', 'BJSE.')

# ===== 全局 Session（连接复用）=====
SESSION = requests.Session()
