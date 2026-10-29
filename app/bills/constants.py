# -*- coding: utf-8 -*-
"""bills 归属口径常量 —— 「哪些行才能挂到估值标的」的单一真值源。

router / irs.vm_map / 前端列渲染都从这里取，避免三处各写一份白名单然后慢慢漂移。

核心规则（2026-10-29 定稿）：
纯粹按交易类别（category）判断是否可归属。只有三类真交易的行才谈得上
「这笔交易围绕哪个标的」：证券交易、期货交易、期权交易。

不可归属的类别（共 6 类）：
- 理财：国债逆回购 / OTC / TTB 等，靠 category 直接排除
- 融资融券：融资行为本身，不归属任何标的
- 收益费用：利息/手续费流水
- 出入金：资金进出流水
- '-'：配股权证/申购配号等无代码操作
- cash：现金行，资金头寸

之前版本用 category + symbol 双重判断（VIRTUAL_SYMBOLS），但实际数据中
这些不可归属类别的 symbol 不会出现在 TRADE_CATS 里，symbol 检查是冗余的。
简化为纯 category 判断后逻辑更清晰、不易漂移。
"""

# 三类真交易：只有这些行才谈得上「这笔交易围绕哪个标的」
TRADE_CATS = ('证券交易', '期货交易', '期权交易')

# 不可归属的类别 —— 与 TRADE_CATS 互补（所有非 TRADE_CATS 均不可归属）
NON_ATTACHABLE_CATS = frozenset({
    '理财', '融资融券', '收益费用', '出入金', '-', 'cash'
})

# 旧常量兼容保留（下游有引用），但不再参与 is_attachable 判断
VIRTUAL_SYMBOLS = frozenset({'RZRQ', 'SYFY', 'CRJ', 'cash'})
RZRQ_CATEGORY = '融资融券'


def is_attachable(category, symbol=None) -> bool:
    """该行能否归属到一个估值标的。

    口径：纯粹按 category 判断，只有 TRADE_CATS 三个交易类别行才参与归属。
    symbol 参数保留用于调用方签名兼容，实际不参与判断。
    """
    if not category:
        return False
    return category in TRADE_CATS


def attachable_sql(alias: str = "") -> str:
    """可归属行的 WHERE 片段（值全是硬编码常量，无注入面）。

    alias 用于多表 JOIN 时加表前缀，如 `attachable_sql('g')` → `g.category IN (...)`。
    ⚠️ 不要内联成 f-string 常量再复用——`{}` 在 f-string 里会被当成占位符。
    """
    p = f"{alias}." if alias else ""
    cats = ",".join(f"'{c}'" for c in TRADE_CATS)
    return f"{p}category IN ({cats})"


def non_attachable_sql(alias: str = "") -> str:
    """不可归属行（与 `attachable_sql` 严格互补，二者之和 = 全表）。"""
    return "NOT (" + attachable_sql(alias) + ")"


# 无前缀版本，方便直接拼在主表查询里（保持旧调用点可用）
ATTACHABLE_SQL = attachable_sql()
NON_ATTACHABLE_SQL = non_attachable_sql()

# 不可归属的成因（给前端 tooltip / 计数说明用，按 category 展示）
NON_ATTACHABLE_REASONS = {
    '理财': '理财类（国债逆回购/OTC/TTB 等），不是证券交易',
    '融资融券': '融资融券：融资行为本身，不归属任何标的',
    '收益费用': '收益费用流水：利息/手续费，不归属任何标的',
    '出入金': '出入金流水：资金进出，不归属任何标的',
    '-': '无代码操作（配股权证/申购配号等），不参与归属',
    'cash': '现金行：资金头寸，不归属任何标的',
}
