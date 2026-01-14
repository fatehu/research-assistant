"""
LLM 服务 - 多厂商支持
"""
from typing import AsyncGenerator, Optional, List, Dict, Any
from openai import AsyncOpenAI
from loguru import logger

from app.config import settings


class LLMService:
    """LLM 服务类，支持多厂商"""
    
    # ReAct 系统提示词
    REACT_SYSTEM_PROMPT = """你是一个专业的AI科研助手，使用 ReAct (Reasoning and Acting) 框架来帮助用户解决问题。

在回答问题时，请按照以下格式思考和行动：

1. **Thought（思考）**: 分析用户的问题，思考需要什么信息或采取什么步骤
2. **Action（行动）**: 如果需要使用工具，指定要使用的工具和参数
3. **Observation（观察）**: 观察工具返回的结果
4. **Answer（回答）**: 基于思考和观察，给出最终答案

如果不需要使用工具，可以直接给出答案。

请用中文回复，保持专业且友好的语气。"""

    def __init__(self, provider: Optional[str] = None):
        """初始化 LLM 服务
        
        Args:
            provider: LLM 提供商，默认使用配置中的默认值
        """
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
        """非流式对话
        
        Args:
            messages: 对话历史
            system_prompt: 系统提示词
            temperature: 温度
            max_tokens: 最大 token 数
            
        Returns:
            完整响应
        """
        # 构建消息列表
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
        """流式对话
        
        Args:
            messages: 对话历史
            system_prompt: 系统提示词
            temperature: 温度
            max_tokens: 最大 token 数
            
        Yields:
            响应内容片段
        """
        # 构建消息列表
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
        """ReAct 风格的流式对话
        
        Args:
            messages: 对话历史
            available_tools: 可用工具列表
            
        Yields:
            ReAct 步骤数据
        """
        # 构建系统提示词
        system_prompt = self.REACT_SYSTEM_PROMPT
        if available_tools:
            tools_desc = "\n".join([f"- {tool}" for tool in available_tools])
            system_prompt += f"\n\n可用工具:\n{tools_desc}"
        else:
            system_prompt += "\n\n当前没有可用的工具，请直接基于你的知识回答问题。"
        
        # 发送开始事件
        yield {"type": "start", "data": {"provider": self.provider, "model": self.config["model"]}}
        
        # 收集完整响应用于解析
        full_response = ""
        current_section = None
        section_buffer = ""
        
        async for chunk in self.chat_stream(messages, system_prompt):
            full_response += chunk
            
            # 简单的流式解析逻辑
            # 检测 ReAct 标记
            if "**Thought" in chunk or "Thought:" in chunk or "思考:" in chunk:
                if current_section and section_buffer.strip():
                    yield {"type": current_section, "data": section_buffer.strip()}
                current_section = "thought"
                section_buffer = ""
            elif "**Action" in chunk or "Action:" in chunk or "行动:" in chunk:
                if current_section and section_buffer.strip():
                    yield {"type": current_section, "data": section_buffer.strip()}
                current_section = "action"
                section_buffer = ""
            elif "**Observation" in chunk or "Observation:" in chunk or "观察:" in chunk:
                if current_section and section_buffer.strip():
                    yield {"type": current_section, "data": section_buffer.strip()}
                current_section = "observation"
                section_buffer = ""
            elif "**Answer" in chunk or "Answer:" in chunk or "回答:" in chunk or "答案:" in chunk:
                if current_section and section_buffer.strip():
                    yield {"type": current_section, "data": section_buffer.strip()}
                current_section = "answer"
                section_buffer = ""
            else:
                section_buffer += chunk
            
            # 实时发送内容
            yield {"type": "content", "data": chunk}
        
        # 发送最后一个部分
        if current_section and section_buffer.strip():
            yield {"type": current_section, "data": section_buffer.strip()}
        
        # 发送完成事件
        yield {"type": "done", "data": {"full_response": full_response}}


# 便捷函数
async def get_llm_service(provider: Optional[str] = None) -> LLMService:
    """获取 LLM 服务实例"""
    return LLMService(provider)
