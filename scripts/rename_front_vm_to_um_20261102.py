# -*- coding: utf-8 -*-
"""前端 ValueMonitor → UnderlyingMonitor 改名（2026-11-02，方案 C）

排除：router/elegant/（build 自动生成）、typings/elegant-router.d.ts（同）、
      plugins/app.ts（Vue errorHandler 的 vm 惯例参数，与本改名无关）。

用法：
    conda run -n fast311 python scripts/rename_front_vm_to_um_20261102.py --check
    conda run -n fast311 python scripts/rename_front_vm_to_um_20261102.py
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

BASE = Path(r"C:\quant_man\mysite_quant\ams_trae")

FRONTEND_FILES = [
    "web-soybean/src/locales/langs/en-us.ts",
    "web-soybean/src/locales/langs/zh-cn.ts",
    "web-soybean/src/service/api/bills.ts",
    "web-soybean/src/service/api/irs.ts",
    "web-soybean/src/store/modules/tab/index.ts",
    "web-soybean/src/typings/api/bills.d.ts",
    "web-soybean/src/typings/api/irs.d.ts",
    "web-soybean/src/views/bills/bills/index.vue",
    "web-soybean/src/views/bills/bills/modules/BillNotePanel.vue",
    "web-soybean/src/views/bills/group-symbols/index.vue",
    "web-soybean/src/views/irs/underlying-monitors/index.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/DetailDrawer.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/LogicTab.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/OverviewTab.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/planFormat.ts",
    "web-soybean/src/views/irs/underlying-monitors/modules/PlanTab.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/ResearchVersionPanel.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/ReviewTab.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/VersionTab.vue",
    "web-soybean/src/views/irs/underlying-monitors/modules/UnderlyingDataTable.vue",
    "web-soybean/src/views/today/index.vue",
]

# 字符串规则（顺序敏感：长串/特异串优先）
RULES: list[tuple[str, str]] = [
    ("irs_value_monitor", "irs_underlying_monitor"),
    ("value_monitor", "underlying_monitor"),
    ("value-monitors", "underlying-monitors"),
    ("value-monitor", "underlying-monitor"),
    ("ValueMonitor", "UnderlyingMonitor"),
    ("valueMonitor", "underlyingMonitor"),
    ("irs_vm_", "irs_um_"),
    ("vm_symbol", "underlying_symbol"),
    ("updateGroupSymbolVm", "updateGroupSymbolUnderlying"),
    ("GroupSymbolVm", "GroupSymbolUnderlying"),
    ("symbols/${id}/vm", "symbols/${id}/um"),   # PATCH 归属路由 URL
    ("估值监测", "标的监测"),
]

# 正则规则（标识符形态）
REGEX_RULES: list[tuple[str, str]] = [
    (r"Vm(?=[A-Z])", "Underlying"),     # VmPlan → UnderlyingPlan；fetchVmGroups → fetchUnderlyingGroups（无词边界，覆盖驼峰中段）
    (r"\bvm(?=[A-Z])", "um"),           # vmId/vmList/vmOptions/vmSymbol/vmNameMap → umXxx
    (r"\bvm_", "um_"),                  # 防御：vm_map 等前缀（不吞后续词）
    (r"\bvm\b", "um"),                  # query 参数 vm、局部变量
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

    for rel in FRONTEND_FILES:
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
        print("\n--check 模式：未写任何文件。")


if __name__ == "__main__":
    main()
