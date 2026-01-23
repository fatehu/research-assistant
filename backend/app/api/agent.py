"""
Notebook Agent API - 为每个 Notebook 提供 AI 助手功能
支持上下文感知的对话、代码生成、数据分析辅助等

核心功能：
1. 获取 Notebook 上下文（变量、代码、输出）
2. 与 AI 进行对话，AI 能理解当前 Notebook 状态
3. 生成代码建议并可直接插入 Notebook
4. 辅助数据分析、爬取数据等工作
"""
import asyncio
import json
import uuid
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, AsyncGenerator
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger

from app.core.security import get_current_user
from app.models.user import User
from app.api.codelab import kernel_manager, get_notebook, _notebooks
from app.services.llm_service import LLMService

router = APIRouter()

# ========== Pydantic Models ==========

class AgentMessage(BaseModel):
    """Agent 对话消息"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str  # 'user', 'assistant', 'system'
    content: str
    code_blocks: List[Dict[str, str]] = []  # 提取的代码块
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = {}

class AgentChatRequest(BaseModel):
    """Agent 对话请求"""
    message: str
    include_context: bool = True  # 是否包含 Notebook 上下文
    include_variables: bool = True  # 是否包含变量信息
    stream: bool = True  # 是否流式返回

class AgentChatResponse(BaseModel):
    """Agent 对话响应"""
    message: AgentMessage
    suggested_code: Optional[str] = None
    suggested_action: Optional[str] = None  # 'insert_code', 'run_code', 'explain', etc.

class AgentContextResponse(BaseModel):
    """Agent 上下文响应"""
    notebook_id: str
    notebook_title: str
    cell_count: int
    execution_count: int
    variables: Dict[str, str]
    recent_outputs: List[Dict[str, Any]]
    code_summary: str

class AgentConversation(BaseModel):
    """Agent 对话历史"""
    notebook_id: str
    messages: List[AgentMessage] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# ========== 内存存储 (生产环境应使用数据库) ==========

_agent_conversations: Dict[str, AgentConversation] = {}

def get_or_create_conversation(notebook_id: str, user_id: int) -> AgentConversation:
    """获取或创建对话"""
    key = f"{user_id}_{notebook_id}"
    if key not in _agent_conversations:
        _agent_conversations[key] = AgentConversation(notebook_id=notebook_id)
    return _agent_conversations[key]

def clear_conversation(notebook_id: str, user_id: int):
    """清空对话"""
    key = f"{user_id}_{notebook_id}"
    if key in _agent_conversations:
        _agent_conversations[key].messages = []
        _agent_conversations[key].updated_at = datetime.utcnow()

# ========== 辅助函数 ==========

def extract_code_blocks(text: str) -> List[Dict[str, str]]:
    """从文本中提取代码块"""
    import re
    code_blocks = []
    # 匹配 ```python ... ``` 或 ```py ... ``` 或 ``` ... ```
    pattern = r'```(?:python|py)?\s*\n(.*?)\n```'
    matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
    for i, code in enumerate(matches):
        code_blocks.append({
            'id': str(uuid.uuid4()),
            'language': 'python',
            'code': code.strip()
        })
    return code_blocks

def build_context_prompt(notebook: Dict, kernel, include_variables: bool = True) -> str:
    """构建包含 Notebook 上下文的 prompt"""
    context_parts = []
    
    # Notebook 基本信息
    context_parts.append(f"## 当前 Notebook: {notebook['title']}")
    context_parts.append(f"- 单元格数量: {len(notebook['cells'])}")
    context_parts.append(f"- 执行次数: {notebook.get('execution_count', 0)}")
    
    # 变量信息
    if include_variables and kernel:
        variables = kernel.get_variables()
        if variables:
            context_parts.append("\n## 当前变量:")
            for name, type_str in variables.items():
                # 尝试获取变量的简短描述
                try:
                    var = kernel.namespace.get(name)
                    if hasattr(var, 'shape'):  # numpy array 或 pandas DataFrame
                        context_parts.append(f"- `{name}`: {type_str}, shape={var.shape}")
                    elif hasattr(var, '__len__') and not isinstance(var, str):
                        context_parts.append(f"- `{name}`: {type_str}, len={len(var)}")
                    else:
                        context_parts.append(f"- `{name}`: {type_str}")
                except:
                    context_parts.append(f"- `{name}`: {type_str}")
    
    # 最近的代码单元格
    code_cells = [c for c in notebook['cells'] if c['cell_type'] == 'code' and c['source'].strip()]
    if code_cells:
        context_parts.append("\n## 最近的代码:")
        for i, cell in enumerate(code_cells[-3:]):  # 最近 3 个代码单元格
            context_parts.append(f"\n### Cell [{cell.get('execution_count', '?')}]:")
            context_parts.append(f"```python\n{cell['source']}\n```")
            
            # 如果有输出
            if cell.get('outputs'):
                for output in cell['outputs'][:2]:  # 最多显示 2 个输出
                    if output.get('output_type') == 'stream':
                        content = output.get('content', '')[:500]  # 限制长度
                        context_parts.append(f"输出: {content}")
                    elif output.get('output_type') == 'error':
                        error = output.get('content', {})
                        context_parts.append(f"错误: {error.get('ename', '')}: {error.get('evalue', '')}")
    
    return '\n'.join(context_parts)

def build_system_prompt() -> str:
    """构建系统 prompt"""
    return """你是一个专业的数据科学和编程助手，专门帮助用户在 Jupyter Notebook 中进行数据分析和编程工作。

