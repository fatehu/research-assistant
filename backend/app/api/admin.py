"""
管理员路由
"""
import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.orm import aliased
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user, get_password_hash
from app.core.permissions import get_admin_user
from app.models.user import User
from app.models.role import (
    UserRole,
    ResearchGroup,
    GroupMember,
    Invitation,
    InvitationStatus,
    SharedResource,
    Announcement,
    AdminAuditLog,
)
from app.models.conversation import Conversation, Message, MessageRole
from app.models.agent import AgentRun
from app.models.knowledge import KnowledgeBase, Document, DocumentStatus
from app.models.literature import (
    KnowledgeLinkStatus,
    LiteratureQAMessage,
    LiteratureQASession,
    Paper,
    PaperAnnotation,
    PaperCollection,
    PaperComment,
    PaperKnowledgeLink,
    PaperRating,
    PaperReadSession,
)
from app.models.notebook import Notebook, NotebookCell
from app.schemas.role import (
    UserListResponse, UserAdminUpdate, UserRoleUpdate, SystemStatistics, StatisticsDetailResponse,
    AdminAuditLogResponse,
    AdminCreateUserRequest, UserPasswordUpdate
)

router = APIRouter()

SHARE_TYPE_LABELS = {
    "knowledge_base": "知识库共享",
    "paper_collection": "文献集共享",
    "paper": "论文共享",
    "notebook": "Notebook 共享",
}

INVITATION_STATUS_LABELS = {
    InvitationStatus.PENDING.value: "待处理",
    InvitationStatus.ACCEPTED.value: "已接受",
    InvitationStatus.REJECTED.value: "已拒绝",
    InvitationStatus.CANCELLED.value: "已取消",
}

ACTIVITY_TYPE_LABELS = {
    "conversation": "对话",
    "knowledge_base": "知识库",
    "paper": "论文",
    "notebook": "Notebook",
    "announcement": "公告",
}

KNOWLEDGE_LINK_STATUS_LABELS = {
    KnowledgeLinkStatus.PENDING.value: "待处理",
    KnowledgeLinkStatus.RUNNING.value: "处理中",
    KnowledgeLinkStatus.COMPLETED.value: "已完成",
    KnowledgeLinkStatus.FAILED.value: "失败",
    KnowledgeLinkStatus.TIMEOUT.value: "超时",
    KnowledgeLinkStatus.CANCELLED.value: "已取消",
}

AI_AGENT_SUCCESS_STATUSES = {"success", "completed"}
CODELAB_AGENT_CHANNELS = {"codelab_agent", "notebook_agent"}


def _normalize_day_key(value) -> str:
    """将数据库返回的日期字段规整为 YYYY-MM-DD。"""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


async def _count_created_since(db: AsyncSession, model, column, since: datetime) -> int:
    count = await db.scalar(select(func.count()).select_from(model).where(column >= since))
    return int(count or 0)


async def _build_daily_count_series(
    db: AsyncSession,
    model,
    column,
    days: int = 7,
):
    start_date = datetime.utcnow().date() - timedelta(days=days - 1)
    start_dt = datetime.combine(start_date, datetime.min.time())
    rows = (
        await db.execute(
            select(func.date(column), func.count())
            .select_from(model)
            .where(column >= start_dt)
            .group_by(func.date(column))
            .order_by(func.date(column))
        )
    ).all()
    counts_by_day = {_normalize_day_key(row[0]): int(row[1] or 0) for row in rows}

    return [
        {"date": day.isoformat(), "count": counts_by_day.get(day.isoformat(), 0)}
        for day in (start_date + timedelta(days=offset) for offset in range(days))
    ]


async def _build_breakdown(db: AsyncSession, model, field, labels: dict[str, str]):
    rows = (
        await db.execute(
            select(field, func.count())
            .select_from(model)
            .group_by(field)
            .order_by(func.count().desc(), field)
        )
    ).all()
    return [
        {
            "key": str(row[0] or "unknown"),
            "label": labels.get(str(row[0] or "unknown"), str(row[0] or "unknown")),
            "count": int(row[1] or 0),
        }
        for row in rows
    ]


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _build_ai_rag_statistics(message_rows, agent_run_rows):
    stats = {
        "assistant_messages_last_window": 0,
        "rag_messages_last_window": 0,
        "knowledge_search_calls_last_window": 0,
        "citation_required_answers_last_window": 0,
        "citation_valid_answers_last_window": 0,
        "citation_repair_attempts_last_window": 0,
        "citation_repair_successes_last_window": 0,
        "compression_calls_last_window": 0,
        "compression_fallback_chunks_last_window": 0,
        "assistant_total_tokens_last_window": 0,
        "agent_runs_last_window": 0,
        "successful_agent_runs_last_window": 0,
    }

    for row in message_rows:
        metadata = row[0] if len(row) > 0 else None
        total_tokens = row[1] if len(row) > 1 else 0
        stats["assistant_messages_last_window"] += 1
        stats["assistant_total_tokens_last_window"] += _to_int(total_tokens)

        if not isinstance(metadata, dict):
            continue

        rag_metrics = metadata.get("rag_metrics")
        if not isinstance(rag_metrics, dict):
            continue

        stats["rag_messages_last_window"] += 1
        stats["knowledge_search_calls_last_window"] += _to_int(rag_metrics.get("knowledge_search_calls"))
        if bool(rag_metrics.get("citation_required")):
            stats["citation_required_answers_last_window"] += 1
            if bool(rag_metrics.get("citation_valid")):
                stats["citation_valid_answers_last_window"] += 1
        stats["citation_repair_attempts_last_window"] += _to_int(rag_metrics.get("citation_repair_attempts"))
        stats["citation_repair_successes_last_window"] += _to_int(rag_metrics.get("citation_repair_successes"))
        stats["compression_calls_last_window"] += _to_int(rag_metrics.get("compression_calls"))
        stats["compression_fallback_chunks_last_window"] += _to_int(rag_metrics.get("compression_fallback_chunks"))

    for row in agent_run_rows:
        status = str(row[1] or "").strip().lower() if len(row) > 1 else ""
        stats["agent_runs_last_window"] += 1
        if status in AI_AGENT_SUCCESS_STATUSES:
            stats["successful_agent_runs_last_window"] += 1

    return stats


