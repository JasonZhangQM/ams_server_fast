"""SQLAlchemy 数据库基础设施。

提供 Base（模型基类）、SessionLocal（会话工厂）、get_db（请求依赖）。
engine 复用 server_fast.config.settings.DB_ENGINE，不重复创建。
"""
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from server_fast.config import settings

# 所有 ORM 模型的基类，后续模型继承自此
Base = declarative_base()

# 会话工厂：绑定到全局 engine，autocommit/autoflush 关闭以显式控制事务
SessionLocal = sessionmaker(
    bind=settings.DB_ENGINE, autocommit=False, autoflush=False
)


def get_db():
    """FastAPI 依赖：每个请求获取独立 session，请求结束后关闭。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
