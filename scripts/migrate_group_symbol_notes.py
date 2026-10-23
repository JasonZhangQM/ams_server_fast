# -*- coding: utf-8 -*-
"""旧笔记迁移（bills_group_symbol -> irs_vm_* / bills_bill_note）+ 档位版本 + 归属回填

用法：
    conda run -n fast311 python scripts/migrate_group_symbol_notes.py            # dry-run
    conda run -n fast311 python scripts/migrate_group_symbol_notes.py --apply    # 写库

硬要求（方案 v3 §五）：
1. 默认 dry-run，逐条打印去向；
2. 执行顺序：先搬 theme -> 再搬文本 -> 最后才允许 ddl_vm_v3.py --stage 2 删列；
3. bills_bill_note.content_at 用原 note_at，保住"当时写的"语义；
4. 全部幂等（唯一键 upsert），可重复执行。

两处与方案标注不同、按**实质内容**处理的差异（已核对原文）：
- 滚动调仓理由（"2026.10.22 滚动调仓至 au2611P840"）写在 **au2611P880.review**，不是 au2612.review
  —— 按内容拆给两笔成交（2919 平 880 / 2920 开 840），并订正原文月份笔误；
- au2612.review 是**持仓阶段记录**（浮亏 −27,821、四条证伪未触发），不是决策理由
  —— 并入 GC00Y 的 research_log，不制造一个没有新结论的 10-22 版本。
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pymysql


def load_env() -> dict:
    env = {}
    for line in Path(r"C:\quant_man\mysite_quant\ams_trae\server_fast\.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()

# ------------------------------------------------------------------ 迁移内容
# theme：合约层 theme 即将删除，先落到标的层（513100 不在标的池 -> 写进它的交易笔记正文首行）
THEME_TO_VM = {"GC00Y": "黄金"}

OBJECT_TYPE = {
    # ⚠️ 2026-10-23 用户要求删除主表 object_type 列，本字典只作历史留档，不再回写。
    # 原值：GC00Y 商品 / SPX·000300·000905 指数 / 600036·600900·000333·300750 股权
    "GC00Y": "商品",
    "SPX": "指数",
    "SHSE.000300": "指数",
    "SHSE.000905": "指数",
    "SHSE.600036": "股权",
    "SHSE.600900": "股权",
    "SZSE.000333": "股权",
    "SZSE.300750": "股权",
}

RESEARCH = {
    "symbol": "GC00Y",
    "research_date": date(2026, 10, 12),
    "thesis": "黄金的定价锚已从「实际利率」换成「央行购金+财政贬值」：买它不是买避险情绪，而是买美元信用体系的对冲。",
    "conclusion": "持有",
    "max_risk": (
        "叙事消退：央行购金转净卖出或财政赤字收敛，定价锚重新回到实际利率，届时高位建立的头寸没有支撑；"
        "其次是长期横盘的时间成本（10-22 已浮亏 3.06%）。"
    ),
    "expectation_gap": (
        "市场仍用「高利率压黄金」的旧模型定价（2016-21 年金价与 10Y 美债月度相关性 −0.82）；"
        "未定价的是 2022 年后相关性翻正（+0.53）背后的结构性买盘——央行对价格不敏感，"
        "2026H1 全球净购 345 吨、中国连续 23 个月增持且「大跌大买」。"
    ),
    "pre_condition": (
        "组合层面：含股票与股指期货整体浮亏较重、增量资金有限，加仓只能靠已有头寸滚动，不加净敞口（10-22 阶段记录）。"
    ),
    "next_review": date(2026, 11, 6),
    "research_note": "黄金深度研究_2026-10-12",
    "research_brief": (
        "结论：持有（不新增净敞口）。三条核心假设：①定价锚换挡——金价与 10Y 美债收益率月度相关性由 2016-21 年 −0.82 "
        "翻转为 2022 年后 +0.53，「高利率压黄金」旧模型失效；②央行是价格不敏感的结构性买家——2026H1 全球净购 345 吨，"
        "中国连续 23 个月增持至 7,747 万盎司，且大跌大买（3 月 16 万 → 9 月 74 万盎司）；③财政贬值交易是长期燃料——"
        "美国债务 >40 万亿、年利息 >1.17 万亿。建仓时的自知：价格偏贵，定位是「已持头寸可持有」，不是「看好就重仓」。"
        "当前状态（2026-10-22）：au2612 多头 1 手、本合约成本 909.26，浮亏 −27,821（−3.06%）；四条证伪条件均未触发；"
        "价格距「低」档仅 +1.8%。"
    ),
    "research_log": (
        "【持仓沿革】本持仓非主动建仓：由 SHEX.au2606P1040 卖 put 被行权被动持有，初始成本 1040，随后滚动换月至 au2612。"
        "三次向下滚仓把成本线写成 1040 → 998.82 → 880.94 → 909.26，产生约 −13.96 万已实现亏损。"
        "⚠️ 因此本合约成本 909.26 只代表本合约，全链真实回本价 = 1002.6 元/克（国际金约 4,620，已高于五档「中」4,500），"
        "复盘时不要用被重置的成本自我安慰。\n\n"
        "【研究过程】三条假设全部来自《黄金深度研究_2026-10-12》，未换锚。假设 1（相关性翻正）是可复核的统计事实；"
        "假设 2（央行买盘）依赖季度数据，频率低；假设 3（财政贬值）是长期背景，短期不可证伪。"
        "结论强度：假设 1 > 假设 2 > 假设 3，故仓位定位为「已持有」，而非「看好就重仓」。\n\n"
        "【阶段记录 2026-10-22】浮亏 −27,821（−3.06%）。9-28 建仓后 16 个交易日内价格从 909.26 回落至 881.46，"
        "属研究已提示的「高位横盘 / 均值回归」情景，四条证伪条件均未触发。当前价距「低」档仅 +1.8%。"
        "黄金逻辑交易累计亏损 12 万（含期权权利金抵消、期货持仓亏损）。\n\n"
        "【迁移说明】整理自 bills_group_symbol 旧笔记（SHEX.au2612 行，2026-10-22 19:28 写入），原文备份于 "
        "backup/vm_v3_20261023/bills_group_symbol.json。"
    ),
}

# 计划委托：原文 4 条（2026-10-22 19:28 写入）落成 5 条结构化计划。
# trade_symbol 一律使用**真实存在**的合约，不臆造行权价；"虚值 5%+ 滚动卖 put/call"的具体行权价
# 由 reason 承载（下单时按当时价格选），否则会写出不存在的合约代码。
PLANS = [
    dict(seq=1, b_s="卖", o_c="开", trigger_op="lte", trigger_ref="pp_l", trigger_price=None,
         trade_symbol="SHEX.au2612P840",
         reason="「低」档附近：每月滚动卖 put（虚值 5% 以上）等待被动加仓；行权价按当时价格选，此处记录当前执行档 au2612P840。"),
    dict(seq=2, b_s="买", o_c="开", trigger_op="lte", trigger_ref="pp_el", trigger_price=None,
         trade_symbol="SHEX.au2612",
         reason="「极低」档：主动加仓（买期货或卖 put 接货），加仓后须重算全链回本价，勿只看本合约成本。"),
    dict(seq=3, b_s="卖", o_c="开", trigger_op="gte", trigger_ref=None, trigger_price=1040,
         trade_symbol="SHEX.au2612",
         reason="价格回到 1040（初始成本线）附近：滚动卖 call 增厚收益，行权价 ≥ 成本价并留 5% 以上虚值，下单时选具体行权价。"),
    dict(seq=4, b_s="卖", o_c="平", trigger_op="gte", trigger_ref="pp_h", trigger_price=None,
         trade_symbol="SHEX.au2612",
         reason="「高」档：平掉期货多头，改买 call 博后续上涨——用有限权利金代替线性敞口，避免高位回落吃掉本金。"),
    dict(seq=5, b_s="卖", o_c="平", trigger_op="gte", trigger_ref="pp_eh", trigger_price=None,
         trade_symbol="SHEX.au2612",
         reason="「极高」档：清空敞口等回调；若仍想保留参与感，少量买 put / 买 call 而非继续持有期货。"),
]

INVALIDATE = dict(
    seq=1,
    proposition="央行购金 + 财政贬值的长期叙事消退",
    signal="（原笔记无量化阈值，待补：如全球央行季度净购金转负、或美国财政赤字率连续两季回落至 <3%）",
    source="",
    action="减仓/重估",
    status="未触发",
)

# 交易笔记：原 au2611P880.review（滚动调仓理由）+ 513100.logic（无标可挂，暂存成交）
BILL_NOTES = [
    dict(bill_id=2919, kind="想法", note_at="2026-10-22 19:33:54",
         expect_symbol="SHEX.au2611P880",
         content=("［原 au2611P880.review，2026-10-22 记］平掉 880 的理由：判断很可能还有下行空间，决定向下移动开仓位置；"
                  "且若 880 被动行权，需要增加保证金并卖 call 增厚，而当前含股票与股指期货整体浮亏严重、增量资金有限。"
                  "（原文写「滚动调仓至 au2611P840」，实际成交为平 au2611P880 @3.52、开 au2612P840 @5.86，月份已按成交订正）")),
    dict(bill_id=2920, kind="想法", note_at="2026-10-22 19:33:54",
         expect_symbol="SHEX.au2612P840",
         content=("［同上，2026-10-22 记］新开 au2612P840 卖 put 的理由：接续 880 的多头敞口，向下移动开仓位置以降低被行权成本，"
                  "权利金 5.86 计入已实现。⚠️ 移动后本合约成本线 909.26，但全链回本价仍为 1002.6 元/克"
                  "（含前三次向下滚仓的约 −13.96 万已实现亏损），复盘别用被重置的成本。")),
    dict(bill_id=2748, kind="想法", note_at="2026-10-22 19:36:41",
         expect_symbol="SHSE.513100",
         content=("主题：海外。［原 SHSE.513100.logic，2026-10-22 记］全球最优一篮子生意。标普500 同类型标的（可相互代替）。"
                  "注：513100 不在估值监测的 8 个标的里，此条暂存为交易笔记，等决定是否纳入标的池。")),
]

# 合约行归属：只填 5 行，其余靠同代码自动命中
VM_MAP = [
    ("期货交易", "SHEX.au2612", "GC00Y"),
    ("期权交易", "SHEX.au2611P880", "GC00Y"),
    ("证券交易", "SHSE.518880", "GC00Y"),
    ("期权交易", "CFFEX.IO2611-P-3800", "SHSE.000300"),
    ("期货交易", "CFFEX.IC2612", "SHSE.000905"),
]


def run(apply: bool) -> None:
    conn = pymysql.connect(
        host=ENV.get("DB_QUZU", "localhost"), port=int(ENV.get("DB_PORT", 3306)),
        user=ENV["DB_USER"], password=ENV["DB_PASSWORD"], database=ENV["DB_NAME"],
        charset="utf8mb4", autocommit=False, cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    print("=" * 78)
    print("旧笔记迁移 + 档位版本 + 归属回填", "【APPLY】" if apply else "【DRY-RUN】")
    print("=" * 78)

    # ---------------- 0) 前置校验：主表 8 行、目标成交行存在
    cur.execute("SELECT COUNT(*) n FROM irs_value_monitor")
    n_vm = cur.fetchone()["n"]
    print(f"\n[0] 主表 {n_vm} 行")
    for bn in BILL_NOTES:
        cur.execute("SELECT symbol FROM bills_bill WHERE id=%s", (bn["bill_id"],))
        r = cur.fetchone()
        ok = r and r["symbol"] == bn["expect_symbol"]
        print(f"    成交 {bn['bill_id']}: {r['symbol'] if r else '不存在'}  {'OK' if ok else '!! 与预期不符，跳过'}")
        bn["_ok"] = ok

    # ---------------- 1) 档位版本：8 行各迁一版，锚点取建标日
    print("\n[1] 档位版本 irs_value_monitor -> irs_vm_version（锚点 DATE(create_time)）")
    cur.execute("SELECT symbol, DATE(create_time) d, pp_el, pp_l, pp_m, pp_h, pp_eh FROM irs_value_monitor ORDER BY id")
    for r in cur.fetchall():
        print(f"    + {r['symbol']:<14} effective_date={r['d']}  五档={r['pp_el']}/{r['pp_l']}/{r['pp_m']}/{r['pp_h']}/{r['pp_eh']}")
        if apply:
            cur.execute("""
                INSERT INTO irs_vm_version (symbol, effective_date, pp_el, pp_l, pp_m, pp_h, pp_eh, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'迁移自旧表，锚点取建标日')
                ON DUPLICATE KEY UPDATE pp_el=VALUES(pp_el), pp_l=VALUES(pp_l), pp_m=VALUES(pp_m),
                    pp_h=VALUES(pp_h), pp_eh=VALUES(pp_eh)""",
                        (r["symbol"], r["d"], r["pp_el"], r["pp_l"], r["pp_m"], r["pp_h"], r["pp_eh"]))

    # ---------------- 2) theme 落主表
    print("\n[2] theme 落主表")
    for sym, theme in THEME_TO_VM.items():
        print(f"    + {sym}.theme = {theme}")
        if apply:
            cur.execute("UPDATE irs_value_monitor SET theme=%s WHERE symbol=%s", (theme, sym))
    # object_type 回写已移除：该列 2026-10-23 按用户要求 DROP（见 ddl_drop_object_type.py）。
    # 留着这段会让本脚本在重跑时报 1054 Unknown column，而本脚本设计上是幂等可重跑的。

    # ---------------- 3) 标的研究定稿
    print("\n[3] 标的研究 irs_vm_research")
    R = RESEARCH
    print(f"    + {R['symbol']} / {R['research_date']}  结论={R['conclusion']}  brief={len(R['research_brief'])} 字  log={len(R['research_log'])} 字")
    if apply:
        cur.execute("""
            INSERT INTO irs_vm_research
              (symbol, research_date, thesis, conclusion, max_risk, expectation_gap, pre_condition,
               next_review, research_note, research_brief, research_log)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE thesis=VALUES(thesis), conclusion=VALUES(conclusion),
              max_risk=VALUES(max_risk), expectation_gap=VALUES(expectation_gap),
              pre_condition=VALUES(pre_condition), next_review=VALUES(next_review),
              research_note=VALUES(research_note), research_brief=VALUES(research_brief),
              research_log=VALUES(research_log)""",
                    (R["symbol"], R["research_date"], R["thesis"], R["conclusion"], R["max_risk"],
                     R["expectation_gap"], R["pre_condition"], R["next_review"], R["research_note"],
                     R["research_brief"], R["research_log"]))

    # ---------------- 4) 计划委托
    print("\n[4] 计划委托 irs_vm_plan")
    for p in PLANS:
        trig = f"trigger_ref={p['trigger_ref']}" if p["trigger_ref"] else f"trigger_price={p['trigger_price']}"
        print(f"    + seq{p['seq']} {p['b_s']}{p['o_c']} {p['trade_symbol']:<18} {p['trigger_op']} {trig}")
        if apply:
            cur.execute("""
                INSERT INTO irs_vm_plan
                  (vm_symbol, trade_symbol, seq, b_s, o_c, trigger_op, trigger_price, trigger_ref, reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE trade_symbol=VALUES(trade_symbol), b_s=VALUES(b_s), o_c=VALUES(o_c),
                  trigger_op=VALUES(trigger_op), trigger_price=VALUES(trigger_price),
                  trigger_ref=VALUES(trigger_ref), reason=VALUES(reason)""",
                        ("GC00Y", p["trade_symbol"], p["seq"], p["b_s"], p["o_c"], p["trigger_op"],
                         p["trigger_price"], p["trigger_ref"], p["reason"]))

    # ---------------- 5) 证伪条件
    print("\n[5] 证伪条件 irs_vm_invalidate")
    I = INVALIDATE
    print(f"    + seq{I['seq']} {I['proposition']}  （signal 无量化阈值，前端会标红）")
    if apply:
        # signal / source / action 都是 MySQL 关键字，必须反引号
        cur.execute("""
            INSERT INTO irs_vm_invalidate (symbol, seq, proposition, `signal`, `source`, `action`, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE proposition=VALUES(proposition), `signal`=VALUES(`signal`),
              `source`=VALUES(`source`), `action`=VALUES(`action`)""",
                    ("GC00Y", I["seq"], I["proposition"], I["signal"], I["source"], I["action"], I["status"]))

    # ---------------- 6) 交易笔记
    print("\n[6] 交易笔记 bills_bill_note")
    for bn in BILL_NOTES:
        if not bn["_ok"]:
            print(f"    - bill {bn['bill_id']} 校验不通过，跳过")
            continue
        print(f"    + bill {bn['bill_id']} kind={bn['kind']} content_at={bn['note_at']} ({len(bn['content'])} 字)")
        if apply:
            cur.execute("""
                INSERT INTO bills_bill_note (bill_id, biz_key, kind, content, content_at)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE content=VALUES(content), content_at=VALUES(content_at)""",
                        (bn["bill_id"], _biz_key_of(cur, bn["bill_id"]), bn["kind"], bn["content"], bn["note_at"]))

    # ---------------- 7) 合约行归属回填
    print("\n[7] 合约行归属 bills_group_symbol.vm_symbol")
    for cat, sym, vm in VM_MAP:
        cur.execute("SELECT id FROM bills_group_symbol WHERE category=%s AND symbol=%s", (cat, sym))
        r = cur.fetchone()
        print(f"    + {cat}/{sym:<20} -> {vm}  {'(合约行存在)' if r else '!! 无此合约行'}")
        if apply and r:
            cur.execute("UPDATE bills_group_symbol SET vm_symbol=%s WHERE id=%s", (vm, r["id"]))

    # ---------------- 8) 丢弃项说明
    print("\n[8] 丢弃：SHEX.au2611P880 的 logic/plan/invalidate 三个字段全是「同SHEX.au2612」")
    print("    纯换月复制粘贴，无独立信息；其 review（滚动调仓理由）已拆到 2919/2920 两笔成交。")

    if apply:
        conn.commit()
        print("\n[9] 已提交")

        print("\n[10] 迁移后计数")
        for t, sql in [
            ("irs_vm_version", "SELECT COUNT(*) n FROM irs_vm_version"),
            ("irs_vm_research", "SELECT COUNT(*) n FROM irs_vm_research"),
            ("irs_vm_plan", "SELECT COUNT(*) n FROM irs_vm_plan"),
            ("irs_vm_invalidate", "SELECT COUNT(*) n FROM irs_vm_invalidate"),
            ("bills_bill_note", "SELECT COUNT(*) n FROM bills_bill_note"),
            ("bills_group_symbol.vm_symbol 非空", "SELECT COUNT(*) n FROM bills_group_symbol WHERE vm_symbol IS NOT NULL"),
            ("irs_value_monitor.theme 非空", "SELECT COUNT(*) n FROM irs_value_monitor WHERE theme IS NOT NULL"),
        ]:
            cur.execute(sql)
            print(f"     {t:<34} {cur.fetchone()['n']}")
    else:
        print("\n[9] DRY-RUN：未写库。加 --apply 执行。")

    cur.close()
    conn.close()


def _biz_key_of(cur, bill_id: int) -> str:
    """按 bills_bill.unique_keys 口径算 md5（与导入去重同一把尺子）"""
    import hashlib
    from decimal import Decimal
    cur.execute("SELECT account, trade_time, symbol, exec_type, amount_act, id_agree, cash "
                "FROM bills_bill WHERE id=%s", (bill_id,))
    r = cur.fetchone()
    if not r:
        raise ValueError(f"bills_bill.id={bill_id} 不存在")

    def f(v):
        if v is None:
            return ""
        if isinstance(v, Decimal):
            return str(v.quantize(Decimal("0.01")))
        return str(v)

    raw = "|".join(f(r[k]) for k in
                   ("account", "trade_time", "symbol", "exec_type", "amount_act", "id_agree", "cash"))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    run(args.apply)


if __name__ == "__main__":
    main()