你的能力包括：
1. **代码生成**: 根据用户需求生成 Python 代码
2. **错误解释**: 分析代码错误并提供修复建议
3. **数据分析**: 帮助用户理解数据、选择合适的分析方法
4. **可视化建议**: 推荐合适的图表类型和可视化代码
5. **代码优化**: 提供代码改进建议
6. **数据爬取**: 帮助编写网页爬虫代码

输出格式要求：
- 代码应该用 ```python ... ``` 包裹
- 对于可以直接运行的代码，请标注 [可运行]
- 对于需要修改的代码模板，请标注 [需修改]
- 解释应该简洁明了

请基于用户当前的 Notebook 上下文来提供帮助。"""

# ========== API 端点 ==========

@router.get("/notebooks/{notebook_id}/agent/context", response_model=AgentContextResponse)
async def get_agent_context(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """获取 Notebook 上下文信息，供 Agent 使用"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 获取内核信息
    kernel = kernel_manager.get_kernel(notebook_id)
    variables = kernel.get_variables() if kernel else {}
    
    # 获取最近的输出
    recent_outputs = []
    code_cells = [c for c in notebook['cells'] if c['cell_type'] == 'code']
    for cell in code_cells[-5:]:
        if cell.get('outputs'):
            recent_outputs.append({
                'cell_id': cell['id'],
                'execution_count': cell.get('execution_count'),
                'outputs': cell['outputs'][:3]  # 限制输出数量
            })
    
    # 生成代码摘要
    code_parts = [c['source'] for c in code_cells if c['source'].strip()]
    code_summary = f"共 {len(code_parts)} 个代码单元格"
    if code_parts:
        # 提取 import 语句
        imports = []
        for code in code_parts:
            for line in code.split('\n'):
                if line.strip().startswith('import ') or line.strip().startswith('from '):
                    imports.append(line.strip())
        if imports:
            code_summary += f"，使用的库: {', '.join(set(imports[:10]))}"
    
    return AgentContextResponse(
        notebook_id=notebook_id,
        notebook_title=notebook['title'],
        cell_count=len(notebook['cells']),
        execution_count=notebook.get('execution_count', 0),
        variables=variables,
        recent_outputs=recent_outputs,
        code_summary=code_summary
    )


