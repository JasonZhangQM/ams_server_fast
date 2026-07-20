"""验证 sync_index_history 优化逻辑。"""
import sys
sys.path.insert(0, '.')

import inspect
from server_fast.app.bds.services.data_sync import upsert_index_history_sql
src = inspect.getsource(upsert_index_history_sql)
assert 'max_date >= today' in src, '缺少 max_date >= today 判断'
assert '已是今日，跳过同步' in src, '缺少跳过日志'
print('[OK] upsert_index_history_sql 已注入优化逻辑：max_date >= today 时跳过接口调用')

# 验证 router 正常导入
from server_fast.app.bds.router import router
print('OK routes =', len(router.routes))
