# -*- coding: utf-8 -*-
"""SQLAlchemy 2.0 版本的 BaseModel mixin。

替代原 Django 的 BaseModel（server_dj/common/models.py 第 8-102 行）。
作为 mixin 使用：实际模型类继承 (Base, BaseModel)，Base 来自 server_fast.common.db。
"""
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    Numeric,
    Float,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class BaseModel:
    """所有 ORM 模型共享的基础 mixin：通用字段 + 元数据工具方法。"""

    # 三个通用字段：主键、创建时间、更新时间
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # 子类覆盖：格式 {db_field: [别名1, 别名2]}，用于外部列名映射
    cols_map_fields: dict = {}

    @classmethod
    def to_dtype(cls) -> dict:
        """根据数据库列类型返回 pandas 数据类型映射。

        遍历 cls.__table__.columns，按 SQLAlchemy 列类型映射：
        Numeric/Float -> float, Integer -> int, String/Text -> str

        排除 id/create_time/update_time 三个通用字段：
        - id 为自增主键，Django 的 BigAutoField 不在类型映射中
        - create_time/update_time 为数据库自动维护的时间戳
        与原 Django BaseModel.to_dtype() 行为保持一致。
        """
        _skip = {"id", "create_time", "update_time"}
        dtype_dict = {}
        for col in cls.__table__.columns:
            if col.name in _skip:
                continue
            col_type = col.type
            if isinstance(col_type, (Numeric, Float)):
                dtype_dict[col.name] = float
            elif isinstance(col_type, Integer):
                dtype_dict[col.name] = int
            elif isinstance(col_type, (String, Text)):
                dtype_dict[col.name] = str
        return dtype_dict

    @classmethod
    def map_fields(cls) -> dict:
        """将 cols_map_fields 翻转为 {别名: db_field} 字典。

        例：{'serial_number': ['成交编号', '合同编号']}
        ----> {'成交编号': 'serial_number', '合同编号': 'serial_number'}
        """
        field_dict = {}
        for key, value in cls.cols_map_fields.items():
            for name in value:
                field_dict[name] = key
        return field_dict

    @classmethod
    def db_fields(cls, is_id: bool = False) -> list:
        """返回数据库字段名列表。

        is_id=False 时排除 id/create_time/update_time；
        is_id=True 时仅排除 create_time/update_time（保留 id）。
        SQLAlchemy 中外键列名本身就是 xxx_id，无需额外处理后缀。
        """
        if is_id:
            exclude_fields = {"create_time", "update_time"}
        else:
            exclude_fields = {"id", "create_time", "update_time"}
        return [
            col.name for col in cls.__table__.columns if col.name not in exclude_fields
        ]

    def to_dict(self, exclude_fields: Optional[List[str]] = None) -> Dict[str, Any]:
        """将模型实例转换为字典，datetime/date 转为字符串。

        :param exclude_fields: 需排除的字段列表（如敏感字段）
        """
        exclude = exclude_fields or []
        data = {}
        for col in self.__table__.columns:
            if col.name in exclude:
                continue
            value = getattr(self, col.name)
            # datetime/date 转字符串，便于序列化（先判 datetime 因其是 date 子类）
            if isinstance(value, datetime):
                data[col.name] = value.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(value, date):
                data[col.name] = value.strftime("%Y-%m-%d")
            else:
                data[col.name] = value
        return data
