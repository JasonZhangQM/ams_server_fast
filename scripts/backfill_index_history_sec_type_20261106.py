# -*- coding: utf-8 -*-
"""回填存量行 sec_type / adjust：

判定规则（与 sync_symbol_history 一致）：
  - bds_symbol_info.industry = 'A股指数' → sec_type='index'
  - 其余 market IS NOT NULL → sec_type='stock'
  - 都不满足 → 兜底 'index'（历史遗留）
adjust：旧链路统一用 ADJUST_NONE，存量全部设为 'none'
只更新 sec_type IS NULL 的行（新同步的个股/指数行已有值，不碰）
"""
import sys, argparse
sys.path.insert(0, 'c:/quant_man/mysite_quant/ams_trae')
from sqlalchemy import text
from server_fast.common.db import SessionLocal
from server_fast.app.bds.models import SymbolInfo
from server_fast.app.bds.services.symbol_info.market_board import resolve_sec_type

TABLE = 'bds_index_history'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真正执行；不传则只打印 dry-run')
    args = ap.parse_args()
    tag = '【APPLY】' if args.apply else '【DRY-RUN】'
    print('=' * 72)
    print(f'回填存量 sec_type / adjust 到 {TABLE} {tag}')
    print('=' * 72)

    from server_fast.config import settings
    db = SessionLocal()
    # 必须用 autocommit=False 手动控制事务
    with settings.DB_ENGINE.connect() as conn:
        trans = conn.begin()
        try:
            # 1. 先取存量 symbol 的判定结果（与诊断脚本一致）
            rows = conn.execute(text(
                "SELECT symbol, COUNT(*) AS cnt FROM bds_index_history "
                "WHERE sec_type IS NULL GROUP BY symbol ORDER BY symbol"
            )).all()
            if not rows:
                print('✗ 没有 sec_type IS NULL 的行，无需回填')
                trans.rollback()
                return
            total_rows = sum(r[1] for r in rows)
            print(f'存量：{len(rows)} 个 symbol，{total_rows} 行')

            # 2. 判定每个 symbol 的 sec_type
            symbol_list = [r[0] for r in rows]
            info = db.query(SymbolInfo.symbol, SymbolInfo.industry, SymbolInfo.market) \
                     .filter(SymbolInfo.symbol.in_(symbol_list)).all()
            info_map = {r.symbol: (r.industry, r.market) for r in info}

            plan = []
            for sym, cnt in rows:
                ind, mkt = info_map.get(sym, (None, None))
                st = resolve_sec_type(ind, mkt) or 'index'  # 兜底
                plan.append((sym, st, 'none', cnt))

            # 3. 按 symbol 分组 UPDATE（每条带 WHERE symbol=? + sec_type IS NULL，幂等）
            for sym, st, adj, cnt in plan:
                sql = (f"UPDATE {TABLE} SET sec_type='{st}', adjust='{adj}' "
                       f"WHERE symbol='{sym}' AND sec_type IS NULL")
                print(sql, f"  -- 预计影响 {cnt} 行")
                if args.apply:
                    affected = conn.execute(text(sql)).rowcount
                    print(f"  + 实际影响 {affected} 行")
                    if affected != cnt:
                        print(f"  ⚠️ 行数不一致！预期 {cnt}，实际 {affected}")

            # 4. 汇总
            if args.apply:
                trans.commit()
                print('\n✅ 已提交')
            else:
                trans.rollback()
                print('\n🔒 DRY-RUN，已回滚（未真正执行）')

            # 5. 验证（apply 才有效）
            print('\n[验证]')
            if args.apply:
                null_sec = conn.execute(text(
                    f"SELECT COUNT(*) FROM {TABLE} WHERE sec_type IS NULL"
                )).scalar()
                null_adj = conn.execute(text(
                    f"SELECT COUNT(*) FROM {TABLE} WHERE adjust IS NULL"
                )).scalar()
                print(f"  sec_type IS NULL: {null_sec}（应为 0）")
                print(f"  adjust IS NULL:   {null_adj}（应为 0）")
                dist = conn.execute(text(
                    f"SELECT sec_type, adjust, COUNT(*) FROM {TABLE} GROUP BY sec_type, adjust"
                )).all()
                for r in dist:
                    print(f"  ({r[0]}, {r[1]}): {r[2]} 行")
            else:
                print('  （DRY-RUN 未执行，无法验证）')
        except Exception as e:
            trans.rollback()
            print(f'✗ 异常，已回滚：{type(e).__name__}: {str(e)[:300]}')
            raise
        finally:
            conn.close()
    db.close()

if __name__ == '__main__':
    main()
