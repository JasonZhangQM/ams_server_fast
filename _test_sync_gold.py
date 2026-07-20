# -*- coding: utf-8 -*-
"""临时调试脚本：直接调用 IMF 同步函数，定位失败原因。验证完成后删除。"""
import sys
import traceback
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

# 开启 DEBUG 级别日志，便于查看 IMF 调用细节
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

# 单独提升本模块的日志级别
logging.getLogger("uvicorn.error").setLevel(logging.DEBUG)

from server_fast.app.bds.services.gold_reserve import (
    _fetch_gold_reserve_from_imf,
    upsert_gold_reserve_sql,
)


def test_fetch_single(imf_code: str = "US"):
    """测试 IMF API 单国拉取，不入库"""
    print(f"\n=== 测试 IMF API 拉取：{imf_code} ===")
    try:
        df = _fetch_gold_reserve_from_imf(imf_code, "2024-01", "2024-06")
        if df is None or df.empty:
            print(f"[WARN] 返回空 DataFrame")
        else:
            print(f"[OK] 拉取到 {len(df)} 条记录")
            print(df.head())
            print(f"\n字段类型：\n{df.dtypes}")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()


def test_upsert_single(country_code: str = "US"):
    """测试单国家 upsert 全流程"""
    print(f"\n=== 测试 upsert 全流程：{country_code} ===")
    try:
        count = upsert_gold_reserve_sql(country_code)
        print(f"[RESULT] count={count}  (-1=失败, 0=无数据, >0=成功条数)")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    # 步骤 1：仅测试 API 拉取（不入库）
    test_fetch_single("US")

    # 步骤 2：测试完整 upsert 流程
    test_upsert_single("US")
