#!/usr/bin/env python3
"""
数据库清理脚本 - 用于清理失败迁移后的残留数据

使用方法:
    python scripts/cleanup_failed_migration.py

警告: 此脚本会删除多角色系统相关的所有表和类型！
"""
import os
import sys
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://research_user:research_password_123@localhost:5432/research_assistant"
)

# 转换为异步URL
if DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
else:
    ASYNC_DATABASE_URL = DATABASE_URL


async def cleanup():
    """清理失败迁移的残留"""
    engine = create_async_engine(ASYNC_DATABASE_URL)
    
    print("=" * 50)
    print("多角色系统迁移清理脚本")
    print("=" * 50)
    
    confirm = input("\n警告: 此操作将删除多角色系统相关的所有数据!\n输入 'YES' 确认继续: ")
    if confirm != "YES":
        print("已取消")
        return
    
    async with engine.begin() as conn:
        # 删除表（按依赖顺序）
        tables = [
            "announcement_reads",
            "announcements", 
            "shared_resources",
            "invitations",
            "group_members",
            "research_groups",
        ]
        
        for table in tables:
            try:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
                print(f"✓ 已删除表: {table}")
            except Exception as e:
                print(f"✗ 删除表 {table} 失败: {e}")
        
        # 删除用户表的角色相关列
        columns = ["joined_at", "research_direction", "department", "mentor_id", "role"]
        for col in columns:
            try:
                await conn.execute(text(f"ALTER TABLE users DROP COLUMN IF EXISTS {col}"))
                print(f"✓ 已删除列: users.{col}")
            except Exception as e:
                print(f"✗ 删除列 users.{col} 失败: {e}")
        
        # 删除外键约束
        try:
            await conn.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS fk_users_mentor_id"))
            print("✓ 已删除外键约束: fk_users_mentor_id")
        except Exception as e:
            print(f"✗ 删除外键约束失败: {e}")
        
        # 删除索引
        indexes = ["ix_users_mentor_id", "ix_users_role"]
        for idx in indexes:
            try:
                await conn.execute(text(f"DROP INDEX IF EXISTS {idx}"))
                print(f"✓ 已删除索引: {idx}")
            except Exception as e:
                print(f"✗ 删除索引 {idx} 失败: {e}")
        
        # 删除枚举类型
        types = ["share_permission", "share_type", "invitation_status", "user_role"]
        for t in types:
            try:
                await conn.execute(text(f"DROP TYPE IF EXISTS {t} CASCADE"))
                print(f"✓ 已删除类型: {t}")
            except Exception as e:
                print(f"✗ 删除类型 {t} 失败: {e}")
        
        # 删除迁移记录
        try:
            await conn.execute(text("DELETE FROM alembic_version WHERE version_num = '006_multi_role'"))
            print("✓ 已清除迁移记录: 006_multi_role")
        except Exception as e:
            print(f"✗ 清除迁移记录失败: {e}")
    
    await engine.dispose()
    
    print("\n" + "=" * 50)
    print("清理完成！现在可以重新运行 alembic upgrade head")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(cleanup())
