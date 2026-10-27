# -*- coding: utf-8 -*-
"""期权衍生字段计算 —— 全项目唯一实现（2026-10-27 从 irs OptionMonitor 钩子迁入）。

为什么放 bds：`bds_option_market` 与 `irs_option_monitor` 两张表都要算同一组衍生字段
（atm_i / value_i / value_t / ratio_t / ratio_i / ratio_t_y / ratio_i_y）。
模块依赖只允许向下（irs → bds），所以唯一实现放 bds，irs 只准委托、不准再抄一份。

为什么是纯函数（不 import 任何模型）：bds/irs 两边的 `models.py` 都要在模型事件钩子
里调它，若它反过来 import 模型会造成循环导入。

口径（照搬自原 `irs.models._compute_option_monitor`，2026-10-27 起为全项目口径）：

- 输入字段（由调用方保证 target 上已存在）：
    price_strike 行权价 / price_ud 标的价 / price 期权价 / option_type call|put / days_left 剩余自然日
- `price_ud` 或 `price` 任一为 None ⇒ 七个衍生字段**全部清空**，绝不保留旧值：
  否则改上游后页面会显示"按新输入算的"旧值冒充新值（原 irs v3 §5.3 结论）。
- `atm_i`    = (行权价 / 标的价 − 1) × 100   —— 行权价偏离标的的幅度，正=行权价在标的上方
- `value_i`  内在价值：call = max(0, 标的价 − 行权价)；put = max(0, 行权价 − 标的价)
- `value_t`  = price − value_i，**不截 0**（深度实值合约为负是真实市场现象，2026-10-27
  用户拍板 bds 放弃原来的 max(0,·) 截断，与 irs 统一）
- `ratio_t` / `ratio_i` = value_t / value_i 占行权价的百分比
- `ratio_t_y` / `ratio_i_y` = 年化 = ratio × 365 / days_left；days_left=0 时置 0 防除零
- days_left 的**基准日由调用方决定**（本函数不管）：
    - irs_option_monitor：到期日 − today（监测口径，随时点变化）
    - bds_option_market：到期日 − trade_date（快照 as-of 口径，当日快照下两者相等）
"""

from decimal import Decimal

# 七个衍生字段名（清空/校验用，避免各处散落字符串列表）
DERIVED_FIELDS = (
    "atm_i", "value_i", "value_t", "ratio_t", "ratio_i", "ratio_t_y", "ratio_i_y",
)

_D1 = Decimal("1")
_D100 = Decimal("100")
_D365 = Decimal("365")


def compute_option_derived(target) -> None:
    """在 target 上计算/清空七个期权衍生字段（原地修改，无返回值）。

    target 需已有属性：price_strike、price_ud、price、option_type（call/put）、days_left。
    bds 侧由模型钩子按 contract_id 查字典补 option_type/days_left 后调用；
    irs 侧字段本就在表上，钩子先算 days_left 再委托本函数。
    """
    # 价格缺失：宁可全空也不能留上一版算出的旧值（清空语义见模块 docstring）
    if target.price_ud is None or target.price is None:
        for f in DERIVED_FIELDS:
            setattr(target, f, None)
        return

    strike = target.price_strike
    ud = target.price_ud

    # 平值度：行权价相对标的价的偏移百分比
    target.atm_i = (strike / ud - _D1) * _D100

    # 内在价值：认购/认沽方向不同（虚值侧一律 0）
    if target.option_type == "call":
        target.value_i = Decimal("0") if strike > ud else (ud - strike)
    else:
        target.value_i = Decimal("0") if strike < ud else (strike - ud)

    # 时间价值 = 期权价 − 内在价值（可为负，不截 0 —— 2026-10-27 拍板）
    target.value_t = target.price - target.value_i

    # 占行权价百分比
    target.ratio_t = (target.value_t / strike) * _D100
    target.ratio_i = (target.value_i / strike) * _D100

    # 年化：days_left=0（到期日当天）置 0 防除零
    if target.days_left:
        target.ratio_t_y = target.ratio_t * _D365 / target.days_left
        target.ratio_i_y = target.ratio_i * _D365 / target.days_left
    else:
        target.ratio_t_y = Decimal("0")
        target.ratio_i_y = Decimal("0")