def _build_codelab_statistics(
    *,
    notebooks_active_last_window: int,
    executed_notebooks: int,
    total_execution_count: int,
    code_cells: int,
    executed_code_cells: int,
    agent_run_rows,
):
    agent_runs = 0
    agent_tokens = 0
    for row in agent_run_rows:
        channel = str(row[0] or "").strip()
        if channel not in CODELAB_AGENT_CHANNELS:
            continue
        agent_runs += 1
        agent_tokens += _to_int(row[2] if len(row) > 2 else 0)

    return {
        "notebooks_active_last_window": _to_int(notebooks_active_last_window),
        "executed_notebooks": _to_int(executed_notebooks),
        "total_execution_count": _to_int(total_execution_count),
        "code_cells": _to_int(code_cells),
        "executed_code_cells": _to_int(executed_code_cells),
        "agent_runs_last_window": agent_runs,
        "agent_tokens_last_window": agent_tokens,
    }


async def _record_admin_audit(
    db: AsyncSession,
    *,
    admin_user: User,
    action: str,
    summary: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict | None = None,
):
    db.add(
        AdminAuditLog(
            admin_user_id=int(admin_user.id),
            action=str(action),
            target_type=str(target_type) if target_type else None,
            target_id=str(target_id) if target_id is not None else None,
            summary=str(summary),
            details=details or {},
        )
    )


def _build_statistics_summary_rows(stats: SystemStatistics):
    rows = [
        ("time_window_days", stats.time_window_days),
        ("total_users", stats.total_users),
        ("admin_count", stats.admin_count),
        ("mentor_count", stats.mentor_count),
        ("student_count", stats.student_count),
        ("active_users", stats.active_users),
        ("inactive_users", stats.inactive_users),
        ("total_conversations", stats.total_conversations),
        ("total_knowledge_bases", stats.total_knowledge_bases),
        ("total_documents", stats.total_documents),
        ("total_papers", stats.total_papers),
        ("total_notebooks", stats.total_notebooks),
        ("total_groups", stats.total_groups),
        ("pending_invitations", stats.pending_invitations),
        ("total_shared_resources", stats.total_shared_resources),
        ("total_announcements", stats.total_announcements),
        ("students_with_mentor", stats.students_with_mentor),
        ("students_without_mentor", stats.students_without_mentor),
        ("activity.new_users_last_7_days", stats.activity.new_users_last_7_days),
        ("activity.new_conversations_last_7_days", stats.activity.new_conversations_last_7_days),
        ("activity.new_knowledge_bases_last_7_days", stats.activity.new_knowledge_bases_last_7_days),
        ("activity.new_papers_last_7_days", stats.activity.new_papers_last_7_days),
        ("activity.new_notebooks_last_7_days", stats.activity.new_notebooks_last_7_days),
        ("document_pipeline.completed_documents", stats.document_pipeline.completed_documents),
        ("document_pipeline.running_documents", stats.document_pipeline.running_documents),
        ("document_pipeline.failed_documents", stats.document_pipeline.failed_documents),
        ("document_pipeline.pending_documents", stats.document_pipeline.pending_documents),
        ("document_pipeline.timeout_documents", stats.document_pipeline.timeout_documents),
        ("document_pipeline.cancelled_documents", stats.document_pipeline.cancelled_documents),
        ("ai_rag.agent_runs_last_window", stats.ai_rag.agent_runs_last_window),
        ("ai_rag.successful_agent_runs_last_window", stats.ai_rag.successful_agent_runs_last_window),
        ("ai_rag.rag_messages_last_window", stats.ai_rag.rag_messages_last_window),
        ("ai_rag.knowledge_search_calls_last_window", stats.ai_rag.knowledge_search_calls_last_window),
        ("ai_rag.citation_valid_answers_last_window", stats.ai_rag.citation_valid_answers_last_window),
        ("ai_rag.citation_required_answers_last_window", stats.ai_rag.citation_required_answers_last_window),
        ("ai_rag.compression_calls_last_window", stats.ai_rag.compression_calls_last_window),
        ("ai_rag.compression_fallback_chunks_last_window", stats.ai_rag.compression_fallback_chunks_last_window),
        ("ai_rag.assistant_total_tokens_last_window", stats.ai_rag.assistant_total_tokens_last_window),
        ("codelab.notebooks_active_last_window", stats.codelab.notebooks_active_last_window),
        ("codelab.executed_notebooks", stats.codelab.executed_notebooks),
        ("codelab.total_execution_count", stats.codelab.total_execution_count),
        ("codelab.code_cells", stats.codelab.code_cells),
        ("codelab.executed_code_cells", stats.codelab.executed_code_cells),
        ("codelab.agent_runs_last_window", stats.codelab.agent_runs_last_window),
        ("codelab.agent_tokens_last_window", stats.codelab.agent_tokens_last_window),
        ("literature.total_collections", stats.literature.total_collections),
        ("literature.active_read_sessions_last_window", stats.literature.active_read_sessions_last_window),
        ("literature.annotations_last_window", stats.literature.annotations_last_window),
        ("literature.comments_last_window", stats.literature.comments_last_window),
        ("literature.ratings_last_window", stats.literature.ratings_last_window),
        ("literature.qa_sessions_last_window", stats.literature.qa_sessions_last_window),
        ("literature.qa_messages_last_window", stats.literature.qa_messages_last_window),
        ("literature.knowledge_links_total", stats.literature.knowledge_links_total),
    ]
    for item in stats.share_breakdown:
        rows.append((f"share_breakdown.{item.key}", item.count))
    for item in stats.invitation_breakdown:
        rows.append((f"invitation_breakdown.{item.key}", item.count))
    for item in stats.literature.knowledge_link_breakdown:
        rows.append((f"literature.knowledge_link_breakdown.{item.key}", item.count))
    return rows


