"""
FastAPI 主入口文件 - 多角色系统扩展版
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import sys

from app.config import settings
from app.core.database import create_tables
from app.api import (
    auth, users, chat, health, knowledge, literature, codelab, agent, notebook_agent,
    admin, mentor, student, invitations, share, announcements
)

from app.api.chunking import router as chunking_router


# 配置日志
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG" if settings.debug else "INFO"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(f"🚀 启动 {settings.app_name} v{settings.app_version}")
    logger.info(f"📦 默认 LLM 提供商: {settings.default_llm_provider}")
    
    # 打印关键配置（验证环境变量是否生效）
    logger.info("=" * 50)
    logger.info("📋 当前配置:")
    logger.info(f"  LLM_TEMPERATURE: {settings.llm_temperature}")
    logger.info(f"  LLM_MAX_TOKENS: {settings.llm_max_tokens}")
    logger.info(f"  REACT_MAX_ITERATIONS: {settings.react_max_iterations}")
    logger.info(f"  REACT_OUTPUT_MAX_LENGTH: {settings.react_output_max_length}")
    logger.info(f"  CODE_EXECUTION_TIMEOUT: {settings.code_execution_timeout}s")
    logger.info(f"  KERNEL_IDLE_TIMEOUT: {settings.kernel_idle_timeout}s")
    logger.info(f"  NOTEBOOK_CONTEXT_CELLS: {settings.notebook_context_cells}")
    logger.info(f"  NOTEBOOK_CONTEXT_CELL_MAX_LENGTH: {settings.notebook_context_cell_max_length}")
    logger.info(f"  NOTEBOOK_CONTEXT_VARIABLES: {settings.notebook_context_variables}")
    logger.info("=" * 50)
    
    # 启动时创建表
    await create_tables()
    
    yield
    
    logger.info("👋 应用关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于 ReAct Agent 的综合科研助手平台 - 多角色系统",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册原有路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(users.router, prefix="/api/users", tags=["用户"])
app.include_router(chat.router, prefix="/api/chat", tags=["对话"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["知识库"])
app.include_router(literature.router, prefix="/api", tags=["文献管理"])
app.include_router(codelab.router, prefix="/api/codelab", tags=["代码实验室"])
app.include_router(agent.router, prefix="/api/codelab", tags=["Notebook Agent"])
app.include_router(notebook_agent.router, prefix="/api/codelab", tags=["Notebook ReAct Agent"])

# === 注册多角色系统路由 ===
app.include_router(admin.router, prefix="/api/admin", tags=["管理员"])
app.include_router(mentor.router, prefix="/api/mentor", tags=["导师"])
app.include_router(student.router, prefix="/api/student", tags=["学生"])
app.include_router(invitations.router, prefix="/api/invitations", tags=["邀请管理"])
app.include_router(share.router, prefix="/api/share", tags=["资源共享"])
app.include_router(announcements.router, prefix="/api/announcements", tags=["公告管理"])

# 注册文本分块路由
app.include_router(chunking_router, prefix="/api/chunking", tags=["chunking"])


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "features": [
            "multi-role-system",
            "mentor-student-relation",
            "research-groups",
            "resource-sharing",
            "announcements"
        ]
    }
