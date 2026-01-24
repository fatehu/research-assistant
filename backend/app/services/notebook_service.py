"""
Notebook 服务层
处理 Notebook 的数据库操作
"""
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from sqlalchemy.orm import selectinload
from loguru import logger

from app.models.notebook import Notebook, NotebookCell


class NotebookService:
    """Notebook 数据库操作服务"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def get_user_notebooks(self, user_id: int) -> List[Dict]:
        """获取用户的所有 Notebook"""
        result = await self.db.execute(
            select(Notebook)
            .where(Notebook.user_id == user_id)
            .options(selectinload(Notebook.cells))
            .order_by(Notebook.updated_at.desc())
        )
        notebooks = result.scalars().all()
        return [nb.to_dict() for nb in notebooks]
    
    async def get_notebook(self, notebook_id: str, user_id: int) -> Optional[Dict]:
        """获取单个 Notebook"""
        result = await self.db.execute(
            select(Notebook)
            .where(Notebook.id == notebook_id, Notebook.user_id == user_id)
            .options(selectinload(Notebook.cells))
        )
        notebook = result.scalar_one_or_none()
        return notebook.to_dict() if notebook else None
    
    async def get_notebook_model(self, notebook_id: str, user_id: int) -> Optional[Notebook]:
        """获取 Notebook 模型对象"""
        result = await self.db.execute(
            select(Notebook)
            .where(Notebook.id == notebook_id, Notebook.user_id == user_id)
            .options(selectinload(Notebook.cells))
        )
        return result.scalar_one_or_none()
    
    async def create_notebook(self, user_id: int, title: str, description: str = None, 
                            initial_cells: List[Dict] = None) -> Dict:
        """创建新的 Notebook"""
        notebook_id = str(uuid.uuid4())
        now = datetime.utcnow()
        
        notebook = Notebook(
            id=notebook_id,
            user_id=user_id,
            title=title,
            description=description,
            execution_count=0,
            created_at=now,
            updated_at=now,
        )
        
        # 准备单元格数据
        cells_data = []
        
        # 添加初始单元格
        if initial_cells:
            for i, cell_data in enumerate(initial_cells):
                cell = NotebookCell(
                    id=cell_data.get('id', str(uuid.uuid4())),
                    notebook_id=notebook_id,
                    cell_type=cell_data.get('cell_type', 'code'),
                    source=cell_data.get('source', ''),
                    outputs=cell_data.get('outputs', []),
                    execution_count=cell_data.get('execution_count'),
                    cell_metadata=cell_data.get('metadata', {}),
                    position=i,
                )
                notebook.cells.append(cell)
                cells_data.append(cell.to_dict())
        else:
            # 默认单元格
            default_cell = NotebookCell(
                id=str(uuid.uuid4()),
                notebook_id=notebook_id,
                cell_type='code',
                source='# 欢迎使用代码实验室！\n# 支持 Python、numpy、pandas、matplotlib、torch 等\n# Cell 之间共享变量，就像 Jupyter Notebook 一样\n\nimport numpy as np\nimport pandas as pd\nimport matplotlib.pyplot as plt\n\nprint("Hello, Code Lab!")\nx = 10  # 这个变量可以在后续 cell 中使用',
                outputs=[],
                position=0,
            )
            notebook.cells.append(default_cell)
            cells_data.append(default_cell.to_dict())
        
        self.db.add(notebook)
        await self.db.commit()
        
        # 直接构建返回字典，避免 lazy load 问题
        # 保持与 to_dict() 相同的格式：ISO 格式字符串
        return {
            "id": notebook_id,
            "user_id": user_id,
            "title": title,
            "description": description,
            "execution_count": 0,
            "metadata": {},
            "cells": cells_data,
            "created_at": now.isoformat() if now else None,
            "updated_at": now.isoformat() if now else None,
        }
    
    async def update_notebook(self, notebook_id: str, user_id: int, 
                            title: str = None, description: str = None) -> Optional[Dict]:
        """更新 Notebook 基本信息"""
        notebook = await self.get_notebook_model(notebook_id, user_id)
        if not notebook:
            return None
        
        if title is not None:
            notebook.title = title
        if description is not None:
            notebook.description = description
        notebook.updated_at = datetime.utcnow()
        
        await self.db.commit()
        
        # 重新查询以获取完整数据（包括 cells）
        return await self.get_notebook(notebook_id, user_id)
    
    async def delete_notebook(self, notebook_id: str, user_id: int) -> bool:
        """删除 Notebook"""
        result = await self.db.execute(
            delete(Notebook)
            .where(Notebook.id == notebook_id, Notebook.user_id == user_id)
        )
        await self.db.commit()
        return result.rowcount > 0
    
    async def add_cell(self, notebook_id: str, user_id: int, 
                      cell_type: str = 'code', source: str = '',
                      index: int = None) -> Optional[Dict]:
        """添加单元格"""
        notebook = await self.get_notebook_model(notebook_id, user_id)
        if not notebook:
            return None
        
        # 确定位置
        if index is None:
            index = len(notebook.cells)
        
        # 更新后续单元格的位置
        for cell in notebook.cells:
            if cell.position >= index:
                cell.position += 1
        
        # 创建新单元格
        cell = NotebookCell(
            id=str(uuid.uuid4()),
            notebook_id=notebook_id,
            cell_type=cell_type,
            source=source,
            outputs=[],
            position=index,
        )
        notebook.cells.append(cell)
        notebook.updated_at = datetime.utcnow()
        
        await self.db.commit()
        
        # 重新查询以获取完整数据
        return await self.get_notebook(notebook_id, user_id)
    
    async def update_cell(self, notebook_id: str, user_id: int, cell_id: str,
                         source: str = None, cell_type: str = None,
                         outputs: List = None, execution_count: int = None) -> Optional[Dict]:
        """更新单元格"""
        notebook = await self.get_notebook_model(notebook_id, user_id)
        if not notebook:
            return None
        
        for cell in notebook.cells:
            if cell.id == cell_id:
                if source is not None:
                    cell.source = source
                if cell_type is not None:
                    cell.cell_type = cell_type
                if outputs is not None:
                    cell.outputs = outputs
                if execution_count is not None:
                    cell.execution_count = execution_count
                cell.updated_at = datetime.utcnow()
                break
        
        notebook.updated_at = datetime.utcnow()
        await self.db.commit()
        
        # 重新查询以获取完整数据
        return await self.get_notebook(notebook_id, user_id)
    
    async def delete_cell(self, notebook_id: str, user_id: int, cell_id: str) -> Optional[Dict]:
        """删除单元格"""
        notebook = await self.get_notebook_model(notebook_id, user_id)
        if not notebook:
            return None
        
        # 找到并删除单元格
        cell_to_delete = None
        for cell in notebook.cells:
            if cell.id == cell_id:
                cell_to_delete = cell
                break
        
        if cell_to_delete:
            await self.db.delete(cell_to_delete)
            notebook.updated_at = datetime.utcnow()
            await self.db.commit()
        
        # 重新查询以获取完整数据
        return await self.get_notebook(notebook_id, user_id)
    
    async def move_cell(self, notebook_id: str, user_id: int, 
                       cell_id: str, new_index: int) -> Optional[Dict]:
        """移动单元格"""
        notebook = await self.get_notebook_model(notebook_id, user_id)
        if not notebook:
            return None
        
        # 找到单元格
        target_cell = None
        old_index = None
        for i, cell in enumerate(sorted(notebook.cells, key=lambda c: c.position)):
            if cell.id == cell_id:
                target_cell = cell
                old_index = cell.position
                break
        
        if not target_cell:
            return await self.get_notebook(notebook_id, user_id)
        
        # 更新位置
        if new_index > old_index:
            # 向下移动
            for cell in notebook.cells:
                if old_index < cell.position <= new_index:
                    cell.position -= 1
        else:
            # 向上移动
            for cell in notebook.cells:
                if new_index <= cell.position < old_index:
                    cell.position += 1
        
        target_cell.position = new_index
        notebook.updated_at = datetime.utcnow()
        
        await self.db.commit()
        
        # 重新查询以获取完整数据
        return await self.get_notebook(notebook_id, user_id)
    
    async def update_execution_count(self, notebook_id: str, user_id: int, 
                                    execution_count: int) -> Optional[Dict]:
        """更新执行计数"""
        notebook = await self.get_notebook_model(notebook_id, user_id)
        if not notebook:
            return None
        
        notebook.execution_count = execution_count
        notebook.updated_at = datetime.utcnow()
        
        await self.db.commit()
        
        # 重新查询以获取完整数据
        return await self.get_notebook(notebook_id, user_id)
    
    async def save_cell_execution(self, notebook_id: str, user_id: int, cell_id: str,
                                 outputs: List, execution_count: int) -> Optional[Dict]:
        """保存单元格执行结果"""
        notebook = await self.get_notebook_model(notebook_id, user_id)
        if not notebook:
            return None
        
        # 将 CellOutput 对象转换为字典
        serialized_outputs = []
        for output in outputs:
            if hasattr(output, 'model_dump'):
                serialized_outputs.append(output.model_dump())
            elif hasattr(output, 'dict'):
                serialized_outputs.append(output.dict())
            elif isinstance(output, dict):
                serialized_outputs.append(output)
            else:
                serialized_outputs.append({
                    'output_type': getattr(output, 'output_type', 'unknown'),
                    'content': getattr(output, 'content', str(output)),
                    'mime_type': getattr(output, 'mime_type', None),
                })
        
        for cell in notebook.cells:
            if cell.id == cell_id:
                cell.outputs = serialized_outputs
                cell.execution_count = execution_count
                cell.updated_at = datetime.utcnow()
                break
        
        notebook.execution_count = execution_count
        notebook.updated_at = datetime.utcnow()
        
        await self.db.commit()
        
        # 重新查询以获取完整数据
        return await self.get_notebook(notebook_id, user_id)


async def get_notebook_service(db: AsyncSession) -> NotebookService:
    """获取 NotebookService 实例"""
    return NotebookService(db)
