"""
FastAPI 主入口文件 - 多角色系统扩展版
"""
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import sys

from app.config import settings
from app.core.database import create_tables
from app.core.error_handlers import register_error_handlers
from app.core.rate_limit import build_rate_limit_dependency
from app.api import (
    auth, users, chat, health, knowledge, literature, codelab, agent, notebook_agent,
    admin, mentor, student, invitations, share, announcements, mcp
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
    logger.info(f"  MCP_ENABLED: {settings.mcp_enabled}")
    logger.info(f"  MCP_TOOL_PREFIX: {settings.mcp_tool_prefix}")
    logger.info(f"  MCP_CONFIG_PATH: {settings.mcp_config_path}")
    logger.info(f"  MCP_TOOL_ROUTES: {settings.mcp_tool_routes}")
    logger.info(f"  MCP_ROUTE_TIMEOUT_SECONDS: {settings.mcp_route_timeout_seconds}")
    logger.info(f"  MCP_ROUTE_RETRY_ATTEMPTS: {settings.mcp_route_retry_attempts}")
    logger.info(f"  CODE_EXECUTION_TIMEOUT: {settings.code_execution_timeout}s")
    logger.info(f"  KERNEL_IDLE_TIMEOUT: {settings.kernel_idle_timeout}s")
    logger.info(f"  NOTEBOOK_CONTEXT_CELLS: {settings.notebook_context_cells}")
    logger.info(f"  NOTEBOOK_CONTEXT_CELL_MAX_LENGTH: {settings.notebook_context_cell_max_length}")
    logger.info(f"  NOTEBOOK_CONTEXT_VARIABLES: {settings.notebook_context_variables}")
    logger.info("=" * 50)
    
    # 启动时建表仅用于开发/测试；生产请使用 Alembic
    if settings.auto_create_tables:
        logger.warning("AUTO_CREATE_TABLES=true，启动时将执行 create_tables()")
        await create_tables()
    else:
        logger.info("AUTO_CREATE_TABLES=false，跳过启动建表（推荐生产使用 Alembic）")
    
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
cors_allow_origins = settings.get_cors_allow_origins()
if "*" in cors_allow_origins:
    raise RuntimeError("不允许在 allow_credentials=true 时使用 CORS_ALLOW_ORIGINS=*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_error_handlers(app)

auth_rate_limit = build_rate_limit_dependency(
    bucket="auth",
    limit=int(getattr(settings, "api_rate_limit_auth_per_minute", 20)),
    scope="ip",
)
chat_rate_limit = build_rate_limit_dependency(
    bucket="chat",
    limit=int(getattr(settings, "api_rate_limit_chat_per_minute", 60)),
    scope="user_or_ip",
)
knowledge_rate_limit = build_rate_limit_dependency(
    bucket="knowledge",
    limit=int(getattr(settings, "api_rate_limit_knowledge_per_minute", 120)),
    scope="user_or_ip",
)
codelab_rate_limit = build_rate_limit_dependency(
    bucket="codelab",
    limit=int(getattr(settings, "api_rate_limit_codelab_per_minute", 30)),
    scope="user_or_ip",
)


@app.middleware("http")
async def attach_rate_limit_headers(request: Request, call_next):
    response = await call_next(request)
    headers = getattr(request.state, "rate_limit_headers", None)
    if isinstance(headers, dict):
        for key, value in headers.items():
            if value is not None and key not in response.headers:
                response.headers[key] = str(value)
    return response


# 注册原有路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(auth.router, prefix="/api/v1/auth", tags=["认证"], dependencies=[Depends(auth_rate_limit)])
app.include_router(users.router, prefix="/api/v1/users", tags=["用户"])
app.include_router(chat.router, prefix="/api/v1/chat", tags=["对话"], dependencies=[Depends(chat_rate_limit)])
app.include_router(knowledge.router, prefix="/api/v1/knowledge", tags=["知识库"], dependencies=[Depends(knowledge_rate_limit)])
app.include_router(literature.router, prefix="/api/v1", tags=["文献管理"])
app.include_router(codelab.router, prefix="/api/v1/codelab", tags=["代码实验室"], dependencies=[Depends(codelab_rate_limit)])
app.include_router(agent.router, prefix="/api/v1/codelab", tags=["Notebook Agent"], dependencies=[Depends(codelab_rate_limit)])
app.include_router(notebook_agent.router, prefix="/api/v1/codelab", tags=["Notebook ReAct Agent"], dependencies=[Depends(codelab_rate_limit)])
app.include_router(mcp.router, prefix="/api/v1/mcp", tags=["MCP 管理"])

# === 注册多角色系统路由 ===
app.include_router(admin.router, prefix="/api/v1/admin", tags=["管理员"])
app.include_router(mentor.router, prefix="/api/v1/mentor", tags=["导师"])
app.include_router(student.router, prefix="/api/v1/student", tags=["学生"])
app.include_router(invitations.router, prefix="/api/v1/invitations", tags=["邀请管理"])
app.include_router(share.router, prefix="/api/v1/share", tags=["资源共享"])
app.include_router(announcements.router, prefix="/api/v1/announcements", tags=["公告管理"])

# 注册文本分块路由
app.include_router(chunking_router, prefix="/api/v1/chunking", tags=["chunking"])


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
