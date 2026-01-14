"""
用户路由
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import get_current_user, get_password_hash
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdate

router = APIRouter()


@router.get("/profile", response_model=UserResponse)
async def get_profile(current_user: User = Depends(get_current_user)):
    """获取用户资料"""
    return UserResponse.model_validate(current_user)


@router.put("/profile", response_model=UserResponse)
async def update_profile(
    user_data: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新用户资料"""
    update_data = user_data.model_dump(exclude_unset=True)
    
    for field, value in update_data.items():
        setattr(current_user, field, value)
    
    await db.commit()
    await db.refresh(current_user)
    
    return UserResponse.model_validate(current_user)


@router.put("/password")
async def change_password(
    old_password: str,
    new_password: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改密码"""
    from app.core.security import verify_password
    
    if not verify_password(old_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原密码错误"
        )
    
    current_user.hashed_password = get_password_hash(new_password)
    await db.commit()
    
    return {"message": "密码修改成功"}


@router.get("/llm-providers")
async def get_llm_providers(current_user: User = Depends(get_current_user)):
    """获取可用的 LLM 提供商列表"""
    from app.config import settings
    
    providers = [
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "model": settings.deepseek_model,
            "available": bool(settings.deepseek_api_key),
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "model": settings.openai_model,
            "available": bool(settings.openai_api_key),
        },
        {
            "id": "aliyun",
            "name": "阿里云通义",
            "model": settings.aliyun_model,
            "available": bool(settings.aliyun_api_key),
        },
        {
            "id": "ollama",
            "name": "Ollama (本地)",
            "model": settings.ollama_model,
            "available": True,  # Ollama 不需要 API key
        },
    ]
    
    return {
        "default": current_user.preferred_llm_provider or settings.default_llm_provider,
        "providers": providers
    }
