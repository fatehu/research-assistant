"""
Notebook 数据库模型
支持 Notebook 和 Cell 的持久化存储
"""
from datetime import datetime
from typing import Optional, List, Any
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship

from app.core.database import Base


class Notebook(Base):
    """Notebook 模型"""
    __tablename__ = "notebooks"
    
    id = Column(String(36), primary_key=True)  # UUID
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(255), default="Untitled Notebook")
    description = Column(Text, nullable=True)
    
    # 执行状态
    execution_count = Column(Integer, default=0)
    
    # 元数据 (避免使用 metadata 保留字)
    notebook_metadata = Column("metadata", JSON, default=dict)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    user = relationship("User", back_populates="notebooks")
    cells = relationship("NotebookCell", back_populates="notebook", 
                        cascade="all, delete-orphan",
                        order_by="NotebookCell.position")
    
    def to_dict(self) -> dict:
        """转换为字典格式（兼容原有内存存储格式）"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "description": self.description,
            "execution_count": self.execution_count,
            "metadata": self.notebook_metadata or {},
            "cells": [cell.to_dict() for cell in self.cells],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class NotebookCell(Base):
    """Notebook 单元格模型"""
    __tablename__ = "notebook_cells"
    
    id = Column(String(36), primary_key=True)  # UUID
    notebook_id = Column(String(36), ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 单元格类型和内容
    cell_type = Column(String(20), default="code")  # 'code' or 'markdown'
    source = Column(Text, default="")
    
    # 执行信息
    execution_count = Column(Integer, nullable=True)
    
    # 输出（JSON 存储）
    outputs = Column(JSON, default=list)
    
    # 元数据 (避免使用 metadata 保留字)
    cell_metadata = Column("metadata", JSON, default=dict)
    
    # 位置（用于排序）
    position = Column(Integer, default=0, index=True)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    notebook = relationship("Notebook", back_populates="cells")
    
    def to_dict(self) -> dict:
        """转换为字典格式（兼容原有格式）"""
        return {
            "id": self.id,
            "cell_type": self.cell_type,
            "source": self.source,
            "outputs": self.outputs or [],
            "execution_count": self.execution_count,
            "metadata": self.cell_metadata or {},
        }
