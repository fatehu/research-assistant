"""
健康检查路由
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.database import get_db
from app.config import settings

router = APIRouter()


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """健康检查"""
    # 检查数据库连接
    try:
        await db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    
    return {
        "status": "healthy" if db_status == "healthy" else "degraded",
        "app": settings.app_name,
        "version": settings.app_version,
        "database": db_status,
        "llm_provider": settings.default_llm_provider,
    }


@router.get("/health/llm")
async def llm_health_check():
    """LLM 服务健康检查"""
    from app.services.llm_service import LLMService
    
    llm_service = LLMService()
    
    providers_status = {}
    for provider in ["deepseek", "openai", "aliyun"]:
        try:
            # 简单测试
            config = settings.get_llm_config(provider)
            if config["api_key"]:
                providers_status[provider] = "configured"
            else:
                providers_status[provider] = "not_configured"
        except Exception as e:
            providers_status[provider] = f"error: {str(e)}"
    
    return {
        "default_provider": settings.default_llm_provider,
        "providers": providers_status
    }
