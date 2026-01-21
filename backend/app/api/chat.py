"""
聊天路由
"""
import json
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.conversation import Conversation, Message, MessageRole, MessageType
from app.models.knowledge import KnowledgeBase
from app.schemas.chat import (
    ConversationCreate, ConversationResponse, ConversationListResponse,
    MessageResponse, ChatRequest, SaveStoppedMessageRequest
)
from app.services.llm_service import LLMService
from app.services.agent_tools import get_tool_registry

router = APIRouter()


def message_to_response(msg: Message) -> MessageResponse:
    """将 Message 模型转换为 MessageResponse，处理 Enum 类型"""
    return MessageResponse(
        id=msg.id,
        conversation_id=msg.conversation_id,
        role=msg.role.value if hasattr(msg.role, 'value') else str(msg.role),
        content=msg.content,
        message_type=msg.message_type.value if hasattr(msg.message_type, 'value') else str(msg.message_type),
        thought=msg.thought,
        react_steps=msg.react_steps,  # 添加 ReAct 推理步骤
        action=msg.action,
        action_input=msg.action_input,
        observation=msg.observation,
        metadata=msg.metadata_,
        prompt_tokens=msg.prompt_tokens or 0,
        completion_tokens=msg.completion_tokens or 0,
        total_tokens=msg.total_tokens or 0,
        created_at=msg.created_at,
    )


