"""
LLM 服务 - 多厂商支持 + ReAct 思考过程 + 工具调用
"""
import re
import json
from typing import AsyncGenerator, Optional, List, Dict, Any
from openai import AsyncOpenAI
from loguru import logger

from app.config import settings


class LLMService:
    """LLM 服务类，支持多厂商"""
    
    # ReAct 系统提示词 - 强制 LLM 使用特定格式
    REACT_SYSTEM_PROMPT = """你是一个专业的AI科研助手。在回答问题时，请严格按照以下格式组织你的回复：

<think>
在这里写下你的思考过程：
- 分析用户问题的核心需求
- 考虑需要哪些知识或步骤来解答
- 规划你的回答结构
</think>

<answer>
在这里写下你的正式回答，使用清晰的 Markdown 格式。
</answer>

重要规则：
1. 必须先在 <think> 标签内思考，再在 <answer> 标签内回答
2. 思考过程要详细，展示你的推理链
3. 回答要专业、准确、有条理
4. 使用中文回复
5. 如果问题涉及代码，在 <answer> 中使用代码块"""

    # 带工具的 ReAct 系统提示词
    REACT_TOOLS_SYSTEM_PROMPT = """你是一个专业的AI科研助手，拥有以下工具可以使用：

{tools_description}

在回答问题时，请严格按照以下格式组织你的回复：

<think>
在这里写下你的思考过程：
- 分析用户问题的核心需求
- 判断是否需要使用工具获取信息
- 如果需要工具，说明原因和计划
</think>

如果需要使用工具，使用以下格式：
<action>
{{"tool": "工具名称", "input": {{"参数名": "参数值"}}}}
</action>

工具执行后，你会收到 <observation> 标签包裹的结果，然后继续思考和回答。

最终回答使用：
<answer>
在这里写下你的正式回答，使用清晰的 Markdown 格式。
如果使用了工具获取的信息，请适当引用。
</answer>

重要规则：
1. 必须先思考，判断是否需要工具
2. 如果用户问题涉及他们上传的文档或知识库内容，应该使用 knowledge_search 工具
3. 工具返回结果后，需要继续思考并给出最终回答
4. 使用中文回复"""

    def __init__(self, provider: Optional[str] = None):
        """初始化 LLM 服务"""
        self.provider = provider or settings.default_llm_provider
        self.config = settings.get_llm_config(self.provider)
        self.client = self._create_client()
        
    def _create_client(self) -> AsyncOpenAI:
        """创建 OpenAI 兼容客户端"""
        return AsyncOpenAI(
            api_key=self.config["api_key"],
            base_url=self.config["base_url"],
        )
    
    async def chat(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """非流式对话"""
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)
        
        try:
            response = await self.client.chat.completions.create(
                model=self.config["model"],
                messages=full_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            
            return {
                "content": response.choices[0].message.content,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
            }
        except Exception as e:
            logger.error(f"LLM 调用失败 [{self.provider}]: {e}")
            raise
    
    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        """流式对话"""
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)
        
        try:
            stream = await self.client.chat.completions.create(
                model=self.config["model"],
                messages=full_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                    
        except Exception as e:
            logger.error(f"LLM 流式调用失败 [{self.provider}]: {e}")
            raise
    
    async def react_chat_stream(
        self,
        messages: List[Dict[str, str]],
        available_tools: Optional[List[str]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """ReAct 风格的流式对话 - 解析 <think> 和 <answer> 标签"""
        system_prompt = self.REACT_SYSTEM_PROMPT
        if available_tools:
            tools_desc = "\n".join([f"- {tool}" for tool in available_tools])
            system_prompt += f"\n\n可用工具:\n{tools_desc}"
        
        # 发送开始事件
        yield {"type": "start", "data": {"provider": self.provider, "model": self.config["model"]}}
        
        full_response = ""
        buffer = ""
        current_mode = None  # None, 'think', 'answer'
        think_content = ""
        answer_content = ""
        think_sent = False
        
        async for chunk in self.chat_stream(messages, system_prompt):
            full_response += chunk
            buffer += chunk
            
            # 解析标签的状态机
            while True:
                processed = False
                
                if current_mode is None:
                    # 查找 <think> 开始
                    if "<think>" in buffer:
                        idx = buffer.find("<think>")
                        buffer = buffer[idx + 7:]
                        current_mode = "think"
                        yield {"type": "thinking_start", "data": ""}
                        processed = True
                    # 查找 <answer> 开始
                    elif "<answer>" in buffer:
                        idx = buffer.find("<answer>")
                        buffer = buffer[idx + 8:]
                        current_mode = "answer"
                        processed = True
                        
                elif current_mode == "think":
                    # 查找 </think> 结束
                    if "</think>" in buffer:
                        idx = buffer.find("</think>")
                        think_content += buffer[:idx]
                        buffer = buffer[idx + 8:]
                        current_mode = None
                        think_sent = True
                        yield {"type": "thought", "data": think_content.strip()}
                        processed = True
                    else:
                        # 流式发送思考内容（保留一些缓冲避免截断标签）
                        if len(buffer) > 15:
                            send_chunk = buffer[:-15]
                            think_content += send_chunk
                            yield {"type": "thinking", "data": send_chunk}
                            buffer = buffer[-15:]
                        break
                        
                elif current_mode == "answer":
                    # 查找 </answer> 结束
                    if "</answer>" in buffer:
                        idx = buffer.find("</answer>")
                        answer_chunk = buffer[:idx]
                        answer_content += answer_chunk
                        yield {"type": "content", "data": answer_chunk}
                        buffer = buffer[idx + 9:]
                        current_mode = "done"
                        processed = True
                    else:
                        # 流式发送回答内容
                        if len(buffer) > 15:
                            send_chunk = buffer[:-15]
                            answer_content += send_chunk
                            yield {"type": "content", "data": send_chunk}
                            buffer = buffer[-15:]
                        break
                
                if not processed:
                    break
        
        # 处理剩余缓冲区
        if buffer.strip():
            # 清理可能的残留标签
            clean_buffer = re.sub(r'</?(?:think|answer)>', '', buffer).strip()
            if clean_buffer:
                if current_mode == "think":
                    think_content += clean_buffer
                    yield {"type": "thought", "data": think_content.strip()}
                elif current_mode == "answer":
                    answer_content += clean_buffer
                    yield {"type": "content", "data": clean_buffer}
                elif not answer_content:
                    # 如果还没有答案内容，把剩余的作为内容发送
                    answer_content = clean_buffer
                    yield {"type": "content", "data": clean_buffer}
        
        # 如果 LLM 完全没遵循格式，直接返回完整响应
        if not answer_content.strip() and not think_content.strip():
            clean_response = re.sub(r'</?(?:think|answer)>', '', full_response).strip()
            if clean_response:
                yield {"type": "content", "data": clean_response}
                answer_content = clean_response
        
        # 发送完成事件
        yield {"type": "done", "data": {
            "thought": think_content.strip(),
            "answer": answer_content.strip()
        }}

    async def react_chat_with_tools_stream(
        self,
        messages: List[Dict[str, str]],
        tools_description: str,
        tool_executor,  # 工具执行函数
        max_iterations: int = 3,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        带工具调用的 ReAct 流式对话
        支持多轮工具调用
        """
        system_prompt = self.REACT_TOOLS_SYSTEM_PROMPT.format(
            tools_description=tools_description
        )
        
        # 发送开始事件
        yield {"type": "start", "data": {"provider": self.provider, "model": self.config["model"]}}
        
        conversation_messages = messages.copy()
        iteration = 0
        final_thought = ""
        final_answer = ""
        
        while iteration < max_iterations:
            iteration += 1
            logger.debug(f"ReAct 迭代 {iteration}")
            
            full_response = ""
            buffer = ""
            current_mode = None
            think_content = ""
            action_content = ""
            answer_content = ""
            has_action = False
            
            # 流式获取 LLM 响应
            async for chunk in self.chat_stream(conversation_messages, system_prompt):
                full_response += chunk
                buffer += chunk
                
                # 状态机解析
                while True:
                    processed = False
                    
                    if current_mode is None:
                        if "<think>" in buffer:
                            idx = buffer.find("<think>")
                            buffer = buffer[idx + 7:]
                            current_mode = "think"
                            yield {"type": "thinking_start", "data": ""}
                            processed = True
                        elif "<action>" in buffer:
                            idx = buffer.find("<action>")
                            buffer = buffer[idx + 8:]
                            current_mode = "action"
                            processed = True
                        elif "<answer>" in buffer:
                            idx = buffer.find("<answer>")
                            buffer = buffer[idx + 8:]
                            current_mode = "answer"
                            processed = True
                            
                    elif current_mode == "think":
                        if "</think>" in buffer:
                            idx = buffer.find("</think>")
                            think_content += buffer[:idx]
                            buffer = buffer[idx + 8:]
                            current_mode = None
                            final_thought = think_content.strip()
                            yield {"type": "thought", "data": final_thought}
                            processed = True
                        else:
                            if len(buffer) > 15:
                                send_chunk = buffer[:-15]
                                think_content += send_chunk
                                yield {"type": "thinking", "data": send_chunk}
                                buffer = buffer[-15:]
                            break
                            
                    elif current_mode == "action":
                        if "</action>" in buffer:
                            idx = buffer.find("</action>")
                            action_content = buffer[:idx].strip()
                            buffer = buffer[idx + 9:]
                            current_mode = None
                            has_action = True
                            processed = True
                        else:
                            if len(buffer) > 15:
                                buffer_keep = buffer[-15:]
                                action_content += buffer[:-15]
                                buffer = buffer_keep
                            break
                            
                    elif current_mode == "answer":
                        if "</answer>" in buffer:
                            idx = buffer.find("</answer>")
                            answer_content += buffer[:idx]
                            buffer = buffer[idx + 9:]
                            current_mode = "done"
                            final_answer = answer_content.strip()
                            yield {"type": "content", "data": final_answer}
                            processed = True
                        else:
                            if len(buffer) > 15:
                                send_chunk = buffer[:-15]
                                answer_content += send_chunk
                                yield {"type": "content", "data": send_chunk}
                                buffer = buffer[-15:]
                            break
                    
                    if not processed:
                        break
            
            # 处理剩余缓冲
            if buffer.strip():
                clean_buffer = re.sub(r'</?(?:think|action|answer|observation)>', '', buffer).strip()
                if clean_buffer and current_mode == "answer":
                    answer_content += clean_buffer
                    final_answer = answer_content.strip()
                    yield {"type": "content", "data": clean_buffer}
            
            # 如果有工具调用
            if has_action and action_content:
                try:
                    # 解析工具调用
                    action_data = json.loads(action_content)
                    tool_name = action_data.get("tool")
                    tool_input = action_data.get("input", {})
                    
                    yield {"type": "action", "data": {
                        "tool": tool_name,
                        "input": tool_input
                    }}
                    
                    # 执行工具
                    result = await tool_executor(tool_name, **tool_input)
                    
                    yield {"type": "observation", "data": {
                        "tool": tool_name,
                        "success": result.success,
                        "output": result.output[:1000]  # 限制长度
                    }}
                    
                    # 将工具结果添加到对话历史
                    observation_msg = f"\n<observation>\n{result.output}\n</observation>\n\n请根据以上工具返回的信息，继续思考并给出最终回答。"
                    conversation_messages.append({
                        "role": "assistant",
                        "content": full_response
                    })
                    conversation_messages.append({
                        "role": "user", 
                        "content": observation_msg
                    })
                    
                except json.JSONDecodeError as e:
                    logger.error(f"工具调用解析失败: {e}")
                    yield {"type": "error", "data": f"工具调用格式错误: {action_content}"}
                    break
                except Exception as e:
                    logger.error(f"工具执行失败: {e}")
                    yield {"type": "error", "data": f"工具执行失败: {str(e)}"}
                    break
            else:
                # 没有工具调用，直接结束
                break
        
        # 如果没有获得回答，尝试从完整响应中提取
        if not final_answer:
            clean_response = re.sub(r'</?(?:think|action|answer|observation)>', '', full_response).strip()
            if clean_response:
                final_answer = clean_response
                yield {"type": "content", "data": clean_response}
        
        # 发送完成事件
        yield {"type": "done", "data": {
            "thought": final_thought,
            "answer": final_answer
        }}


# 便捷函数
async def get_llm_service(provider: Optional[str] = None) -> LLMService:
    """获取 LLM 服务实例"""
    return LLMService(provider)