def _csv_response(filename: str, headers: list[str], rows: list[list | tuple]) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _build_top_mentors(db: AsyncSession, limit: int = 5):
    student_alias = aliased(User)
    rows = (
        await db.execute(
            select(
                User.id,
                User.username,
                User.full_name,
                func.count(func.distinct(student_alias.id)).label("student_count"),
                func.count(func.distinct(ResearchGroup.id)).label("group_count"),
            )
            .select_from(User)
            .outerjoin(student_alias, student_alias.mentor_id == User.id)
            .outerjoin(ResearchGroup, ResearchGroup.mentor_id == User.id)
            .where(User.role == UserRole.MENTOR.value)
            .group_by(User.id, User.username, User.full_name)
            .order_by(desc("student_count"), desc("group_count"), User.username.asc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "mentor_id": int(row[0]),
            "username": str(row[1]),
            "full_name": row[2],
            "student_count": int(row[3] or 0),
            "group_count": int(row[4] or 0),
        }
        for row in rows
    ]


async def _build_recent_activity(db: AsyncSession, limit: int = 10):
    conversation_rows = (
        await db.execute(
            select(Conversation.id, Conversation.title, Conversation.created_at, User.username, User.role)
            .join(User, User.id == Conversation.user_id)
            .order_by(Conversation.created_at.desc())
            .limit(limit)
        )
    ).all()
    knowledge_rows = (
        await db.execute(
            select(KnowledgeBase.id, KnowledgeBase.name, KnowledgeBase.created_at, User.username, User.role)
            .join(User, User.id == KnowledgeBase.user_id)
            .order_by(KnowledgeBase.created_at.desc())
            .limit(limit)
        )
    ).all()
    paper_rows = (
        await db.execute(
            select(Paper.id, Paper.title, Paper.created_at, User.username, User.role)
            .join(User, User.id == Paper.user_id)
            .order_by(Paper.created_at.desc())
            .limit(limit)
        )
    ).all()
    notebook_rows = (
        await db.execute(
            select(Notebook.id, Notebook.title, Notebook.created_at, User.username, User.role)
            .join(User, User.id == Notebook.user_id)
            .order_by(Notebook.created_at.desc())
            .limit(limit)
        )
    ).all()
    announcement_rows = (
        await db.execute(
            select(Announcement.id, Announcement.title, Announcement.created_at, User.username, User.role)
            .join(User, User.id == Announcement.mentor_id)
            .order_by(Announcement.created_at.desc())
            .limit(limit)
        )
    ).all()

    items = []
    for row in conversation_rows:
        items.append(
            {
                "id": f"conversation-{row[0]}",
                "type": "conversation",
                "title": row[1] or f"{ACTIVITY_TYPE_LABELS['conversation']} #{row[0]}",
                "owner_name": row[3],
                "owner_role": row[4],
                "created_at": row[2],
            }
        )
    for row in knowledge_rows:
        items.append(
            {
                "id": f"knowledge-{row[0]}",
                "type": "knowledge_base",
                "title": row[1] or f"{ACTIVITY_TYPE_LABELS['knowledge_base']} #{row[0]}",
                "owner_name": row[3],
                "owner_role": row[4],
                "created_at": row[2],
            }
        )
    for row in paper_rows:
        items.append(
            {
                "id": f"paper-{row[0]}",
                "type": "paper",
                "title": row[1] or f"{ACTIVITY_TYPE_LABELS['paper']} #{row[0]}",
                "owner_name": row[3],
                "owner_role": row[4],
                "created_at": row[2],
            }
        )
    for row in notebook_rows:
        items.append(
            {
                "id": f"notebook-{row[0]}",
                "type": "notebook",
                "title": row[1] or f"{ACTIVITY_TYPE_LABELS['notebook']} {row[0]}",
                "owner_name": row[3],
                "owner_role": row[4],
                "created_at": row[2],
            }
        )
    for row in announcement_rows:
        items.append(
            {
                "id": f"announcement-{row[0]}",
                "type": "announcement",
                "title": row[1] or f"{ACTIVITY_TYPE_LABELS['announcement']} #{row[0]}",
                "owner_name": row[3],
                "owner_role": row[4],
                "created_at": row[2],
            }
        )

    items.sort(key=lambda item: item["created_at"] or datetime.min, reverse=True)
    return items[:limit]


async def _resolve_share_resource_name(db: AsyncSession, resource_type: str, resource_id: str) -> str:
    if resource_type == "knowledge_base":
        try:
            target_id = int(resource_id)
        except (TypeError, ValueError):
            return resource_id
        row = (
            await db.execute(select(KnowledgeBase.name).where(KnowledgeBase.id == target_id))
        ).first()
        return row[0] if row else resource_id
    if resource_type == "paper_collection":
        try:
            target_id = int(resource_id)
        except (TypeError, ValueError):
            return resource_id
        row = (
            await db.execute(select(PaperCollection.name).where(PaperCollection.id == target_id))
        ).first()
        return row[0] if row else resource_id
    if resource_type == "paper":
        try:
            target_id = int(resource_id)
        except (TypeError, ValueError):
            return resource_id
        row = (
            await db.execute(select(Paper.title).where(Paper.id == target_id))
        ).first()
        return row[0] if row else resource_id
    if resource_type == "notebook":
        row = (
            await db.execute(select(Notebook.title).where(Notebook.id == resource_id))
        ).first()
        return row[0] if row else resource_id
    return resource_id


async def _resolve_share_target_name(db: AsyncSession, shared_with_type: str, shared_with_id: int | None) -> str:
    if shared_with_type == "user" and shared_with_id is not None:
        row = (
            await db.execute(
                select(User.full_name, User.username).where(User.id == shared_with_id)
            )
        ).first()
        if row:
            return row[0] or row[1]
        return f"用户#{shared_with_id}"
    if shared_with_type == "group" and shared_with_id is not None:
        row = (
            await db.execute(
                select(ResearchGroup.name).where(ResearchGroup.id == shared_with_id)
            )
        ).first()
        return row[0] if row else f"研究组#{shared_with_id}"
    if shared_with_type == "all_students":
        return "所有学生"
    return "-"


@router.get("/users", response_model=list[UserListResponse])
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    role: UserRole = None,
    search: str = None,
    is_active: bool = None,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户列表（管理员）"""
    query = select(User)
    
    # 筛选条件
    if role:
        query = query.where(User.role == role)
    if is_active is not None:
        query = query.where(User.is_active == is_active)
    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            or_(
                User.username.ilike(search_pattern),
                User.email.ilike(search_pattern),
                User.full_name.ilike(search_pattern)
            )
        )
    
    query = query.order_by(User.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()
    
    return [UserListResponse.model_validate(u) for u in users]


@router.post("/users", response_model=UserListResponse)
async def create_user(
    data: AdminCreateUserRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """管理员创建用户"""
    # 检查邮箱是否已存在
    result = await db.execute(
        select(User).where(User.email == data.email)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该邮箱已被注册")
    
    # 检查用户名是否已存在
    result = await db.execute(
        select(User).where(User.username == data.username)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该用户名已被使用")
    
    # 创建用户
    new_user = User(
        email=data.email,
        username=data.username,
        hashed_password=get_password_hash(data.password),
        full_name=data.full_name,
        role=data.role or "student",
        department=data.department,
        research_direction=data.research_direction,
        is_active=True
    )
    
    db.add(new_user)
    await db.flush()
    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="create_user",
        target_type="user",
        target_id=new_user.id,
        summary=f"创建用户 {new_user.username}",
        details={
            "email": new_user.email,
            "username": new_user.username,
            "role": new_user.role,
        },
    )
    await db.commit()
    await db.refresh(new_user)
    
    logger.info(f"管理员 {current_user.username} 创建了用户 {new_user.username} (角色: {new_user.role})")
    
    return UserListResponse.model_validate(new_user)


@router.get("/users/count")
async def get_user_count(
    role: UserRole = None,
    search: str = None,
    is_active: bool = None,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户数量"""
    query = select(func.count(User.id))
    
    if role:
        query = query.where(User.role == role)
    if is_active is not None:
        query = query.where(User.is_active == is_active)
    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            or_(
                User.username.ilike(search_pattern),
                User.email.ilike(search_pattern),
                User.full_name.ilike(search_pattern)
            )
        )
    
    count = await db.scalar(query)
    return {"count": count}


