# -*- coding: utf-8 -*-
"""定时运行脚本（非初始化脚本）。

按原 `server_dj/run.py` 的执行顺序依次调用三个应用的 service 函数，
对已存在的 `ams_trae` 数据库做增量数据更新。

- 不创建数据库、不建表（ams_trae 已建表并已导入基础数据）
- 不读写原 `ams` 数据库
- 入口：`python -m server_fast.run`，供外部调度（如 Windows 计划任务）调用
"""
from datetime import datetime

from server_fast.app.bds.service import (
    insert_trade_date_em_sql,
    upsert_symbol_info_excel_sql,
)
from server_fast.app.bills.service import (
    cash_update_group_sql,
    del_old_symbol_group_sql,
    insert_bill_all_excel_sql,
    update_symbol_bill_sql,
    upsert_daily_acc_sql,
    upsert_daily_all_sql,
    upsert_daily_cash_group_sql,
    upsert_daily_value_group_em_sql,
    upsert_group_acc_sql,
    upsert_group_cash_sql,
    upsert_group_profit_sql,
    upsert_profit_group_sql,
)
from server_fast.app.irs.config import Config as IrsCfg
from server_fast.app.irs.models import (
    SymbolDiscount,
    SymbolUnderlying,
    SymbolValue,
)
from server_fast.app.irs.service import (
    symbol_option_update_self_orm,
    upsert_discount_em_sql,
    upsert_model_excel_sql,
)


def _build_steps():
    """构造待执行的步骤列表。

    每个元素为 (描述, 可调用对象)，按原 server_dj/run.py 顺序排列。
    使用函数封装以便延迟绑定导入，避免模块加载阶段的副作用。
    """
    return [
        ("交易日历数据", insert_trade_date_em_sql),
        ("导入股票信息", upsert_symbol_info_excel_sql),
        ("导入账单数据", insert_bill_all_excel_sql),
        ("更新账单中的代码", update_symbol_bill_sql),
        ("删除汇总表中的旧代码", del_old_symbol_group_sql),
        ("资金汇总", upsert_group_cash_sql),
        ("收益汇总", upsert_group_profit_sql),
        ("收益试算", upsert_profit_group_sql),
        ("资金试算", cash_update_group_sql),
        ("交易日结", upsert_daily_value_group_em_sql),
        ("资金日结", upsert_daily_cash_group_sql),
        ("账户日结、月结、季结、年结", upsert_daily_acc_sql),
        ("月结、季结、年结", upsert_daily_all_sql),
        ("账户汇总", upsert_group_acc_sql),
        ("估值数据导入", lambda: upsert_model_excel_sql(
            IrsCfg.FOLDER_SYMBOL_VALUE, SymbolValue)),
        ("期权标的导入", lambda: upsert_model_excel_sql(
            IrsCfg.FOLDER_OPTION, SymbolUnderlying)),
        ("贴水标的(连续合约)导入", lambda: upsert_model_excel_sql(
            IrsCfg.FOLDER_SYMBOL_CON, SymbolDiscount)),
        ("连续合约信息完善(基础数据+主力标志)", upsert_discount_em_sql),
        ("SymbolOption 更新到期日", symbol_option_update_self_orm),
    ]


def run_all():
    """按顺序执行所有定时任务步骤。

    单步失败不中断后续步骤（与原脚本顺序执行语义一致），仅在末尾汇总失败数。
    """
    print(f"========== 定时任务启动：{datetime.now():%Y-%m-%d %H:%M:%S} ==========")
    steps = _build_steps()
    fail_count = 0
    for idx, (desc, func) in enumerate(steps, start=1):
        print(f"\n[{idx}/{len(steps)}] {desc}")
        try:
            func()
        except Exception as e:
            # 单步失败不中断后续步骤，记录错误后继续
            fail_count += 1
            print(f"****** 步骤失败：{desc} -> {type(e).__name__}: {e}")
    print(f"\n========== 定时任务结束：{datetime.now():%Y-%m-%d %H:%M:%S} ==========")
    print(f"总计 {len(steps)} 步，成功 {len(steps) - fail_count} 步，失败 {fail_count} 步")
    return fail_count


if __name__ == "__main__":
    run_all()
