# -*- coding: utf-8 -*-
"""P2 落表哨兵实验 4/5/6/7/8 + 幂等 + M2 快跳过（方案 §10、优化方案 §1）。

- 哨兵 4「不覆盖人工值」：给已有行写 `note`（不在 fields_pct_update 白名单），
  跑 M2 全量重算后确认 note 仍在。
- 哨兵 5「孤儿清理」：插一行 `__sentinel__` subject，显式 `prune_orphans=True`
  + 全量 L1 + M2 后确认被删且计数正确。
- 哨兵 6「修订响应 / 不误报」：**不改生产源表**，改为验证等价语义——
  ① `input_fp` 对源序列任一值变化敏感；② 源未变时 `rebuild(auto)` 全部 skip。
- 哨兵 7「P0-1 单标的不得删库」：传 `subjects=['SHSE.600036']` 跑 M2，
  孤儿行必须还在、`orphans_deleted=0`；只有显式 prune + 全量才允许删。
- 哨兵 8「P0-2/P0-3 fp 变化一律强制全量」：把某标的**最新行** `input_fp` 改假
  （等价于上游历史/同日修订，但绝不碰源表），auto 与 M1 都必须全量重写该
  subject 的整段历史（约 5,965 行），而不是只写最新 1 行或直接跳过。
- 幂等：全量重写前后 `audit.changed/appeared/vanished` 全 0。
- 快跳过：源未变时 M2 / auto 全部跳过，不写任何行。

🔴 快跳过会让「哨兵 4」假通过：不写当然不会覆盖 note，等于没测到白名单。
故布置哨兵时把 SHSE.000300 最新行 `input_fp` 改成假值 ⇒ 新语义下该 subject
因 fp 变化被强制全量重写（真 upsert 5,240 行），其余 5 个走快跳过。

⚠️ 会写 `bds_valuation_percentile`（测试行 + 覆盖写），不会碰任何上游源表。
跑完自动把 note 清回 NULL、删除孤儿行、把两个标的的 input_fp 恢复为真实值。

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
FORCE_REWRITE = "SHSE.000300"          # index，用于哨兵 4
STOCK = "SHSE.600036"                  # stock，用于哨兵 7/8
FAKE_FP = "sentinel-stale-fp-forces-rewrite"


def _count_orphan() -> int:
    with SessionLocal() as db:
        return int(db.execute(text(
            "SELECT COUNT(*) FROM bds_valuation_percentile WHERE symbol=:s"),
            {"s": SENTINEL}).scalar())


def _fake_fp(subject_type: str, symbol: str, as_of, fp: str) -> None:
    """只改表内最新行的指纹，模拟「上游序列被修订」（不动任何源表）。"""
    with SessionLocal() as db:
        db.execute(text(
            "UPDATE bds_valuation_percentile SET input_fp=:fp "
            "WHERE subject_type=:t AND symbol=:s AND as_of=:d"),
            {"fp": fp, "t": subject_type, "s": symbol, "d": as_of})
        db.commit()


def main():
    # 🔒4.3：落笔前查无 running
    with SessionLocal() as db:
        n = db.execute(text("SELECT COUNT(*) FROM bds_sync_task WHERE status='running'")).scalar()
        assert n == 0, f"有 {n} 条 running 任务，按 4.3 中止"
        end = store.source_end_date(db)

    # ---- 布置哨兵 ----
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
    print(f"[布置] 源末日={end}，note 哨兵 + 孤儿行 {SENTINEL} + {FORCE_REWRITE} 最新行 fp 造假")

    # ---- 哨兵 7a：单标的 M2 绝不删别人的历史行（P0-1）----
    r_single = store.rebuild(mode="M2", subjects=[STOCK])
    orphan_after_single = _count_orphan()
    single_ok = (r_single["orphans_deleted"] == 0
                 and r_single["prune_orphans"] is False
                 and r_single["subjects_explicit"] is True
                 and orphan_after_single == 1)
    print(f"[哨兵 7a] 单标的 M2：写入 {r_single['rows_upserted']} 行, "
          f"orphans_deleted={r_single['orphans_deleted']}, prune={r_single['prune_orphans']}, "
          f"孤儿残留={orphan_after_single} → {'✅ 未误删' if single_ok else '❌'}")

    # ---- 哨兵 4/5/7b：显式 prune + 全量 L1 才允许删；同时真重写测白名单 ----
    r_prune = store.rebuild(mode="M2", prune_orphans=True)
    print(f"[M2+prune] rows_upserted={r_prune['rows_upserted']} "
          f"skipped={r_prune['skipped_count']} {r_prune['skipped_subjects']} "
          f"forced_full={r_prune['forced_full_count']} fp_changed={r_prune['fp_changed_subjects']} "
          f"orphans_deleted={r_prune['orphans_deleted']} "
          f"prune={r_prune['prune_orphans']} duration={r_prune['duration_ms']}ms")
    print(f"            audit={r_prune['audit']}")

    with SessionLocal() as db:
        note = db.execute(text(
            "SELECT note FROM bds_valuation_percentile "
            "WHERE subject_type='index' AND symbol=:s AND as_of=:d"),
            {"s": FORCE_REWRITE, "d": end}).scalar()
    orphan_after_prune = _count_orphan()
    rewrote = r_prune["rows_upserted"] >= 5240 and FORCE_REWRITE not in r_prune["skipped_subjects"]
    skipped_ok = r_prune["skipped_count"] == 5
    fp_ok = FORCE_REWRITE in r_prune["fp_changed_subjects"]
    prune_ok = (r_prune["prune_orphans"] is True and r_prune["orphans_deleted"] >= 1
                and orphan_after_prune == 0)
    print(f"[哨兵 4] note 保留 = {note!r} → {'✅' if note == NOTE else '❌'}")
    print(f"[哨兵 5/7b] 真重写 {r_prune['rows_upserted']} 行 → {'✅' if rewrote else '❌'}；"
          f"跳过 5 个 → {'✅' if skipped_ok else '❌'}；"
          f"指纹变化被识别 → {'✅' if fp_ok else '❌'}；"
          f"显式 prune 才删孤儿（残留 {orphan_after_prune}）→ {'✅' if prune_ok else '❌'}")

    # ---- 哨兵 8a：auto 遇到 fp 变化必须全量重写该 subject，而不是只写最新 1 行 ----
    _fake_fp("stock", STOCK, end, FAKE_FP)
    r_auto = store.rebuild(mode="auto")
    auto_ok = (r_auto["rows_upserted"] >= 5965
               and STOCK in r_auto["fp_changed_subjects"]
               and STOCK not in r_auto["skipped_subjects"]
               and r_auto["skipped_count"] == 5)
    print(f"[哨兵 8a] auto 历史/同日修订：写入 {r_auto['rows_upserted']} 行, "
          f"fp_changed={r_auto['fp_changed_subjects']}, skipped={r_auto['skipped_count']} "
          f"→ {'✅ 全量重写（原实现只写 1 行）' if auto_ok else '❌'}")

    # ---- 哨兵 8b：M1 也要能感知 fp 变化（原实现 rows_total=0 直接跳过）----
    _fake_fp("stock", STOCK, end, FAKE_FP)
    r_m1 = store.rebuild(mode="M1")
    m1_ok = r_m1["rows_upserted"] >= 5965 and STOCK in r_m1["fp_changed_subjects"]
    print(f"[哨兵 8b] M1 同日修订：写入 {r_m1['rows_upserted']} 行, "
          f"rows_total={r_m1['rows_total']} → {'✅ 不再漏算' if m1_ok else '❌'}")

    # ---- 哨兵 6：input_fp 敏感性 + auto 不误触发 ----
    with SessionLocal() as db:
        raw = store.load_source(db, "stock", STOCK)
    fp1 = store.fingerprint(raw)
    i = len(raw) // 2
    mutated = list(raw[:i]) + [tuple(raw[i])] + list(raw[i + 1:])
    row = list(mutated[i])       # 改一条历史行的 pe_ttm（只在内存里，绝不写库）
    row[1] = (float(row[1]) * 3) if row[1] is not None else 30.0
    mutated[i] = tuple(row)
    fp2 = store.fingerprint(mutated)
    fp_sensitive = fp1 != fp2
    print(f"[哨兵 6a] 改一条历史 PE ⇒ input_fp 变化 = {fp1[:16]} → {fp2[:16]} "
          f"{'✅' if fp_sensitive else '❌'}")

    ra = store.rebuild(mode="auto")
    no_false_alarm = ra["rows_total"] == 0 and ra["skipped_count"] == 6
    print(f"[哨兵 6b] 源未变时 auto：rows_total={ra['rows_total']} "
          f"skipped={ra['skipped_count']} duration={ra['duration_ms']}ms "
          f"{'✅ 未误触发' if no_false_alarm else '❌ 误触发重算'}")

    idem = (r_prune["audit"]["changed"] == 0 and r_prune["audit"]["appeared"] == 0
            and r_prune["audit"]["vanished"] == 0)
    print(f"[幂等] 全量重写 audit.changed/appeared/vanished 全 0 = {'✅' if idem else '❌'}")

    # ---- 第三次 M2：fp 已全部恢复，6 个 subject 都应快跳过 ----
    r3 = store.rebuild(mode="M2")
    fast_ok = (r3["rows_upserted"] == 0 and r3["skipped_count"] == 6
               and r3["cut_stale_subjects"] == [] and r3["fp_changed_subjects"] == [])
    print(f"[M2 全跳过] rows_upserted={r3['rows_upserted']} skipped={r3['skipped_count']} "
          f"duration={r3['duration_ms']}ms → {'✅ 快跳过生效' if fast_ok else '❌'}")

    # ---- 清理：表恢复生产态 ----
    with SessionLocal() as db:
        db.execute(text(
            "UPDATE bds_valuation_percentile SET note=NULL "
            "WHERE subject_type='index' AND symbol=:s AND as_of=:d"),
            {"s": FORCE_REWRITE, "d": end})
        db.execute(text("DELETE FROM bds_valuation_percentile WHERE symbol=:s"), {"s": SENTINEL})
        # 保险：即使中途异常，也把两个标的的最新行 fp 恢复成真实指纹
        for t, s in (("index", FORCE_REWRITE), ("stock", STOCK)):
            true_fp = store.fingerprint(store.load_source(db, t, s))
            db.execute(text(
                "UPDATE bds_valuation_percentile SET input_fp=:fp "
                "WHERE subject_type=:t AND symbol=:s AND as_of=:d"),
                {"fp": true_fp, "t": t, "s": s, "d": end})
        db.commit()
    print("[清理] note 置回 NULL、孤儿行删除、两个标的 input_fp 恢复真实值（表恢复生产态）")

    ok = all([single_ok, rewrote, skipped_ok, fp_ok, prune_ok, auto_ok, m1_ok,
              fp_sensitive, no_false_alarm, idem, fast_ok, note == NOTE,
              orphan_after_prune == 0])
    print("\n=== Exit ===", "✅ 全部通过" if ok else "❌ 存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