@router.get("/users/{user_id}", response_model=UserListResponse)
async def get_user(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户详情（管理员）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    return UserListResponse.model_validate(user)


@router.put("/users/{user_id}", response_model=UserListResponse)
async def update_user(
    user_id: int,
    data: UserAdminUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """更新用户信息（管理员）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 更新字段
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="update_user",
        target_type="user",
        target_id=user.id,
        summary=f"更新用户 {user.username} 信息",
        details=update_data,
    )
    await db.commit()
    await db.refresh(user)
    
    logger.info(f"管理员 {current_user.username} 更新了用户 {user.username} 的信息")
    
    return UserListResponse.model_validate(user)


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    data: UserRoleUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """修改用户角色（管理员）"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    old_role = user.role
    new_role = data.role
    
    # 如果从学生变为导师，需要解除与原导师的关系
    if old_role == UserRole.STUDENT.value and new_role == UserRole.MENTOR.value:
        user.mentor_id = None
        user.joined_at = None
    
    # 如果从导师变为学生，需要处理其名下学生
    if old_role == UserRole.MENTOR.value and new_role == UserRole.STUDENT.value:
        # 将其名下学生的 mentor_id 设为 NULL
        await db.execute(
            select(User).where(User.mentor_id == user_id)
        )
        students = (await db.execute(
            select(User).where(User.mentor_id == user_id)
        )).scalars().all()
        
        for student in students:
            student.mentor_id = None
            student.joined_at = None
        
        logger.info(f"导师 {user.username} 角色变更，{len(students)} 名学生已解除关联")
    
    user.role = new_role
    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="update_user_role",
        target_type="user",
        target_id=user.id,
        summary=f"修改用户 {user.username} 角色为 {new_role}",
        details={
            "old_role": old_role,
            "new_role": new_role,
        },
    )
    await db.commit()
    
    logger.info(f"管理员 {current_user.username} 将用户 {user.username} 的角色从 {old_role} 修改为 {new_role}")
    
    return {"message": f"用户角色已更新为 {new_role}"}


@router.put("/users/{user_id}")
async def update_user_info(
    user_id: int,
    data: UserAdminUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """更新用户信息（管理员）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 更新字段
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.department is not None:
        user.department = data.department
    if data.research_direction is not None:
        user.research_direction = data.research_direction
    if data.is_active is not None:
        if user_id == current_user.id and not data.is_active:
            raise HTTPException(status_code=400, detail="不能禁用自己")
        user.is_active = data.is_active
    if data.role is not None:
        # 处理角色变更逻辑
        old_role = user.role
        new_role = data.role.value if hasattr(data.role, 'value') else data.role
        
        if old_role == UserRole.MENTOR.value and new_role == UserRole.STUDENT.value:
            # 从导师变为学生，解除与学生的关联
            students = (await db.execute(
                select(User).where(User.mentor_id == user_id)
            )).scalars().all()
            for student in students:
                student.mentor_id = None
                student.joined_at = None
        
        user.role = new_role

    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="update_user_info",
        target_type="user",
        target_id=user.id,
        summary=f"更新用户 {user.username} 信息",
        details=data.model_dump(exclude_unset=True),
    )
    await db.commit()
    await db.refresh(user)
    
    logger.info(f"管理员 {current_user.username} 更新了用户 {user.username} 的信息")
    
    return UserListResponse.model_validate(user)


@router.put("/users/{user_id}/password")
async def update_user_password(
    user_id: int,
    data: UserPasswordUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """修改用户密码（管理员）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 更新密码
    user.hashed_password = get_password_hash(data.password)

    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="update_user_password",
        target_type="user",
        target_id=user.id,
        summary=f"重置用户 {user.username} 密码",
        details={},
    )
    await db.commit()
    
    logger.info(f"管理员 {current_user.username} 修改了用户 {user.username} 的密码")
    
    return {"message": "密码修改成功"}


@router.put("/users/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """切换用户状态（管理员）"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能禁用自己")
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    user.is_active = not user.is_active
    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="toggle_user_active",
        target_type="user",
        target_id=user.id,
        summary=f"{'启用' if user.is_active else '禁用'}用户 {user.username}",
        details={"is_active": bool(user.is_active)},
    )
    await db.commit()
    
    action = "启用" if user.is_active else "禁用"
    logger.info(f"管理员 {current_user.username} {action}了用户 {user.username}")
    
    return {"message": f"用户已{action}", "is_active": user.is_active}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """删除用户（管理员）"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    username = user.username
    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="delete_user",
        target_type="user",
        target_id=user.id,
        summary=f"删除用户 {username}",
        details={"username": username, "email": user.email},
    )
    await db.delete(user)
    await db.commit()
    
    logger.info(f"管理员 {current_user.username} 删除了用户 {username}")
    
    return {"message": "用户已删除"}


@router.get("/audit-logs", response_model=AdminAuditLogResponse)
async def get_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    action: str | None = Query(None),
    search: str | None = Query(None),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """获取管理员审计日志。"""
    offset = (page - 1) * page_size
    admin_alias = aliased(User)

    query = (
        select(
            AdminAuditLog.id,
            AdminAuditLog.action,
            AdminAuditLog.target_type,
            AdminAuditLog.target_id,
            AdminAuditLog.summary,
            AdminAuditLog.created_at,
            admin_alias.full_name,
            admin_alias.username,
        )
        .select_from(AdminAuditLog)
        .join(admin_alias, admin_alias.id == AdminAuditLog.admin_user_id)
    )
    total_query = (
        select(func.count(AdminAuditLog.id))
        .select_from(AdminAuditLog)
        .join(admin_alias, admin_alias.id == AdminAuditLog.admin_user_id)
    )

    if action:
        query = query.where(AdminAuditLog.action == action)
        total_query = total_query.where(AdminAuditLog.action == action)
    if search:
        pattern = f"%{search}%"
        filters = or_(
            AdminAuditLog.summary.ilike(pattern),
            AdminAuditLog.target_type.ilike(pattern),
            AdminAuditLog.target_id.ilike(pattern),
            admin_alias.username.ilike(pattern),
            admin_alias.full_name.ilike(pattern),
        )
        query = query.where(filters)
        total_query = total_query.where(filters)

    rows = (
        await db.execute(
            query.order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc()).offset(offset).limit(page_size)
        )
    ).all()
    total = int(await db.scalar(total_query) or 0)

    return AdminAuditLogResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            {
                "id": int(row[0]),
                "action": str(row[1]),
                "target_type": row[2],
                "target_id": row[3],
                "summary": str(row[4]),
                "created_at": row[5],
                "admin_name": row[6] or row[7],
            }
            for row in rows
        ],
    )


@router.get("/statistics", response_model=SystemStatistics)
async def get_statistics(
    days: int = Query(7, ge=7, le=90),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取系统统计（管理员）"""
    window_start = datetime.utcnow() - timedelta(days=days)

    # 用户统计
    total_users = await db.scalar(select(func.count(User.id)))
    admin_count = await db.scalar(
        select(func.count(User.id)).where(User.role == UserRole.ADMIN.value)
    )
    mentor_count = await db.scalar(
        select(func.count(User.id)).where(User.role == UserRole.MENTOR.value)
    )
    student_count = await db.scalar(
        select(func.count(User.id)).where(User.role == UserRole.STUDENT.value)
    )
    active_users = await db.scalar(
        select(func.count(User.id)).where(User.is_active == True)
    )

    # 资源统计
    total_conversations = await db.scalar(select(func.count(Conversation.id)))
    total_knowledge_bases = await db.scalar(select(func.count(KnowledgeBase.id)))
    total_documents = await db.scalar(select(func.count(Document.id)))
    total_papers = await db.scalar(select(func.count(Paper.id)))
    total_notebooks = await db.scalar(select(func.count(Notebook.id)))

    # 协作统计
    total_groups = await db.scalar(select(func.count(ResearchGroup.id)))
    active_groups = await db.scalar(
        select(func.count(ResearchGroup.id)).where(ResearchGroup.is_active == True)
    )
    total_group_members = await db.scalar(select(func.count(GroupMember.id)))
    pending_invitations = await db.scalar(
        select(func.count(Invitation.id)).where(Invitation.status == InvitationStatus.PENDING.value)
    )
    total_shared_resources = await db.scalar(select(func.count(SharedResource.id)))
    total_announcements = await db.scalar(select(func.count(Announcement.id)))
    active_announcements = await db.scalar(
        select(func.count(Announcement.id)).where(Announcement.is_active == True)
    )

    # 导师制统计
    students_with_mentor = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.role == UserRole.STUDENT.value, User.mentor_id.is_not(None))
        )
    )
    students_without_mentor = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.role == UserRole.STUDENT.value, User.mentor_id.is_(None))
        )
    )

    # 文档处理状态
    pending_documents = await db.scalar(
        select(func.count(Document.id)).where(Document.status == DocumentStatus.PENDING.value)
    )
    running_documents = await db.scalar(
        select(func.count(Document.id)).where(Document.status == DocumentStatus.RUNNING.value)
    )
    completed_documents = await db.scalar(
        select(func.count(Document.id)).where(Document.status == DocumentStatus.COMPLETED.value)
    )
    failed_documents = await db.scalar(
        select(func.count(Document.id)).where(Document.status == DocumentStatus.FAILED.value)
    )
    timeout_documents = await db.scalar(
        select(func.count(Document.id)).where(Document.status == DocumentStatus.TIMEOUT.value)
    )
    cancelled_documents = await db.scalar(
        select(func.count(Document.id)).where(Document.status == DocumentStatus.CANCELLED.value)
    )

    # 近 7 天增量
    new_users_last_7_days = await _count_created_since(db, User, User.created_at, window_start)
    new_conversations_last_7_days = await _count_created_since(
        db, Conversation, Conversation.created_at, window_start
    )
    new_knowledge_bases_last_7_days = await _count_created_since(
        db, KnowledgeBase, KnowledgeBase.created_at, window_start
    )
    new_papers_last_7_days = await _count_created_since(db, Paper, Paper.created_at, window_start)
    new_notebooks_last_7_days = await _count_created_since(
        db, Notebook, Notebook.created_at, window_start
    )

    # CodeLab 统计
    notebooks_active_last_window = await db.scalar(
        select(func.count(Notebook.id)).where(Notebook.updated_at >= window_start)
    )
    executed_notebooks = await db.scalar(
        select(func.count(Notebook.id)).where(Notebook.execution_count > 0)
    )
    total_execution_count = await db.scalar(
        select(func.coalesce(func.sum(Notebook.execution_count), 0))
    )
    code_cells = await db.scalar(
        select(func.count(NotebookCell.id)).where(NotebookCell.cell_type == "code")
    )
    executed_code_cells = await db.scalar(
        select(func.count(NotebookCell.id)).where(
            and_(NotebookCell.cell_type == "code", NotebookCell.execution_count.is_not(None), NotebookCell.execution_count > 0)
        )
    )

    # 文献阅读统计
    total_collections = await db.scalar(select(func.count(PaperCollection.id)))
    active_read_sessions_last_window = await db.scalar(
        select(func.count(PaperReadSession.id)).where(PaperReadSession.updated_at >= window_start)
    )
    annotations_last_window = await db.scalar(
        select(func.count(PaperAnnotation.id)).where(PaperAnnotation.created_at >= window_start)
    )
    comments_last_window = await db.scalar(
        select(func.count(PaperComment.id)).where(
            and_(PaperComment.created_at >= window_start, PaperComment.deleted_at.is_(None))
        )
    )
    ratings_last_window = await db.scalar(
        select(func.count(PaperRating.id)).where(PaperRating.created_at >= window_start)
    )
    qa_sessions_last_window = await db.scalar(
        select(func.count(LiteratureQASession.id)).where(LiteratureQASession.created_at >= window_start)
    )
    qa_messages_last_window = await db.scalar(
        select(func.count(LiteratureQAMessage.id)).where(LiteratureQAMessage.created_at >= window_start)
    )
    knowledge_links_total = await db.scalar(select(func.count(PaperKnowledgeLink.id)))

    trends_7d = {
        "users": await _build_daily_count_series(db, User, User.created_at, days=days),
        "conversations": await _build_daily_count_series(db, Conversation, Conversation.created_at, days=days),
        "knowledge_bases": await _build_daily_count_series(db, KnowledgeBase, KnowledgeBase.created_at, days=days),
        "papers": await _build_daily_count_series(db, Paper, Paper.created_at, days=days),
        "notebooks": await _build_daily_count_series(db, Notebook, Notebook.created_at, days=days),
    }
    share_breakdown = await _build_breakdown(db, SharedResource, SharedResource.resource_type, SHARE_TYPE_LABELS)
    invitation_breakdown = await _build_breakdown(db, Invitation, Invitation.status, INVITATION_STATUS_LABELS)
    knowledge_link_breakdown = await _build_breakdown(
        db,
        PaperKnowledgeLink,
        PaperKnowledgeLink.status,
        KNOWLEDGE_LINK_STATUS_LABELS,
    )
    top_mentors = await _build_top_mentors(db)
    recent_activity = await _build_recent_activity(db)
    ai_rag_message_rows = (
        await db.execute(
            select(Message.metadata_, Message.total_tokens)
            .where(and_(Message.role == MessageRole.ASSISTANT, Message.created_at >= window_start))
            .order_by(Message.created_at.desc())
        )
    ).all()
    agent_run_rows = (
        await db.execute(
            select(AgentRun.channel, AgentRun.status, AgentRun.total_tokens)
            .where(AgentRun.started_at >= window_start)
            .order_by(AgentRun.started_at.desc())
        )
    ).all()
    ai_rag = _build_ai_rag_statistics(ai_rag_message_rows, agent_run_rows)
    codelab = _build_codelab_statistics(
        notebooks_active_last_window=notebooks_active_last_window or 0,
        executed_notebooks=executed_notebooks or 0,
        total_execution_count=total_execution_count or 0,
        code_cells=code_cells or 0,
        executed_code_cells=executed_code_cells or 0,
        agent_run_rows=agent_run_rows,
    )

    return SystemStatistics(
        time_window_days=days,
        total_users=total_users or 0,
        admin_count=admin_count or 0,
        mentor_count=mentor_count or 0,
        student_count=student_count or 0,
        active_users=active_users or 0,
        inactive_users=max(int((total_users or 0) - (active_users or 0)), 0),
        total_conversations=total_conversations or 0,
        total_knowledge_bases=total_knowledge_bases or 0,
        total_documents=total_documents or 0,
        total_papers=total_papers or 0,
        total_notebooks=total_notebooks or 0,
        total_groups=total_groups or 0,
        pending_invitations=pending_invitations or 0,
        total_shared_resources=total_shared_resources or 0,
        total_announcements=total_announcements or 0,
        students_with_mentor=students_with_mentor or 0,
        students_without_mentor=students_without_mentor or 0,
        activity={
            "new_users_last_7_days": new_users_last_7_days,
            "new_conversations_last_7_days": new_conversations_last_7_days,
            "new_knowledge_bases_last_7_days": new_knowledge_bases_last_7_days,
            "new_papers_last_7_days": new_papers_last_7_days,
            "new_notebooks_last_7_days": new_notebooks_last_7_days,
        },
        collaboration={
            "total_groups": total_groups or 0,
            "active_groups": active_groups or 0,
            "total_group_members": total_group_members or 0,
            "pending_invitations": pending_invitations or 0,
            "total_shared_resources": total_shared_resources or 0,
            "total_announcements": total_announcements or 0,
            "active_announcements": active_announcements or 0,
        },
        mentorship={
            "students_with_mentor": students_with_mentor or 0,
            "students_without_mentor": students_without_mentor or 0,
        },
        document_pipeline={
            "total_documents": total_documents or 0,
            "completed_documents": completed_documents or 0,
            "running_documents": running_documents or 0,
            "failed_documents": failed_documents or 0,
            "pending_documents": pending_documents or 0,
            "timeout_documents": timeout_documents or 0,
            "cancelled_documents": cancelled_documents or 0,
        },
        trends_7d=trends_7d,
        share_breakdown=share_breakdown,
        invitation_breakdown=invitation_breakdown,
        top_mentors=top_mentors,
        recent_activity=recent_activity,
        ai_rag=ai_rag,
        codelab=codelab,
        literature={
            "total_collections": total_collections or 0,
            "active_read_sessions_last_window": active_read_sessions_last_window or 0,
            "annotations_last_window": annotations_last_window or 0,
            "comments_last_window": comments_last_window or 0,
            "ratings_last_window": ratings_last_window or 0,
            "qa_sessions_last_window": qa_sessions_last_window or 0,
            "qa_messages_last_window": qa_messages_last_window or 0,
            "knowledge_links_total": knowledge_links_total or 0,
            "knowledge_link_breakdown": knowledge_link_breakdown,
        },
    )