@router.get("/notebooks/{notebook_id}/agent/history")
async def get_agent_history(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """获取 Agent 对话历史"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    conversation = get_or_create_conversation(notebook_id, current_user.id)
    
    return {
        'notebook_id': notebook_id,
        'messages': [msg.dict() for msg in conversation.messages],
        'created_at': conversation.created_at,
        'updated_at': conversation.updated_at
    }


@router.delete("/notebooks/{notebook_id}/agent/history")
async def clear_agent_history(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """清空 Agent 对话历史"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    clear_conversation(notebook_id, current_user.id)
    
    return {'message': '对话历史已清空'}


@router.post("/notebooks/{notebook_id}/agent/chat")
async def agent_chat(
    notebook_id: str,
    request: AgentChatRequest,
    current_user: User = Depends(get_current_user)
):
    """与 Agent 进行对话"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 获取或创建对话
    conversation = get_or_create_conversation(notebook_id, current_user.id)
    
    # 添加用户消息
    user_message = AgentMessage(
        role='user',
        content=request.message
    )
    conversation.messages.append(user_message)
    conversation.updated_at = datetime.utcnow()
    
    # 构建上下文
    context = ""
    if request.include_context:
        kernel = kernel_manager.get_kernel(notebook_id)
        context = build_context_prompt(notebook, kernel, request.include_variables)
    
    # 构建消息历史 (限制长度)
    history_messages = []
    for msg in conversation.messages[-10:]:  # 最近 10 条消息
        history_messages.append({
            'role': msg.role,
            'content': msg.content
        })
    
    # 如果是流式返回
    if request.stream:
        return StreamingResponse(
            stream_agent_response(
                conversation, context, history_messages, notebook_id, current_user.id
            ),
            media_type='text/event-stream'
        )
    
    # 非流式返回
    try:
        llm_service = LLMService()
        
        # 构建完整的消息列表
        messages = [{'role': 'system', 'content': build_system_prompt()}]
        if context:
            messages.append({'role': 'system', 'content': f"当前 Notebook 上下文:\n{context}"})
        messages.extend(history_messages)
        
        response = await llm_service.chat(messages)
        
        # 提取代码块
        code_blocks = extract_code_blocks(response)
        
        # 添加助手消息
        assistant_message = AgentMessage(
            role='assistant',
            content=response,
            code_blocks=code_blocks
        )
        conversation.messages.append(assistant_message)
        conversation.updated_at = datetime.utcnow()
        
        # 确定建议的操作
        suggested_action = None
        suggested_code = None
        if code_blocks:
            suggested_code = code_blocks[0]['code']
            if '[可运行]' in response:
                suggested_action = 'run_code'
            else:
                suggested_action = 'insert_code'
        
        return AgentChatResponse(
            message=assistant_message,
            suggested_code=suggested_code,
            suggested_action=suggested_action
        )
        
    except Exception as e:
        logger.error(f"Agent 对话失败: {e}")
        raise HTTPException(status_code=500, detail=f"对话失败: {str(e)}")


async def stream_agent_response(
    conversation: AgentConversation,
    context: str,
    history_messages: List[Dict],
    notebook_id: str,
    user_id: int
) -> AsyncGenerator[str, None]:
    """流式返回 Agent 响应"""
    try:
        llm_service = LLMService()
        
        # 构建完整的消息列表
        messages = [{'role': 'system', 'content': build_system_prompt()}]
        if context:
            messages.append({'role': 'system', 'content': f"当前 Notebook 上下文:\n{context}"})
        messages.extend(history_messages)
        
        full_response = ""
        
        async for chunk in llm_service.chat_stream(messages):
            full_response += chunk
            # SSE 格式
            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
        
        # 提取代码块
        code_blocks = extract_code_blocks(full_response)
        
        # 添加助手消息
        assistant_message = AgentMessage(
            role='assistant',
            content=full_response,
            code_blocks=code_blocks
        )
        conversation.messages.append(assistant_message)
        conversation.updated_at = datetime.utcnow()
        
        # 发送完成事件
        suggested_action = None
        suggested_code = None
        if code_blocks:
            suggested_code = code_blocks[0]['code']
            if '[可运行]' in full_response:
                suggested_action = 'run_code'
            else:
                suggested_action = 'insert_code'
        
        yield f"data: {json.dumps({'type': 'done', 'code_blocks': code_blocks, 'suggested_action': suggested_action, 'suggested_code': suggested_code})}\n\n"
        
    except Exception as e:
        logger.error(f"Agent 流式响应失败: {e}")
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"


@router.post("/notebooks/{notebook_id}/agent/suggest-code")
async def suggest_code(
    notebook_id: str,
    description: str,
    current_user: User = Depends(get_current_user)
):
    """根据描述生成代码建议"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 获取上下文
    kernel = kernel_manager.get_kernel(notebook_id)
    context = build_context_prompt(notebook, kernel)
    
    try:
        llm_service = LLMService()
        
        messages = [
            {'role': 'system', 'content': build_system_prompt()},
            {'role': 'system', 'content': f"当前 Notebook 上下文:\n{context}"},
            {'role': 'user', 'content': f"请生成以下功能的 Python 代码:\n{description}\n\n只返回代码，不需要额外解释。代码应该可以直接在当前 Notebook 中运行。"}
        ]
        
        response = await llm_service.chat(messages)
        code_blocks = extract_code_blocks(response)
        
        return {
            'description': description,
            'code': code_blocks[0]['code'] if code_blocks else response.strip(),
            'full_response': response
        }
        
    except Exception as e:
        logger.error(f"代码生成失败: {e}")
        raise HTTPException(status_code=500, detail=f"代码生成失败: {str(e)}")


@router.post("/notebooks/{notebook_id}/agent/explain-error")
async def explain_error(
    notebook_id: str,
    error_message: str,
    code: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """解释代码错误"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    try:
        llm_service = LLMService()
        
        prompt = f"请解释以下 Python 错误并提供修复建议:\n\n错误信息:\n{error_message}"
        if code:
            prompt += f"\n\n相关代码:\n```python\n{code}\n```"
        
        messages = [
            {'role': 'system', 'content': '你是一个 Python 专家，擅长分析和修复代码错误。请用简洁的语言解释错误原因，并提供具体的修复代码。'},
            {'role': 'user', 'content': prompt}
        ]
        
        response = await llm_service.chat(messages)
        code_blocks = extract_code_blocks(response)
        
        return {
            'explanation': response,
            'fix_code': code_blocks[0]['code'] if code_blocks else None
        }
        
    except Exception as e:
        logger.error(f"错误解释失败: {e}")
        raise HTTPException(status_code=500, detail=f"错误解释失败: {str(e)}")


@router.post("/notebooks/{notebook_id}/agent/analyze-data")
async def analyze_data(
    notebook_id: str,
    variable_name: str,
    analysis_type: str = "overview",  # overview, statistics, distribution, correlation
    current_user: User = Depends(get_current_user)
):
    """分析 Notebook 中的数据变量"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    kernel = kernel_manager.get_kernel(notebook_id)
    if not kernel:
        raise HTTPException(status_code=400, detail="内核未启动")
    
    # 检查变量是否存在
    if variable_name not in kernel.namespace:
        raise HTTPException(status_code=400, detail=f"变量 '{variable_name}' 不存在")
    
    var = kernel.namespace[variable_name]
    
    # 根据分析类型生成代码
    analysis_code_templates = {
        'overview': f"""
# 数据概览
print(f"类型: {{type({variable_name})}}")
if hasattr({variable_name}, 'shape'):
    print(f"形状: {{{variable_name}.shape}}")
if hasattr({variable_name}, 'dtypes'):
    print(f"数据类型:\\n{{{variable_name}.dtypes}}")
if hasattr({variable_name}, 'head'):
    print(f"\\n前5行:\\n{{{variable_name}.head()}}")
if hasattr({variable_name}, 'info'):
    print(f"\\n数据信息:")
    {variable_name}.info()
""",
        'statistics': f"""
# 统计信息
if hasattr({variable_name}, 'describe'):
    print({variable_name}.describe())
else:
    import numpy as np
    print(f"均值: {{np.mean({variable_name})}}")
    print(f"标准差: {{np.std({variable_name})}}")
    print(f"最小值: {{np.min({variable_name})}}")
    print(f"最大值: {{np.max({variable_name})}}")
""",
        'distribution': f"""
# 分布可视化
import matplotlib.pyplot as plt
import numpy as np

if hasattr({variable_name}, 'hist'):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    {variable_name}.hist(ax=axes[0])
    axes[0].set_title('直方图')
    if hasattr({variable_name}, 'plot'):
        {variable_name}.plot(kind='box', ax=axes[1])
        axes[1].set_title('箱线图')
    plt.tight_layout()
    plt.show()
else:
    plt.figure(figsize=(10, 4))
    plt.subplot(1, 2, 1)
    plt.hist(np.array({variable_name}).flatten(), bins=30)
    plt.title('直方图')
    plt.subplot(1, 2, 2)
    plt.boxplot(np.array({variable_name}).flatten())
    plt.title('箱线图')
    plt.tight_layout()
    plt.show()
""",
        'correlation': f"""
# 相关性分析
if hasattr({variable_name}, 'corr'):
    import matplotlib.pyplot as plt
    import seaborn as sns
    
    corr = {variable_name}.corr()
    print("相关性矩阵:")
    print(corr)
    
    plt.figure(figsize=(10, 8))
    sns.heatmap(corr, annot=True, cmap='coolwarm', center=0)
    plt.title('相关性热力图')
    plt.tight_layout()
    plt.show()
else:
    print("该变量不支持相关性分析")
"""
    }
    
    suggested_code = analysis_code_templates.get(analysis_type, analysis_code_templates['overview'])
    
    return {
        'variable_name': variable_name,
        'analysis_type': analysis_type,
        'suggested_code': suggested_code.strip(),
        'description': f"对变量 '{variable_name}' 进行 {analysis_type} 分析"
    }
