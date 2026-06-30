# -*- coding: utf-8 -*-
"""统一分页响应 Schema。

为所有 GET 列表路由提供统一的分页包装结构，
前端可据 total 计算总页数与当前页码。

用法：
    from server_fast.common.pagination import PageResponse

    @router.get("/xxx", response_model=PageResponse[XxxOut])
    def list_xxx(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
        query = db.query(Xxx)
        total = query.count()                # 过滤后的总记录数
        items = query.offset(offset).limit(limit).all()
        return {"items": items, "total": total, "limit": limit, "offset": offset}
"""
from typing import Generic, List, TypeVar

from pydantic import BaseModel, ConfigDict

# 泛型类型变量，绑定各业务的响应 Schema（如 TradeDateOut、BillOut 等）
T = TypeVar("T")


class PageResponse(BaseModel, Generic[T]):
    """统一分页响应结构。

    items  - 当前页数据列表
    total  - 满足过滤条件的总记录数（非当前页条数）
    limit  - 每页条数
    offset - 偏移量
    """

    model_config = ConfigDict(from_attributes=True)

    items: List[T]
    total: int
    limit: int
    offset: int
