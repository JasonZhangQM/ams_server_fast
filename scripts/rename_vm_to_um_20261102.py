# -*- coding: utf-8 -*-
"""ValueMonitor → UnderlyingMonitor 全量改名（2026-11-02，方案 C）

改名映射（长串优先，按序应用）：
  数据库层（缩写 um）：
    irs_value_monitor   → irs_underlying_monitor
    irs_vm_*            → irs_um_*（8 张从表）
    vm_symbol           → underlying_symbol（列名：irs_um_plan / bills_group_symbol）
    value_monitor(.py)  → underlying_monitor
    vm_*.py 子模块      → um_*.py
  代码层（全拼 Underlying）：
    ValueMonitor        → UnderlyingMonitor
    VmXxx（类/函数/接口）→ UnderlyingXxx
    value-monitor(s)    → underlying-monitor(s)（URL / SYNC 键）
    局部变量 vm / vm_xxx → um / um_xxx
  文案层：
    估值监测            → 标的监测（仅指本模块的语境）

用法：
    conda run -n fast311 python scripts/rename_vm_to_um_20261102.py --check   # dry-run，只统计
    conda run -n fast311 python scripts/rename_vm_to_um_20261102.py           # 执行替换
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

BASE = Path(r"C:\quant_man\mysite_quant\ams_trae")

# ---- 后端文件清单（前端另行处理）----
BACKEND_FILES = [
    "server_fast/app/irs/service/vm_review.py",
    "server_fast/app/bills/services/bill_note.py",
    "server_fast/app/bills/services/group_summary.py",
    "server_fast/app/bills/schemas.py",
    "server_fast/app/bills/router.py",
]

# ---- 替换规则（顺序敏感：长串优先，vm_symbol 必须在 vm_ 之前）----
RULES: list[tuple[str, str]] = [
    ("irs_value_monitor", "irs_underlying_monitor"),
    ("k_irs_value_monitor", "k_irs_underlying_monitor"),   # 索引名（已被上一条覆盖，防御性保留）
    ("value_monitors", "underlying_monitors"),
    ("value_monitor", "underlying_monitor"),
    ("value-monitor-hlc", "underlying-monitor-hlc"),       # SYNC_MAP 键（被下一条覆盖，防御性保留）
    ("value-monitors", "underlying-monitors"),
    ("value-monitor", "underlying-monitor"),
    ("ValueMonitor", "UnderlyingMonitor"),
    ("valueMonitor", "underlyingMonitor"),
    ("irs_vm_", "irs_um_"),
    ("VmVersion", "UnderlyingVersion"),
    ("VmResearch", "UnderlyingResearch"),
    ("VmLogic", "UnderlyingLogic"),
    ("VmInvalidate", "UnderlyingInvalidate"),
    ("VmPlan", "UnderlyingPlan"),
    ("VmReview", "UnderlyingReview"),
    ("VmStateLog", "UnderlyingStateLog"),
    ("VmTarget", "UnderlyingTarget"),
    ("VmGroup", "UnderlyingGroup"),
    ("VmTrade", "UnderlyingTrade"),
    ("VmCoverageRow", "UnderlyingCoverageRow"),
    ("VmUnmappedRow", "UnderlyingUnmappedRow"),
    ("VmUpdate", "UnderlyingUpdate"),   # GroupSymbolVmUpdate → GroupSymbolUnderlyingUpdate
    ("VmVersionSave", "UnderlyingVersionSave"),            # 已被 VmVersion 覆盖，防御性保留
    ("vm_symbol", "underlying_symbol"),
    ("vm_version", "um_version"),
    ("vm_research", "um_research"),
    ("vm_plan", "um_plan"),
    ("vm_map", "um_map"),
    ("vm_today", "um_today"),
    ("uk_vmp_symbol_seq", "uk_ump_symbol_seq"),
    ("估值监测", "标的监测"),
]

# 独立小写 vm（局部变量 / query 参数）：词边界正则
REGEX_RULES: list[tuple[str, str]] = [
    (r"\bvm\b", "um"),
]


def apply_rules(text: str) -> tuple[str, dict]:
    stats: dict = {}
    for old, new in RULES:
        n = text.count(old)
        if n:
            text = text.replace(old, new)
            stats[old] = n
    for pat, new in REGEX_RULES:
        n = len(re.findall(pat, text))
        if n:
            text = re.sub(pat, new, text)
            stats[f"regex:{pat}"] = n
    return text, stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只统计，不写回")
    args = parser.parse_args()

    for rel in BACKEND_FILES:
        path = BASE / rel
        if not path.exists():
            print(f"[MISS] {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        new_text, stats = apply_rules(text)
        if not stats:
            print(f"[SKIP] {rel}: 无命中")
            continue
        detail = ", ".join(f"{k}×{v}" for k, v in stats.items())
        print(f"[{'DRY' if args.check else 'OK'}] {rel}: {detail}")
        if not args.check and new_text != text:
            path.write_text(new_text, encoding="utf-8")

    if args.check:
        print("\n--check 模式：未写任何文件。审查上面的命中数后去掉 --check 执行。")


if __name__ == "__main__":
    main()
