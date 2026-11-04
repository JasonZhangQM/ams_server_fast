# -*- coding: utf-8 -*-
"""P2 落表哨兵实验 4/5/6 + 幂等 + M2 快跳过验证（方案 §10）。

- 哨兵 4「不覆盖人工值」：给已有行写 `note`（不在 fields_pct_update 白名单），
  跑 M2 全量重算后确认 note 仍在。
- 哨兵 5「孤儿清理」：插一行 `__sentinel__` subject，跑 M2 后确认被删且
  `orphans_deleted` 计数正确。
- 哨兵 6「修订响应 / 不误报」：**不改生产源表**（只有一个库，改历史值风险太大），
  改为验证等价语义——① `input_fp` 对源序列任一值变化敏感；
  ② 源未变时 `rebuild(auto)` 全部 skip（对应「重复同步同一天刷了 update_time 但值没变」）。
- 幂等：第二次 M2 的 `audit.changed = 0`、`appeared = 0`、`vanished = 0`。
- 快跳过：M2 对「fp / 源末日 / 窗口口径三未变」的 subject 不重写。

🔴 快跳过会让「哨兵 4」假通过：不写当然不会覆盖 note，等于没测到白名单。
故布置哨兵时把 SHSE.000300 最新行的 `input_fp` 改成假值，强制该 subject 真重写，
其余 5 个 subject 走快跳过 —— 一次运行同时验证「白名单生效」与「跳过生效」。

⚠️ 会写 `bds_valuation_percentile`（测试行 + 覆盖写），不会碰任何上游源表。
跑完会自动把 note 清回 NULL、删除孤儿行；input_fp 由重写恢复为真实值（另加保险 UPDATE）。

用法：conda run -n fast311 python server_fast/scripts/probe_sentinels_20261104.py
"""
from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, r"C:/quant_man/mysite_quant/ams_trae")

from sqlalchemy import text  # noqa: E402
from server_fast.app.bds.services.valuation import percentile_store as store  # noqa: E402
from server_fast.common.db import SessionLocal  # noqa: E402

SENTINEL = "__sentinel__"
NOTE = "哨兵-勿覆盖-20261104"
FORCE_REWRITE = "SHSE.000300"
FAKE_FP = "sentinel-stale-fp-forces-rewrite"


