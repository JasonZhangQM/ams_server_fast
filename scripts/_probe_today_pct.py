# -*- coding: utf-8 -*-
"""探针：今日页 percentiles() 个股分支验证（2026-11-03）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server_fast.app.irs.service.um_today import percentiles

pct = percentiles()
for (sym, metric), v in sorted(pct.items()):
    print(f"{sym:14s} {metric:8s} 10y分位 = {v}")
print(f"共 {len(pct)} 项")
