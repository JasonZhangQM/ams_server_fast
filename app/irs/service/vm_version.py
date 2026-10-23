# -*- coding: utf-8 -*-
"""档位版本（irs_vm_version）：生效区间语义 + 主表 pp_* 回写。

一条铁律：**档位五档只能经 save_version() 写入**。主表 pp_* 是"当前生效版缓存"，
任何地方直写 pp_* 都会让版本链与显示值分叉（v1 §八 R1）。

- 某日 D 的档位 = effective_date <= D 的最大版；
- 未来生效日允许录入，但 refresh_current_pp 只认 <= today，主表不变；
- 版本只存五档，不存行情；列表里的 pv_* 按当前 price 现算，不落库。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from server_fast.app.irs.models import ValueMonitor, VmVersion

PP_FIELDS = ("pp_el", "pp_l", "pp_m", "pp_h", "pp_eh")
PP_LABELS = {"pp_el": "极低", "pp_l": "低", "pp_m": "中", "pp_h": "高", "pp_eh": "极高"}


def calc_pv(pp: Optional[Decimal], price: Optional[Decimal],
            base: Optional[Decimal]) -> Optional[Decimal]:
    """(估值价 / 基准 - 1) * 100；任一为空或基准为 0 时返回 None（不编造）。"""
    if pp is None or base is None or base == 0:
        return None
    return ((pp / base) - Decimal("1")) * Decimal("100")


def _version_pv(row: VmVersion, price: Optional[Decimal],
                py_close: Optional[Decimal]) -> dict:
    """给版本行补现算的 pv_*（相对现价 / 相对上年末），不写库。"""
    out = {}
    for f in PP_FIELDS:
        out[f"pv_{f[3:]}"] = calc_pv(getattr(row, f), price, price)
        out[f"pv_{f[3:]}_y"] = calc_pv(getattr(row, f), price, py_close)
    return out


def effective_version(db: Session, symbol: str,
                      as_of: Optional[date] = None) -> Optional[VmVersion]:
    """某日生效的档位版 = effective_date <= as_of 的最大版。"""
    as_of = as_of or date.today()
    return (db.query(VmVersion)
            .filter(VmVersion.symbol == symbol, VmVersion.effective_date <= as_of)
            .order_by(VmVersion.effective_date.desc())
            .first())


def refresh_current_pp(db: Session, symbol: str) -> Optional[VmVersion]:
    """把主表 pp_* 刷成"当前生效版"。

    没有 <= today 的版本（全是未来版）时，退化为取最早一版：让主表至少有个可见值，
    同时由 status 标出 'pending'，避免列表页出现一列空白档位。
    """
    vm = db.query(ValueMonitor).filter(ValueMonitor.symbol == symbol).one_or_none()
    if vm is None:
        return None
    ver = effective_version(db, symbol)
    fallback = False
    if ver is None:
        ver = (db.query(VmVersion).filter(VmVersion.symbol == symbol)
               .order_by(VmVersion.effective_date.asc()).first())
        fallback = ver is not None
    if ver is not None:
        for f in PP_FIELDS:
            setattr(vm, f, getattr(ver, f))
    db.flush()  # 触发 ValueMonitor 钩子按新 pp_* 重算 pv_*
    if fallback:
        ver._fallback = True  # 仅用于返回值说明，不入库
    return ver


def save_version(db: Session, symbol: str, effective_date: date,
                 pp: dict, note: Optional[str] = None) -> dict:
    """按 (symbol, effective_date) upsert 一版，并回写主表。

    返回 {action: created|updated|unchanged, ...}——前端据此提示"将新建 X 日"还是"将覆盖 X 日"。
    """
    if db.query(ValueMonitor.symbol).filter(ValueMonitor.symbol == symbol).first() is None:
        raise ValueError(f"标的 {symbol} 不在估值监测里，请先新增标的")

    row = (db.query(VmVersion)
           .filter(VmVersion.symbol == symbol, VmVersion.effective_date == effective_date)
           .one_or_none())
    action = "created" if row is None else "updated"
    if row is None:
        row = VmVersion(symbol=symbol, effective_date=effective_date)
        db.add(row)

    changed = []
    for f in PP_FIELDS:
        new = pp.get(f)
        old = getattr(row, f, None) if action == "updated" else None
        if action == "updated" and new == old:
            continue
        setattr(row, f, new)
        if action == "updated":
            changed.append({"field": f, "label": PP_LABELS[f],
                            "old": str(old) if old is not None else None,
                            "new": str(new) if new is not None else None})
    if note is not None:
        row.note = note
    if action == "updated" and not changed and note is None:
        action = "unchanged"

    db.flush()
    ver = refresh_current_pp(db, symbol)
    is_current = (ver is not None and ver.effective_date == effective_date
                  and effective_date <= date.today())
    return {
        "action": action,
        "version_id": row.id,
        "effective_date": effective_date,
        "is_current": bool(is_current),
        "is_future": effective_date > date.today(),
        "changed": changed,
    }


def list_versions(db: Session, symbol: str) -> list[dict]:
    """该标的全部版本（日期倒序），每行附现算 pv_*、status 与相对上一版的差值。"""
    vm = db.query(ValueMonitor).filter(ValueMonitor.symbol == symbol).one_or_none()
    price = vm.price if vm else None
    py_close = vm.py_close if vm else None
    today = date.today()
    rows = (db.query(VmVersion).filter(VmVersion.symbol == symbol)
            .order_by(VmVersion.effective_date.desc()).all())
    cur = effective_version(db, symbol)
    pending = (db.query(VmVersion)
               .filter(VmVersion.symbol == symbol, VmVersion.effective_date > today)
               .order_by(VmVersion.effective_date.asc()).first())
    out = []
    for i, r in enumerate(rows):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        item = {
            "id": r.id, "symbol": r.symbol,
            "effective_date": r.effective_date, "note": r.note,
            "create_time": r.create_time, "update_time": r.update_time,
            "status": ("current" if cur is not None and r.id == cur.id
                       else ("pending" if r.effective_date > today else "history")),
            "pending_next": bool(pending is not None and r.id == pending.id),
            "diff": {f: (None if prev is None or getattr(r, f) is None or getattr(prev, f) is None
                         else getattr(r, f) - getattr(prev, f)) for f in PP_FIELDS},
        }
        for f in PP_FIELDS:
            item[f] = getattr(r, f)
        item.update(_version_pv(r, price, py_close))
        out.append(item)
    return out


def seed_versions_from_main(db: Session) -> int:
    """给还没有任何版本行的标的补一版（锚点取建标日，不用 update_time——它是同步时间）。"""
    n = 0
    vms = db.query(ValueMonitor).all()
    for vm in vms:
        exists = db.query(VmVersion.id).filter(VmVersion.symbol == vm.symbol).first()
        if exists:
            continue
        anchor = (vm.create_time.date() if vm.create_time else date.today())
        db.add(VmVersion(symbol=vm.symbol, effective_date=anchor,
                         note="新建标的时自动建立首版",
                         **{f: getattr(vm, f) for f in PP_FIELDS}))
        n += 1
    db.flush()
    return n