@router.get("/statistics/details", response_model=StatisticsDetailResponse)
async def get_statistics_details(
    entity: str = Query(..., pattern="^(groups|shares|invitations|announcements)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    status: str | None = Query(None),
    category: str | None = Query(None),
    search: str | None = Query(None),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """获取系统统计页下钻明细。"""
    offset = (page - 1) * page_size

    if entity == "groups":
        mentor_alias = aliased(User)
        query = (
            select(
                ResearchGroup.id,
                ResearchGroup.name,
                ResearchGroup.description,
                ResearchGroup.is_active,
                ResearchGroup.created_at,
                ResearchGroup.updated_at,
                mentor_alias.full_name,
                mentor_alias.username,
                func.count(GroupMember.id).label("member_count"),
            )
            .select_from(ResearchGroup)
            .join(mentor_alias, mentor_alias.id == ResearchGroup.mentor_id)
            .outerjoin(GroupMember, GroupMember.group_id == ResearchGroup.id)
            .group_by(
                ResearchGroup.id,
                ResearchGroup.name,
                ResearchGroup.description,
                ResearchGroup.is_active,
                ResearchGroup.created_at,
                ResearchGroup.updated_at,
                mentor_alias.full_name,
                mentor_alias.username,
            )
        )
        total_query = select(func.count(ResearchGroup.id)).select_from(ResearchGroup).join(
            mentor_alias, mentor_alias.id == ResearchGroup.mentor_id
        )
        if status in {"active", "inactive"}:
            is_active = status == "active"
            query = query.where(ResearchGroup.is_active == is_active)
            total_query = total_query.where(ResearchGroup.is_active == is_active)
        if search:
            pattern = f"%{search}%"
            filters = or_(
                ResearchGroup.name.ilike(pattern),
                ResearchGroup.description.ilike(pattern),
                mentor_alias.username.ilike(pattern),
                mentor_alias.full_name.ilike(pattern),
            )
            query = query.where(filters)
            total_query = total_query.where(filters)
        rows = (
            await db.execute(
                query.order_by(ResearchGroup.updated_at.desc()).offset(offset).limit(page_size)
            )
        ).all()
        total = int(await db.scalar(total_query) or 0)
        items = [
            {
                "id": f"group-{row[0]}",
                "entity": "groups",
                "title": row[1],
                "subtitle": row[2],
                "status": "active" if row[3] else "inactive",
                "owner_name": row[6] or row[7],
                "owner_role": UserRole.MENTOR.value,
                "member_count": int(row[8] or 0),
                "created_at": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]
        return StatisticsDetailResponse(entity=entity, total=total, page=page, page_size=page_size, items=items)

    if entity == "shares":
        owner_alias = aliased(User)
        query = (
            select(
                SharedResource.id,
                SharedResource.resource_type,
                SharedResource.resource_id,
                SharedResource.shared_with_type,
                SharedResource.shared_with_id,
                SharedResource.permission,
                SharedResource.created_at,
                owner_alias.full_name,
                owner_alias.username,
                owner_alias.role,
            )
            .select_from(SharedResource)
            .join(owner_alias, owner_alias.id == SharedResource.owner_id)
        )
        total_query = select(func.count(SharedResource.id)).select_from(SharedResource).join(
            owner_alias, owner_alias.id == SharedResource.owner_id
        )
        if category:
            query = query.where(SharedResource.resource_type == category)
            total_query = total_query.where(SharedResource.resource_type == category)
        if search:
            pattern = f"%{search}%"
            filters = or_(
                owner_alias.username.ilike(pattern),
                owner_alias.full_name.ilike(pattern),
                SharedResource.resource_id.ilike(pattern),
            )
            query = query.where(filters)
            total_query = total_query.where(filters)
        rows = (
            await db.execute(
                query.order_by(SharedResource.created_at.desc()).offset(offset).limit(page_size)
            )
        ).all()
        total = int(await db.scalar(total_query) or 0)
        items = []
        for row in rows:
            resource_name = await _resolve_share_resource_name(db, str(row[1]), str(row[2]))
            target_name = await _resolve_share_target_name(db, str(row[3]), row[4])
            items.append(
                {
                    "id": f"share-{row[0]}",
                    "entity": "shares",
                    "title": resource_name,
                    "subtitle": f"{SHARE_TYPE_LABELS.get(str(row[1]), str(row[1]))} · {str(row[2])}",
                    "category": str(row[1]),
                    "owner_name": row[7] or row[8],
                    "owner_role": row[9],
                    "target_name": target_name,
                    "permission": str(row[5]),
                    "created_at": row[6],
                }
            )
        return StatisticsDetailResponse(entity=entity, total=total, page=page, page_size=page_size, items=items)

    if entity == "invitations":
        from_alias = aliased(User)
        to_alias = aliased(User)
        query = (
            select(
                Invitation.id,
                Invitation.type,
                Invitation.status,
                Invitation.message,
                Invitation.created_at,
                Invitation.responded_at,
                from_alias.full_name,
                from_alias.username,
                to_alias.full_name,
                to_alias.username,
                ResearchGroup.name,
            )
            .select_from(Invitation)
            .join(from_alias, from_alias.id == Invitation.from_user_id)
            .join(to_alias, to_alias.id == Invitation.to_user_id)
            .outerjoin(ResearchGroup, ResearchGroup.id == Invitation.group_id)
        )
        total_query = (
            select(func.count(Invitation.id))
            .select_from(Invitation)
            .join(from_alias, from_alias.id == Invitation.from_user_id)
            .join(to_alias, to_alias.id == Invitation.to_user_id)
            .outerjoin(ResearchGroup, ResearchGroup.id == Invitation.group_id)
        )
        if status:
            query = query.where(Invitation.status == status)
            total_query = total_query.where(Invitation.status == status)
        if category:
            query = query.where(Invitation.type == category)
            total_query = total_query.where(Invitation.type == category)
        if search:
            pattern = f"%{search}%"
            filters = or_(
                from_alias.username.ilike(pattern),
                from_alias.full_name.ilike(pattern),
                to_alias.username.ilike(pattern),
                to_alias.full_name.ilike(pattern),
                Invitation.message.ilike(pattern),
                ResearchGroup.name.ilike(pattern),
            )
            query = query.where(filters)
            total_query = total_query.where(filters)
        rows = (
            await db.execute(
                query.order_by(Invitation.created_at.desc()).offset(offset).limit(page_size)
            )
        ).all()
        total = int(await db.scalar(total_query) or 0)
        items = [
            {
                "id": f"invitation-{row[0]}",
                "entity": "invitations",
                "title": f"{row[6] or row[7]} -> {row[8] or row[9]}",
                "subtitle": row[10] or row[3],
                "status": str(row[2]),
                "category": str(row[1]),
                "owner_name": row[6] or row[7],
                "target_name": row[8] or row[9],
                "created_at": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]
        return StatisticsDetailResponse(entity=entity, total=total, page=page, page_size=page_size, items=items)

    mentor_alias = aliased(User)
    query = (
        select(
            Announcement.id,
            Announcement.title,
            Announcement.content,
            Announcement.is_active,
            Announcement.is_pinned,
            Announcement.created_at,
            Announcement.updated_at,
            mentor_alias.full_name,
            mentor_alias.username,
            ResearchGroup.name,
        )
        .select_from(Announcement)
        .join(mentor_alias, mentor_alias.id == Announcement.mentor_id)
        .outerjoin(ResearchGroup, ResearchGroup.id == Announcement.group_id)
    )
    total_query = (
        select(func.count(Announcement.id))
        .select_from(Announcement)
        .join(mentor_alias, mentor_alias.id == Announcement.mentor_id)
        .outerjoin(ResearchGroup, ResearchGroup.id == Announcement.group_id)
    )
    if status in {"active", "inactive"}:
        is_active = status == "active"
        query = query.where(Announcement.is_active == is_active)
        total_query = total_query.where(Announcement.is_active == is_active)
    if category in {"pinned", "normal"}:
        is_pinned = category == "pinned"
        query = query.where(Announcement.is_pinned == is_pinned)
        total_query = total_query.where(Announcement.is_pinned == is_pinned)
    if search:
        pattern = f"%{search}%"
        filters = or_(
            Announcement.title.ilike(pattern),
            Announcement.content.ilike(pattern),
            mentor_alias.username.ilike(pattern),
            mentor_alias.full_name.ilike(pattern),
            ResearchGroup.name.ilike(pattern),
        )
        query = query.where(filters)
        total_query = total_query.where(filters)
    rows = (
        await db.execute(
            query.order_by(Announcement.created_at.desc()).offset(offset).limit(page_size)
        )
    ).all()
    total = int(await db.scalar(total_query) or 0)
    items = [
        {
            "id": f"announcement-{row[0]}",
            "entity": "announcements",
            "title": row[1],
            "subtitle": row[9] or (str(row[2])[:120] if row[2] else None),
            "status": "active" if row[3] else "inactive",
            "category": "pinned" if row[4] else "normal",
            "owner_name": row[7] or row[8],
            "owner_role": UserRole.MENTOR.value,
            "created_at": row[5],
            "updated_at": row[6],
        }
        for row in rows
    ]
    return StatisticsDetailResponse(entity=entity, total=total, page=page, page_size=page_size, items=items)


@router.get("/statistics/export")
async def export_statistics(
    scope: str = Query("summary", pattern="^(summary|details|audit)$"),
    days: int = Query(7, ge=7, le=90),
    entity: str | None = Query(None, pattern="^(groups|shares|invitations|announcements)$"),
    action: str | None = Query(None),
    status: str | None = Query(None),
    category: str | None = Query(None),
    search: str | None = Query(None),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """导出系统统计 CSV。"""
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    if scope == "summary":
        stats = await get_statistics(days=days, current_user=current_user, db=db)
        await _record_admin_audit(
            db,
            admin_user=current_user,
            action="export_statistics_summary",
            target_type="statistics",
            target_id="summary",
            summary=f"导出系统统计总览（{days} 天）",
            details={"days": days},
        )
        await db.commit()
        rows = [[metric, value] for metric, value in _build_statistics_summary_rows(stats)]
        return _csv_response(
            f"system-statistics-summary-{timestamp}.csv",
            ["metric", "value"],
            rows,
        )

    if scope == "details":
        if not entity:
            raise HTTPException(status_code=400, detail="导出明细时必须指定 entity")
        detail_response = await get_statistics_details(
            entity=entity,
            page=1,
            page_size=1000,
            status=status,
            category=category,
            search=search,
            current_user=current_user,
            db=db,
        )
        await _record_admin_audit(
            db,
            admin_user=current_user,
            action="export_statistics_details",
            target_type="statistics_detail",
            target_id=entity,
            summary=f"导出统计明细 {entity}",
            details={
                "entity": entity,
                "status": status,
                "category": category,
                "search": search,
            },
        )
        await db.commit()
        rows = [
            [
                item.id,
                item.entity,
                item.title,
                item.subtitle or "",
                item.status or "",
                item.category or "",
                item.owner_name or "",
                item.target_name or "",
                item.permission or "",
                item.member_count or 0,
                item.created_at.isoformat() if item.created_at else "",
                item.updated_at.isoformat() if item.updated_at else "",
            ]
            for item in detail_response.items
        ]
        return _csv_response(
            f"system-statistics-details-{entity}-{timestamp}.csv",
            [
                "id",
                "entity",
                "title",
                "subtitle",
                "status",
                "category",
                "owner_name",
                "target_name",
                "permission",
                "member_count",
                "created_at",
                "updated_at",
            ],
            rows,
        )

    audit_response = await get_audit_logs(
        page=1,
        page_size=1000,
        action=action,
        search=search,
        current_user=current_user,
        db=db,
    )
    await _record_admin_audit(
        db,
        admin_user=current_user,
        action="export_statistics_audit",
        target_type="audit",
        target_id=action or "all",
        summary="导出管理员审计日志",
        details={"action": action, "search": search},
    )
    await db.commit()
    rows = [
        [
            item.id,
            item.action,
            item.target_type or "",
            item.target_id or "",
            item.admin_name,
            item.summary,
            item.created_at.isoformat(),
        ]
        for item in audit_response.items
    ]
    return _csv_response(
        f"system-statistics-audit-{timestamp}.csv",
        ["id", "action", "target_type", "target_id", "admin_name", "summary", "created_at"],
        rows,
    )


@router.get("/mentors")
async def list_mentors(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取所有导师列表"""
    result = await db.execute(
        select(User)
        .where(User.role == UserRole.MENTOR.value)
        .order_by(User.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    mentors = result.scalars().all()
    
    # 统计每个导师的学生数量
    mentor_list = []
    for mentor in mentors:
        student_count = await db.scalar(
            select(func.count(User.id)).where(User.mentor_id == mentor.id)
        )
        mentor_data = UserListResponse.model_validate(mentor).model_dump()
        mentor_data["student_count"] = student_count or 0
        mentor_list.append(mentor_data)
    
    return mentor_list