@router.get("/conversations", response_model=List[ConversationListResponse])
async def list_conversations(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    archived: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取对话列表"""
    # 子查询：获取每个对话的最后一条消息
    last_message_subq = (
        select(
            Message.conversation_id,
            Message.content.label("last_message"),
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=desc(Message.created_at)
            ).label("rn")
        )
        .where(Message.role == MessageRole.ASSISTANT)
        .subquery()
    )
    
    # 子查询：消息计数
    message_count_subq = (
        select(
            Message.conversation_id,
            func.count(Message.id).label("message_count")
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    
    # 主查询
    query = (
        select(
            Conversation,
            last_message_subq.c.last_message,
            func.coalesce(message_count_subq.c.message_count, 0).label("message_count")
        )
        .outerjoin(
            last_message_subq,
            (Conversation.id == last_message_subq.c.conversation_id) & 
            (last_message_subq.c.rn == 1)
        )
        .outerjoin(
            message_count_subq,
            Conversation.id == message_count_subq.c.conversation_id
        )
        .where(
            Conversation.user_id == current_user.id,
            Conversation.is_archived == (1 if archived else 0)
        )
        .order_by(desc(Conversation.updated_at))
        .offset(skip)
        .limit(limit)
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    conversations = []
    for row in rows:
        conv = row[0]
        conv_dict = {
            "id": conv.id,
            "title": conv.title,
            "llm_provider": conv.llm_provider,
            "is_archived": conv.is_archived,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "last_message": row[1][:100] if row[1] else None,
            "message_count": row[2] or 0
        }
        conversations.append(ConversationListResponse(**conv_dict))
    
    return conversations


@router.get("/messages/search")
async def search_messages(
    q: str = Query(..., min_length=1, max_length=200, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """搜索用户的历史消息"""
    search_term = f"%{q}%"
    
    # 搜索消息内容，同时获取对话标题
    query = (
        select(
            Message.id,
            Message.conversation_id,
            Message.role,
            Message.content,
            Message.created_at,
            Conversation.title.label("conversation_title")
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.user_id == current_user.id,
            Conversation.is_archived == 0,
            Message.content.ilike(search_term)
        )
        .order_by(desc(Message.created_at))
        .limit(limit)
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    results = []
    for row in rows:
        # 找到匹配文本的位置，提取上下文片段
        content = row.content
        q_lower = q.lower()
        content_lower = content.lower()
        match_pos = content_lower.find(q_lower)
        
        # 提取匹配位置周围的文本片段（前后各50个字符）
        start = max(0, match_pos - 50)
        end = min(len(content), match_pos + len(q) + 50)
        snippet = content[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."
        
        results.append({
            "message_id": row.id,
            "conversation_id": row.conversation_id,
            "conversation_title": row.conversation_title or "新对话",
            "role": row.role.value if hasattr(row.role, 'value') else str(row.role),
            "content_snippet": snippet,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    
    return {
        "query": q,
        "total": len(results),
        "results": results
    }


@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    data: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建新对话"""
    conversation = Conversation(
        user_id=current_user.id,
        title=data.title,
        llm_provider=data.llm_provider or current_user.preferred_llm_provider,
    )
    
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    
    logger.info(f"创建对话: {conversation.id} by {current_user.username}")
    
    # 手动构建响应，避免触发懒加载
    return ConversationResponse(
        id=conversation.id,
        user_id=conversation.user_id,
        title=conversation.title,
        llm_provider=conversation.llm_provider,
        llm_model=conversation.llm_model,
        is_archived=conversation.is_archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[]  # 新建对话没有消息
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取对话详情"""
    result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    # 手动构建响应
    return ConversationResponse(
        id=conversation.id,
        user_id=conversation.user_id,
        title=conversation.title,
        llm_provider=conversation.llm_provider,
        llm_model=conversation.llm_model,
        is_archived=conversation.is_archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[message_to_response(msg) for msg in conversation.messages]
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除对话"""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    await db.delete(conversation)
    await db.commit()
    
    logger.info(f"删除对话: {conversation_id}")
    
    return {"message": "删除成功"}


@router.put("/conversations/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """归档对话"""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    conversation.is_archived = 1 if conversation.is_archived == 0 else 0
    await db.commit()
    
    return {"message": "操作成功", "is_archived": conversation.is_archived}


@router.post("/send")
async def send_message(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """发送消息（支持流式响应和工具调用）"""
    # 获取或创建对话
    if request.conversation_id:
        result = await db.execute(
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(
                Conversation.id == request.conversation_id,
                Conversation.user_id == current_user.id
            )
        )
        conversation = result.scalar_one_or_none()
        
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="对话不存在"
            )
        # 获取已有消息
        existing_messages = list(conversation.messages)
    else:
        # 创建新对话
        conversation = Conversation(
            user_id=current_user.id,
            title=request.message[:50] + "..." if len(request.message) > 50 else request.message,
            llm_provider=request.llm_provider or current_user.preferred_llm_provider,
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        existing_messages = []
    
    # 保存用户消息
    user_message = Message(
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content=request.message,
        message_type=MessageType.TEXT,
    )
    db.add(user_message)
    await db.commit()
    await db.refresh(user_message)
    
    logger.info(f"用户消息: conv={conversation.id}, msg={user_message.id}")
    
    # 获取 LLM 服务
    llm_provider = request.llm_provider or conversation.llm_provider
    llm_service = LLMService(llm_provider)
    
    # 构建消息历史
    messages = []
    for msg in existing_messages:
        messages.append({
            "role": msg.role.value.lower(),
            "content": msg.content
        })
    # 添加当前消息
    messages.append({
        "role": "user",
        "content": request.message
    })
    
    # 保存对话ID用于流式响应
    conversation_id = conversation.id
    
    # 是否使用工具 - 默认启用，除非明确禁用
    use_tools = True
    if hasattr(request, 'use_tools') and request.use_tools is not None:
        use_tools = request.use_tools
    
    logger.info(f"对话 {conversation_id}: use_tools={use_tools}")
    
    if request.stream:
        # 流式响应
        async def generate():
            full_content = ""
            thought = ""
            
            try:
                # 发送开始事件
                yield f"data: {json.dumps({'event': 'start', 'data': {'conversation_id': conversation_id, 'message_id': user_message.id}})}\n\n"
                
                if use_tools:
                    # 使用 ReAct Agent（带工具）
                    from app.core.database import async_session_factory
                    from app.services.react_agent import create_react_agent
                    
                    # 收集ReAct步骤
                    react_steps = []
                    current_iteration = 0
                    
                    async with async_session_factory() as tool_db:
                        tool_registry = get_tool_registry(tool_db, current_user.id)
                        agent = create_react_agent(llm_service, tool_registry, max_iterations=5)
                        
                        async for event in agent.run(messages, stream=True):
                            event_type = event["type"]
                            event_data = event["data"]
                            
                            if event_type == "start":
                                yield f"data: {json.dumps({'event': 'model_info', 'data': event_data})}\n\n"
                            elif event_type == "thinking_start":
                                current_iteration += 1
                                yield f"data: {json.dumps({'event': 'thinking_start', 'data': {'iteration': current_iteration}})}\n\n"
                            elif event_type == "thinking":
                                yield f"data: {json.dumps({'event': 'thinking', 'data': event_data})}\n\n"
                            elif event_type == "thought":
                                thought = event_data
                                react_steps.append({
                                    "type": "thought",
                                    "iteration": current_iteration,
                                    "content": event_data
                                })
                                yield f"data: {json.dumps({'event': 'thought', 'data': event_data})}\n\n"
                            elif event_type == "action":
                                react_steps.append({
                                    "type": "action",
                                    "iteration": current_iteration,
                                    "tool": event_data.get("tool"),
                                    "input": event_data.get("input")
                                })
                                yield f"data: {json.dumps({'event': 'action', 'data': event_data})}\n\n"
                            elif event_type == "observation":
                                react_steps.append({
                                    "type": "observation",
                                    "iteration": current_iteration,
                                    "tool": event_data.get("tool"),
                                    "success": event_data.get("success"),
                                    "output": event_data.get("output", "")[:500]  # 限制长度
                                })
                                yield f"data: {json.dumps({'event': 'observation', 'data': event_data})}\n\n"
                            elif event_type == "content":
                                full_content += event_data
                                yield f"data: {json.dumps({'event': 'content', 'data': event_data})}\n\n"
                            elif event_type == "answer":
                                full_content = event_data
                                yield f"data: {json.dumps({'event': 'content', 'data': event_data})}\n\n"
                            elif event_type == "error":
                                logger.error(f"[Chat] ReAct Agent 错误: {event_data}")
                                yield f"data: {json.dumps({'event': 'error', 'data': event_data})}\n\n"
                            elif event_type == "done":
                                if isinstance(event_data, dict):
                                    if event_data.get("thought"):
                                        thought = event_data["thought"]
                                    if event_data.get("answer") and not full_content:
                                        full_content = event_data["answer"]
                                
                                logger.info(f"[Chat] 对话完成: iterations={current_iteration}, steps={len(react_steps)}, content_len={len(full_content)}")
                                
                                # 保存助手消息（包含完整的ReAct步骤）
                                async with async_session_factory() as save_db:
                                    assistant_message = Message(
                                        conversation_id=conversation_id,
                                        role=MessageRole.ASSISTANT,
                                        content=full_content,
                                        message_type=MessageType.TEXT,
                                        thought=thought if thought else None,
                                        react_steps=react_steps if react_steps else None,
                                    )
                                    save_db.add(assistant_message)
                                    await save_db.commit()
                                    await save_db.refresh(assistant_message)
                                    
                                    yield f"data: {json.dumps({'event': 'done', 'data': {'message_id': assistant_message.id, 'thought': thought, 'answer': full_content, 'react_steps': react_steps}})}\n\n"
                else:
                    # 使用普通 ReAct 聊天（无工具）
                    async for chunk in llm_service.react_chat_stream(messages):
                        chunk_type = chunk["type"]
                        chunk_data = chunk["data"]
                        
                        if chunk_type == "start":
                            yield f"data: {json.dumps({'event': 'model_info', 'data': chunk_data})}\n\n"
                        elif chunk_type == "thinking_start":
                            yield f"data: {json.dumps({'event': 'thinking_start', 'data': ''})}\n\n"
                        elif chunk_type == "thinking":
                            yield f"data: {json.dumps({'event': 'thinking', 'data': chunk_data})}\n\n"
                        elif chunk_type == "thought":
                            thought = chunk_data
                            yield f"data: {json.dumps({'event': 'thought', 'data': chunk_data})}\n\n"
                        elif chunk_type == "content":
                            full_content += chunk_data
                            yield f"data: {json.dumps({'event': 'content', 'data': chunk_data})}\n\n"
                        elif chunk_type == "done":
                            if isinstance(chunk_data, dict):
                                if chunk_data.get("thought"):
                                    thought = chunk_data["thought"]
                                if chunk_data.get("answer") and not full_content:
                                    full_content = chunk_data["answer"]
                            
                            # 保存助手消息
                            from app.core.database import async_session_factory
                            async with async_session_factory() as save_db:
                                assistant_message = Message(
                                    conversation_id=conversation_id,
                                    role=MessageRole.ASSISTANT,
                                    content=full_content,
                                    message_type=MessageType.TEXT,
                                    thought=thought if thought else None,
                                )
                                save_db.add(assistant_message)
                                await save_db.commit()
                                await save_db.refresh(assistant_message)
                                
                                yield f"data: {json.dumps({'event': 'done', 'data': {'message_id': assistant_message.id, 'thought': thought, 'answer': full_content}})}\n\n"
                
            except Exception as e:
                logger.error(f"流式响应错误: {e}")
                yield f"data: {json.dumps({'event': 'error', 'data': str(e)})}\n\n"
        
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    else:
        # 非流式响应
        try:
            response = await llm_service.chat(messages, llm_service.REACT_SYSTEM_PROMPT)
            
            assistant_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=response["content"],
                message_type=MessageType.TEXT,
                prompt_tokens=response["usage"]["prompt_tokens"],
                completion_tokens=response["usage"]["completion_tokens"],
                total_tokens=response["usage"]["total_tokens"],
            )
            db.add(assistant_message)
            await db.commit()
            await db.refresh(assistant_message)
            
            return {
                "conversation_id": conversation.id,
                "message": message_to_response(assistant_message),
                "usage": response["usage"]
            }
            
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"AI 响应失败: {str(e)}"
            )


@router.get("/conversations/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_messages(
    conversation_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取对话消息列表"""
    # 验证对话所有权
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    # 获取消息
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
        .offset(skip)
        .limit(limit)
    )
    messages = result.scalars().all()
    
    return [message_to_response(msg) for msg in messages]


@router.post("/messages/stopped", response_model=MessageResponse)
async def save_stopped_message(
    request: SaveStoppedMessageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """保存被停止的消息"""
    # 验证对话所有权
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == request.conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话不存在"
        )
    
    # 创建停止的消息
    message = Message(
        conversation_id=request.conversation_id,
        role=MessageRole.ASSISTANT,
        content=request.content,
        message_type=MessageType.TEXT,
        thought=request.thought,
        react_steps=request.react_steps,
    )
    
    db.add(message)
    await db.commit()
    await db.refresh(message)
    
    logger.info(f"保存停止消息: conv={request.conversation_id}, msg={message.id}")
    
    return message_to_response(message)
