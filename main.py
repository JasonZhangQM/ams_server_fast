"""FastAPI 应用入口。

创建 app、注册 CORS 中间件、提供 /health 健康检查路由，
并聚合 bds/bills/irs 三个应用的路由到 /api/v1 前缀下。
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server_fast.config import settings
from server_fast.app.bds.router import router as bds_router
from server_fast.app.bills.router import router as bills_router
from server_fast.app.irs.router import router as irs_router

app = FastAPI(
    title="宽客ams FastAPI",
    description="Django→FastAPI 迁移",
    version="1.0.0",
)

# CORS：允许所有源（与原 Django CSRF_TRUSTED_ORIGINS 包含 localhost:9527 的意图一致）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """健康检查路由。"""
    return {"status": "ok"}


# 聚合三个应用的路由，统一挂载到 /api/v1 前缀下
app.include_router(bds_router, prefix=settings.API_V1_PREFIX)
app.include_router(bills_router, prefix=settings.API_V1_PREFIX)
app.include_router(irs_router, prefix=settings.API_V1_PREFIX)

