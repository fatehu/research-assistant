#!/usr/bin/env python3
"""
创建管理员账户脚本

用法:
    python scripts/create_admin.py
    
或者指定参数:
    python scripts/create_admin.py --email admin@example.com --username admin --password admin123
"""
import asyncio
import argparse
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from passlib.context import CryptContext

from app.config import settings
from app.models.user import User
from app.models.role import UserRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def create_admin(email: str, username: str, password: str, full_name: str = None):
    """创建管理员账户"""
    # 创建数据库连接
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as db:
        # 检查是否已存在
        result = await db.execute(
            select(User).where(
                (User.email == email) | (User.username == username)
            )
        )
        existing = result.scalar_one_or_none()
        
        if existing:
            if existing.email == email:
                print(f"❌ 错误: 邮箱 {email} 已被使用")
            else:
                print(f"❌ 错误: 用户名 {username} 已被使用")
            
            # 如果已存在的用户不是管理员，询问是否升级
            if existing.role != UserRole.ADMIN:
                confirm = input(f"是否将用户 {existing.username} 升级为管理员? (y/n): ")
                if confirm.lower() == 'y':
                    existing.role = UserRole.ADMIN
                    await db.commit()
                    print(f"✅ 用户 {existing.username} 已升级为管理员")
            return
        
        # 创建新管理员
        hashed_password = pwd_context.hash(password)
        admin = User(
            email=email,
            username=username,
            hashed_password=hashed_password,
            full_name=full_name or "System Administrator",
            role=UserRole.ADMIN,
            is_active=True,
            is_superuser=True,
        )
        
        db.add(admin)
        await db.commit()
        
        print(f"✅ 管理员账户创建成功!")
        print(f"   邮箱: {email}")
        print(f"   用户名: {username}")
        print(f"   角色: 管理员")
    
    await engine.dispose()


async def list_admins():
    """列出所有管理员"""
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as db:
        result = await db.execute(
            select(User).where(User.role == UserRole.ADMIN)
        )
        admins = result.scalars().all()
        
        if not admins:
            print("⚠️ 系统中没有管理员账户")
        else:
            print(f"📋 管理员列表 (共 {len(admins)} 人):")
            for admin in admins:
                status = "✅ 活跃" if admin.is_active else "❌ 禁用"
                print(f"   - {admin.username} ({admin.email}) {status}")
    
    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="创建管理员账户")
    parser.add_argument("--email", "-e", help="管理员邮箱")
    parser.add_argument("--username", "-u", help="管理员用户名")
    parser.add_argument("--password", "-p", help="管理员密码")
    parser.add_argument("--name", "-n", help="管理员姓名")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有管理员")
    
    args = parser.parse_args()
    
    if args.list:
        asyncio.run(list_admins())
        return
    
    # 如果没有提供参数，交互式输入
    email = args.email
    username = args.username
    password = args.password
    full_name = args.name
    
    if not email:
        email = input("请输入管理员邮箱: ").strip()
        if not email:
            print("❌ 邮箱不能为空")
            return
    
    if not username:
        username = input("请输入管理员用户名: ").strip()
        if not username:
            print("❌ 用户名不能为空")
            return
    
    if not password:
        import getpass
        password = getpass.getpass("请输入管理员密码: ")
        if not password:
            print("❌ 密码不能为空")
            return
        if len(password) < 6:
            print("❌ 密码长度至少6位")
            return
        
        password_confirm = getpass.getpass("请再次输入密码: ")
        if password != password_confirm:
            print("❌ 两次输入的密码不一致")
            return
    
    if not full_name:
        full_name = input("请输入管理员姓名 (可选，直接回车跳过): ").strip() or None
    
    asyncio.run(create_admin(email, username, password, full_name))


if __name__ == "__main__":
    main()
