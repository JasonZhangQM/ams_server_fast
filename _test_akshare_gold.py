# -*- coding: utf-8 -*-
"""临时调试脚本：测试 akshare 黄金储备接口返回结构。验证完成后删除。"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)


def test_func(name: str, func, *args, **kwargs):
    print(f"\n=== 测试 {name} ===")
    try:
        df = func(*args, **kwargs)
        if df is None or df.empty:
            print(f"[WARN] 返回空")
            return
        print(f"[OK] shape={df.shape}")
        print(f"columns: {list(df.columns)}")
        print(f"dtypes:\n{df.dtypes}")
        print(f"\nhead:")
        print(df.head(10))
        print(f"\ntail:")
        print(df.tail(5))
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()


# 1. macro_cons_gold: 全球央行黄金储备（最可能的目标接口）
test_func("macro_cons_gold", ak.macro_cons_gold)

# 2. macro_china_foreign_exchange_gold: 中国外汇黄金
test_func("macro_china_foreign_exchange_gold", ak.macro_china_foreign_exchange_gold)

# 3. macro_china_fx_gold: 中国外管局黄金
test_func("macro_china_fx_gold", ak.macro_china_fx_gold)
