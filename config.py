"""FastAPI 项目配置：基于 pydantic-settings 加载 .env，提供全局 settings 对象。

关键点：
- DB_ENGINE 为 SQLAlchemy engine 单例（显式包含端口 3306，区别于原 Django 无端口写法）。
- gm SDK set_token 调用用 try/except 包裹，gm 未安装时不影响配置加载。
"""
import sys

# Windows 控制台默认 GBK 代码页会导致中文日志乱码，
# 在所有日志输出前强制将 stdout/stderr 重配置为 UTF-8（Python 3.7+ 支持）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and _stream.encoding != "utf-8":
        _stream.reconfigure(encoding="utf-8")

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# .env 位于 server_fast/ 目录下，基于 __file__ 定位以确保不受运行时 CWD 影响
_ENV_FILE = Path(__file__).parent / ".env"

# 模块级缓存：避免重复创建 engine
_db_engine: Engine | None = None


class Settings(BaseSettings):
    """应用配置，字段与 .env 一一对应。"""

    DB_NAME: str
    DB_USER: str
    DB_PASSWORD: str
    DB_HOST: str
    DB_PORT: int = 3306
    GM_TOKEN: str
    FOLDER_OUT: Path
    API_V1_PREFIX: str = "/api/v1"
    # FRED API（圣路易斯联邦储备银行经济数据）
    FRED_API_KEY: str = ""
    FRED_API_BASE: str = "https://api.stlouisfed.org/fred"
    # HTTP/HTTPS 代理配置（可选，留空则直连）
    # 用于访问被网络环境阻断的境外 API（如 IMF SDMX API: dataservices.imf.org）
    HTTP_PROXY: str = ""
    HTTPS_PROXY: str = ""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def proxies(self):
        """返回 httpx 代理字典，未配置时为 None。

        httpx 的 proxies 参数接受 {scheme: proxy_url} 字典，
        例如 {'http://': 'http://127.0.0.1:7890', 'https://': 'http://127.0.0.1:7890'}
        """
        if not (self.HTTP_PROXY or self.HTTPS_PROXY):
            return None
        return {
            "http://": self.HTTP_PROXY or None,
            "https://": self.HTTPS_PROXY or self.HTTP_PROXY or None,
        }

    @property
    def DB_ENGINE(self) -> Engine:
        """SQLAlchemy engine 单例。

        连接串格式：mysql+pymysql://{user}:{password}@{host}:{port}/{name}
        原 Django 代码无端口，FastAPI 版本显式包含 3306。
        """
        global _db_engine
        if _db_engine is None:
            url = (
                f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
                f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            )
            _db_engine = create_engine(url, pool_pre_ping=True)
        return _db_engine


# 全局配置实例
settings = Settings()

# gm SDK token 设置：gm 未安装或调用失败时不影响配置加载
try:
    from gm.api import set_token

    set_token(settings.GM_TOKEN)
except Exception:
    pass