def main():
    # 🔒4.3：落笔前查无 running
    with SessionLocal() as db:
        n = db.execute(text("SELECT COUNT(*) FROM bds_sync_task WHERE status='running'")).scalar()
        assert n == 0, f"有 {n} 条 running 任务，按 4.3 中止"
        end = store.source_end_date(db)

    # ---- 布置哨兵 ----
    # input_fp 造假只作用于 FORCE_REWRITE 的最新一行 ⇒ 该 subject 的 M2 快跳过闸门失效，
    # 会真的走 upsert（覆盖 note 所在行），白名单才被测到；其余 5 个 subject 照常跳过。
    with SessionLocal() as db:
        db.execute(text(
            "UPDATE bds_valuation_percentile SET note=:n, input_fp=:fp "
            "WHERE subject_type='index' AND symbol=:s AND as_of=:d"),
            {"n": NOTE, "fp": FAKE_FP, "s": FORCE_REWRITE, "d": end})
        db.execute(text(
            "DELETE FROM bds_valuation_percentile WHERE symbol=:s"), {"s": SENTINEL})
        db.execute(text(
            "INSERT INTO bds_valuation_percentile "
            "(subject_type, symbol, as_of, calc_at, note) "
            "VALUES ('stock', :s, :d, :c, '孤儿哨兵')"),
            {"s": SENTINEL, "d": end, "c": datetime.now()})
        db.commit()
    print(f"[布置] 源末日={end}，已写 note 哨兵 + 插孤儿行 {SENTINEL} "
          f"+ 把 {FORCE_REWRITE} 最新行 input_fp 造假")

    # ---- 哨兵 4/5 + 幂等 + 快跳过：第二次 M2 ----
    r2 = store.rebuild(mode="M2")
    print(f"[M2 第二次] rows_upserted={r2['rows_upserted']} rowcount={r2['rows_rowcount']} "
          f"skipped={r2['skipped_count']} {r2['skipped_subjects']} "
          f"cut_stale={r2['cut_stale_subjects']} "
          f"orphans_deleted={r2['orphans_deleted']} duration={r2['duration_ms']}ms")
    print(f"            audit={r2['audit']}")

    rewrote = r2["rows_upserted"] > 0 and FORCE_REWRITE not in r2["skipped_subjects"]
    skipped_ok = r2["skipped_count"] == 5 and FORCE_REWRITE not in r2["skipped_subjects"]
    cut_ok = not r2["cut_stale_subjects"]
    print(f"[快跳过] 真重写={r2['rows_upserted']} 行 → {'✅' if rewrote else '❌'}；"
          f"跳过 5 个 subject → {'✅' if skipped_ok else '❌'}；"
          f"窗口口径未误判陈旧 → {'✅' if cut_ok else '❌'}")

    with SessionLocal() as db:
        note = db.execute(text(
            "SELECT note FROM bds_valuation_percentile "
            "WHERE subject_type='index' AND symbol=:s AND as_of=:d"),
            {"s": FORCE_REWRITE, "d": end}).scalar()
        orphan = db.execute(text(
            "SELECT COUNT(*) FROM bds_valuation_percentile WHERE symbol=:s"),
            {"s": SENTINEL}).scalar()
        print(f"[哨兵 4] note 保留 = {note!r}  → {'✅' if note == NOTE else '❌'}")
        print(f"[哨兵 5] 孤儿残留 = {orphan}，orphans_deleted={r2['orphans_deleted']} "
              f"→ {'✅' if orphan == 0 and r2['orphans_deleted'] >= 1 else '❌'}")

    # ---- 哨兵 6：input_fp 敏感性 + auto 不误触发 ----
    with SessionLocal() as db:
        raw = store.load_source(db, "stock", "SHSE.600036")
    fp1 = store.fingerprint(raw)
    i = len(raw) // 2
    mutated = list(raw[:i]) + [tuple([raw[i][0], raw[i][1], raw[i][2], raw[i][3], raw[i][4]])] + list(raw[i + 1:])
    # 改一条历史行的 pe_ttm（只在内存里，绝不写库）
    row = list(mutated[i])
    row[1] = (float(row[1]) * 3) if row[1] is not None else 30.0
    mutated[i] = tuple(row)
    fp2 = store.fingerprint(mutated)
    print(f"[哨兵 6a] 改一条历史 PE ⇒ input_fp 变化 = {fp1[:16]} → {fp2[:16]}  "
          f"{'✅' if fp1 != fp2 else '❌'}")

    ra = store.rebuild(mode="auto")
    print(f"[哨兵 6b] 源未变时 auto：rows_total={ra['rows_total']} "
          f"changed={ra['audit']['changed']} skipped={ra['skipped_count']} "
          f"duration={ra['duration_ms']}ms "
          f"{'✅ 未误触发' if ra['rows_total'] == 0 else '❌ 误触发重算'}")

    idem = (r2["audit"]["changed"] == 0 and r2["audit"]["appeared"] == 0
            and r2["audit"]["vanished"] == 0)
    print(f"[幂等] 第二次 M2 audit.changed/appeared/vanished 全 0 = {'✅' if idem else '❌'}")

    # ---- 第三次 M2：全部 subject 都应快跳过（r2 已把 input_fp 恢复为真实值）----
    r3 = store.rebuild(mode="M2")
    fast_ok = r3["rows_upserted"] == 0 and r3["skipped_count"] == 6 \
        and r3["cut_stale_subjects"] == []
    print(f"[M2 第三次·全跳过] rows_upserted={r3['rows_upserted']} "
          f"skipped={r3['skipped_count']} duration={r3['duration_ms']}ms → "
          f"{'✅ 快跳过生效' if fast_ok else '❌'}")

    # ---- 清理 ----
    with SessionLocal() as db:
        db.execute(text(
            "UPDATE bds_valuation_percentile SET note=NULL "
            "WHERE subject_type='index' AND symbol=:s AND as_of=:d"),
            {"s": FORCE_REWRITE, "d": end})
        db.execute(text("DELETE FROM bds_valuation_percentile WHERE symbol=:s"), {"s": SENTINEL})
        # 保险：即使 r2 异常没重写，也把 input_fp 恢复成真实指纹，避免下一次 M2 被误判
        true_fp = store.fingerprint(store.load_source(db, "index", FORCE_REWRITE))
        db.execute(text(
            "UPDATE bds_valuation_percentile SET input_fp=:fp "
            "WHERE subject_type='index' AND symbol=:s AND as_of=:d"),
            {"fp": true_fp, "s": FORCE_REWRITE, "d": end})
        db.commit()
    print("[清理] note 已置回 NULL，孤儿行已删，input_fp 已恢复（表恢复生产态）")

    ok = (note == NOTE and orphan == 0 and r2["orphans_deleted"] >= 1
          and fp1 != fp2 and ra["rows_total"] == 0 and idem
          and rewrote and skipped_ok and cut_ok and fast_ok)
    print("\n=== Exit ===", "✅ 全部通过" if ok else "❌ 存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
