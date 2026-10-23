# -*- coding: utf-8 -*-
"""bills 归属口径常量 —— 「哪些行才能挂到估值标的」的单一真值源。

router / irs.vm_map / 前端列渲染都从这里取，避免三处各写一份白名单然后慢慢漂移。

为什么需要「不可归属」这个概念（2026-10-23 用户定）：
`bills_group_symbol` 一行不等于一个标的。资金流水行的 symbol 只是流水占位代码
（`CRJ`=出入金、`SYFY`=收益费用、`cash`=现金、`RZRQ`=融资融券），给它挂一个估值标的
毫无意义：既不会产生「该标的的成交」，还会让「还有多少行没选」永久挂着十几行
永远选不完的东西，反而淹掉真正漏配的那几行。

「理财」行（国债逆回购 204001 / TTB 等）同理不归属，靠 `category not in TRADE_CATS`
一并排除，不必单列代码。
"""

# 三类真交易：只有这些行才谈得上「这笔交易围绕哪个标的」
TRADE_CATS = ('证券交易', '期货交易', '期权交易')

# 资金流水占位代码：不是证券，永不参与归属。
# 即便某天它们被误分类进 TRADE_CATS，这里也拦住。
VIRTUAL_SYMBOLS = frozenset({'RZRQ', 'SYFY', 'CRJ', 'cash'})

# 融资融券分类：唯一会被误当交易的非交易分类，单独留个名字给报错文案用
RZRQ_CATEGORY = '融资融券'


def is_attachable(category, symbol) -> bool:
    """该行能否归属到一个估值标的（category/symbol 任一为 None 都返回 False）。"""
    if not category or not symbol:
        return False
    return category in TRADE_CATS and symbol not in VIRTUAL_SYMBOLS


def attachable_sql(alias: str = "") -> str:
    """可归属行的 WHERE 片段（值全是硬编码常量，无注入面）。

    alias 用于多表 JOIN 时加表前缀，如 `attachable_sql('g')` → `g.category IN (...)`。
    ⚠️ 不要内联成 f-string 常量再复用——`{}` 在 f-string 里会被当成占位符。
    """
    p = f"{alias}." if alias else ""
    cats = ",".join(f"'{c}'" for c in TRADE_CATS)
    syms = ",".join(f"'{s}'" for s in sorted(VIRTUAL_SYMBOLS))
    return f"{p}category IN ({cats}) AND {p}symbol NOT IN ({syms})"


def non_attachable_sql(alias: str = "") -> str:
    """不可归属行（与 `attachable_sql` 严格互补，二者之和 = 全表）。"""
    return "NOT (" + attachable_sql(alias) + ")"


# 无前缀版本，方便直接拼在主表查询里（保持旧调用点可用）
ATTACHABLE_SQL = attachable_sql()
NON_ATTACHABLE_SQL = non_attachable_sql()

# 不可归属的成因（给前端 tooltip / 计数说明用，顺序即展示顺序）
NON_ATTACHABLE_REASONS = {
    '融资融券': 'RZRQ',          # 融资只是融资行为，name 不承担归属含义
    '出入金': 'CRJ',
    '收益费用': 'SYFY',
    'cash': 'cash',
}
