# -*- coding: utf-8 -*-
"""归属哨兵（只读）：验证 bills_group_symbol.vm_symbol 的硬约束。

用法：conda run -n fast311 python scripts/verify_vm_map.py

断言（对应方案 v3 §十 的 16/17/18 + 2026-10-23 「资金流水行不归属」补充）：
1. 不可归属行（RZRQ/CRJ/SYFY/cash + 理财逆回购）vm_symbol 必须为 NULL；
2. vm_symbol 只能指向 irs_value_monitor 里存在的 symbol（外键兜底 + 人话校验）；
3. 同代码自动命中 = 126 笔（由数据决定，稳定）；人工映射笔数只做合理性检查
   ——**不写死具体数字**，因为人会在页面上持续补归属，写死等于每次补完就红；
4. `vm_symbol` 不在 `_DERIVED_COLS` 里（规则二，防止重算清空）；
5. 三态计数之和 = total，且未归属清单里不出现资金流水行。

口径唯一来源：`server_fast.app.bills.constants`，本脚本不再自己写白名单。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"C:\quant_man\mysite_quant\ams_trae")

from sqlalchemy import text

from server_fast.common.db import SessionLocal
from server_fast.app.bills.constants import attachable_sql, non_attachable_sql, VIRTUAL_SYMBOLS
from server_fast.app.bills.services.group_summary import _DERIVED_COLS

EXPECTED_SAME_CODE = 126

fails = []


def check(ok: bool, msg: str) -> None:
    print(f"  {'OK ' if ok else '!! '} {msg}")
    if not ok:
        fails.append(msg)


with SessionLocal() as db:
    print("=" * 74)
    print("归属哨兵 verify_vm_map")
    print("=" * 74)

    print("\n[1] 不可归属行必须不归属（资金流水 + 理财逆回购）")
    bad_rows = db.execute(text(f"""
        SELECT id, category, symbol, vm_symbol
        FROM bills_group_symbol
        WHERE {non_attachable_sql()} AND vm_symbol IS NOT NULL
    """)).mappings().all()
    check(len(bad_rows) == 0, f"不可归属行 vm_symbol 全为 NULL（违规 {len(bad_rows)} 行）")

    hit = db.execute(text(f"""
        SELECT category, symbol, COUNT(*) n FROM bills_group_symbol
        WHERE symbol IN ('RZRQ','SYFY','CRJ','cash') GROUP BY category, symbol
    """)).mappings().all()
    got = {r["symbol"] for r in hit}
    check(got == set(VIRTUAL_SYMBOLS),
          f"四个虚拟代码全部在库且被排除（命中 {sorted(got)}）")

    print("\n[2] vm_symbol 只能指向主表存在的 symbol")
    bad = db.execute(text("""
        SELECT g.id, g.category, g.symbol, g.vm_symbol
        FROM bills_group_symbol g
        LEFT JOIN irs_value_monitor v ON v.symbol = g.vm_symbol
        WHERE g.vm_symbol IS NOT NULL AND v.symbol IS NULL
    """)).mappings().all()
    check(len(bad) == 0, f"无悬挂引用（{len(bad)} 条）")

    print("\n[3] 覆盖笔数")
    same = db.execute(text(f"""
        SELECT COUNT(*) FROM bills_bill b
        JOIN irs_value_monitor v ON v.symbol = b.symbol
        WHERE {attachable_sql('b')}
    """)).scalar()
    mapped = db.execute(text(f"""
        SELECT COUNT(*) FROM bills_bill b
        JOIN bills_group_symbol g ON g.category=b.category AND g.symbol=b.symbol
        WHERE g.vm_symbol IS NOT NULL AND g.vm_symbol <> b.symbol
          AND {attachable_sql('b')}
    """)).scalar()
    total_denom = db.execute(text(f"""
        SELECT COUNT(*) FROM bills_bill b WHERE {attachable_sql('b')}
    """)).scalar()
    print(f"     同代码自动命中 {same}，人工映射 {mapped}，合计 {same + mapped}")
    print(f"     可归属基数 {total_denom} 笔（已剔除全部资金流水）→ 覆盖率 {(same + mapped) / total_denom * 100:.1f}%")
    check(same == EXPECTED_SAME_CODE, f"同代码命中 = {EXPECTED_SAME_CODE}（数据决定，应恒定）")
    # 人工映射随人持续补归属而增长，只校验区间合理性与不重复计数，不写死数字
    check(0 <= mapped <= total_denom, f"人工映射笔数在合理区间（{mapped} ≤ {total_denom}）")
    check(same + mapped <= total_denom, f"两类命中之和不超过基数（{same + mapped} ≤ {total_denom}）")

    print("\n[4] vm_symbol 不得进 _DERIVED_COLS")
    check("vm_symbol" not in _DERIVED_COLS, f"白名单 = {_DERIVED_COLS}")

    print("\n[5] 三态计数（之和必须 = total）")
    cnt = db.execute(text(f"""
        SELECT
          SUM(CASE WHEN {non_attachable_sql()} THEN 1 ELSE 0 END) AS excluded,
          SUM(CASE WHEN {attachable_sql()} AND (vm_symbol IS NOT NULL
                    OR symbol IN (SELECT symbol FROM irs_value_monitor))
                   THEN 1 ELSE 0 END) AS mapped,
          SUM(CASE WHEN {attachable_sql()} AND vm_symbol IS NULL
                    AND symbol NOT IN (SELECT symbol FROM irs_value_monitor)
                   THEN 1 ELSE 0 END) AS unmapped,
          COUNT(*) AS total
        FROM bills_group_symbol
    """)).mappings().first()
    print(f"     已归属 {cnt['mapped']} / 未归属 {cnt['unmapped']} / 不参与 {cnt['excluded']} = {cnt['total']}")
    check(cnt["mapped"] + cnt["unmapped"] + cnt["excluded"] == cnt["total"],
          "三态之和 = total")

    print("\n[6] 未归属合约行（可补的部分，Top 15）")
    un = db.execute(text(f"""
        SELECT g.category, g.symbol, g.count,
               (SELECT COUNT(*) FROM bills_bill b
                 WHERE b.category=g.category AND b.symbol=g.symbol) AS bill_rows
        FROM bills_group_symbol g
        WHERE g.vm_symbol IS NULL AND {attachable_sql('g')}
          AND g.symbol NOT IN (SELECT symbol FROM irs_value_monitor)
        ORDER BY bill_rows DESC, g.symbol LIMIT 15
    """)).mappings().all()
    for r in un:
        print(f"     {r['category']:<8} {r['symbol']:<24} 成交 {r['bill_rows']:>3} 笔")
    check(all(r["symbol"] not in VIRTUAL_SYMBOLS for r in un),
          "未归属清单里无资金流水行")
    print(f"     共 {cnt['unmapped']} 行未归属")

print("\n" + "=" * 74)
print("结果:", "全部通过" if not fails else f"{len(fails)} 项失败")
sys.exit(1 if fails else 0)
