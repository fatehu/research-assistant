"""
ReAct Agent 服务
实现 Reasoning + Acting 的智能代理框架
"""
import json
import re
from typing import AsyncGenerator, List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from loguru import logger

from app.config import settings
from app.services.llm_service import LLMService
from app.services.agent_tools import ToolRegistry, ToolResult


class AgentState(Enum):
    """Agent 状态"""
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    ANSWERING = "answering"
    DONE = "done"
    ERROR = "error"


@dataclass
class AgentStep:
    """Agent 执行步骤"""
    step_type: str  # thought, action, observation, answer
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    tool_name: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[str] = None
    success: Optional[bool] = None


@dataclass
class AgentContext:
    """Agent 执行上下文"""
    messages: List[Dict[str, str]]
    steps: List[AgentStep] = field(default_factory=list)
    state: AgentState = AgentState.IDLE
    iteration: int = 0
    max_iterations: int = field(default_factory=lambda: settings.react_max_iterations)
    final_answer: str = ""
    error: Optional[str] = None


class ReActAgent:
    """
    ReAct Agent 实现
    
    ReAct (Reasoning + Acting) 框架流程:
    1. Thought: 分析问题，决定下一步行动
    2. Action: 选择并执行工具
    3. Observation: 观察工具执行结果
    4. 重复 1-3 直到得到最终答案
    5. Answer: 输出最终回答
    """
    
    # ReAct 系统提示词
    SYSTEM_PROMPT = """你是一个智能AI助手，可以使用以下工具来帮助回答问题：

{tools_description}

## 重要：输出格式要求

你必须严格按照以下XML格式输出，不要输出任何格式之外的内容：

**情况1 - 需要使用工具：**
```
<think>简要分析为什么需要使用工具</think>
<action>{{"tool": "工具名", "input": {{"参数": "值"}}}}</action>
```

**情况2 - 直接回答（不需要工具）：**
```
<think>简要分析</think>
<answer>你的回答内容</answer>
```

**情况3 - 收到工具结果后：**
```
<think>根据工具结果分析</think>
<answer>基于工具结果的完整回答</answer>
```

## 规则

1. **必须**使用 `<think>`, `<action>`, `<answer>` 标签
2. **禁止**在标签外输出任何内容
3. action 内容必须是合法的 JSON 格式
4. 每次只能调用一个工具
5. 使用中文回复

## 工具说明

- `web_search`: 搜索互联网，参数 query（搜索词）
- `knowledge_search`: 搜索知识库，参数 query（搜索词）
- `calculator`: 数学计算，参数 expression（表达式如 sqrt(16)）
- `datetime`: 获取时间，参数 action（now/date/weekday）
- `unit_converter`: 单位转换，参数 value, from_unit, to_unit

## 示例

用户问"今天天气怎么样"，你应该输出：
<think>用户询问天气，需要搜索最新信息</think>
<action>{{"tool": "web_search", "input": {{"query": "今天天气"}}}}</action>

收到搜索结果后，你应该输出：
<think>已获取天气信息，可以回答用户</think>
<answer>根据搜索结果，今天的天气是...</answer>
"""

    def __init__(
        self,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        max_iterations: int = None,
    ):
        self.llm = llm_service
        self.tools = tool_registry
        self.max_iterations = max_iterations if max_iterations is not None else settings.react_max_iterations
    
    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        tools_desc = self.tools.get_tools_description()
        return self.SYSTEM_PROMPT.format(tools_description=tools_desc)
    
    def _parse_response(self, response: str) -> Dict[str, Any]:
        """
        解析 LLM 响应，提取思考、行动和回答
        """
        result = {
            "thought": None,
            "action": None,
            "answer": None,
            "raw": response,
        }
        
        # 提取思考内容
        think_match = re.search(r'<think>(.*?)</think>', response, re.DOTALL)
        if think_match:
            result["thought"] = think_match.group(1).strip()
        
        # 提取行动内容
        action_match = re.search(r'<action>(.*?)</action>', response, re.DOTALL)
        if action_match:
            try:
                action_str = action_match.group(1).strip()
                result["action"] = json.loads(action_str)
            except json.JSONDecodeError as e:
                logger.warning(f"无法解析 action JSON: {e}")
                # 尝试修复常见的 JSON 问题
                try:
                    # 替换单引号为双引号
                    fixed = action_str.replace("'", '"')
                    result["action"] = json.loads(fixed)
                except:
                    pass
        
        # 提取回答内容
        answer_match = re.search(r'<answer>(.*?)</answer>', response, re.DOTALL)
        if answer_match:
            result["answer"] = answer_match.group(1).strip()
        
        return result
    
    async def run(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        运行 ReAct Agent
        
        Args:
            messages: 对话历史
            stream: 是否流式输出
            
        Yields:
            事件字典，包含 type 和 data
        """
        context = AgentContext(
            messages=messages.copy(),
            max_iterations=self.max_iterations,
        )
        
        system_prompt = self._build_system_prompt()
        
        # 发送开始事件
        yield {
            "type": "start",
            "data": {
                "provider": self.llm.provider,
                "model": self.llm.config["model"],
            }
        }
        
        while context.iteration < context.max_iterations:
            context.iteration += 1
            context.state = AgentState.THINKING
            
            logger.info(f"ReAct 迭代 {context.iteration}/{context.max_iterations}")
            
            # 调用 LLM
            if stream:
                async for event in self._stream_iteration(context, system_prompt):
                    yield event
                    
                    # 检查是否完成
                    if event["type"] == "answer":
                        context.state = AgentState.DONE
                        context.final_answer = event["data"]
                        break
                    elif event["type"] == "error":
                        context.state = AgentState.ERROR
                        context.error = event["data"]
                        break
            else:
                result = await self._run_iteration(context, system_prompt)
                for event in result:
                    yield event
            
            # 检查是否完成
            if context.state in [AgentState.DONE, AgentState.ERROR]:
                break
        
        # 如果达到最大迭代次数但没有答案，强制生成答案
        if context.state != AgentState.DONE and not context.final_answer:
            yield {
                "type": "thought",
                "data": "已达到最大迭代次数，根据已有信息生成回答。"
            }
            
            # 构建总结提示
            summary_messages = context.messages.copy()
            summary_messages.append({
                "role": "user",
                "content": "请根据以上信息直接给出最终回答，使用 <answer></answer> 标签包裹。"
            })
            
            full_response = ""
            async for chunk in self.llm.chat_stream(summary_messages, system_prompt):
                full_response += chunk
            
            parsed = self._parse_response(full_response)
            if parsed["answer"]:
                context.final_answer = parsed["answer"]
                yield {"type": "answer", "data": parsed["answer"]}
            else:
                # 清理响应作为答案
                clean_answer = re.sub(r'</?(?:think|action|answer|observation)>', '', full_response).strip()
                context.final_answer = clean_answer
                yield {"type": "answer", "data": clean_answer}
        
        # 发送完成事件
        # 获取最后一次思考内容
        last_thought = ""
        for step in reversed(context.steps):
            if step.step_type == "thought":
                last_thought = step.content
                break
        
        logger.info(f"[ReAct] 完成: iterations={context.iteration}, steps={len(context.steps)}, answer_len={len(context.final_answer)}")
        
        yield {
            "type": "done",
            "data": {
                "iterations": context.iteration,
                "steps": len(context.steps),
                "thought": last_thought,
                "answer": context.final_answer,
            }
        }
    
    async def _stream_iteration(
        self,
        context: AgentContext,
        system_prompt: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        执行一次流式迭代
        """
        full_response = ""
        buffer = ""
        current_mode = None
        think_content = ""
        action_content = ""
        answer_content = ""
        
        logger.info(f"[ReAct] 开始迭代 {context.iteration}")
        
        yield {"type": "thinking_start", "data": ""}
        
        async for chunk in self.llm.chat_stream(context.messages, system_prompt):
            full_response += chunk
            buffer += chunk
            
            # 状态机解析
            while True:
                processed = False
                
                if current_mode is None:
                    # 检查是否进入某个标签
                    if "<think>" in buffer:
                        idx = buffer.find("<think>")
                        # 丢弃 <think> 之前的内容
                        buffer = buffer[idx + 7:]
                        current_mode = "think"
                        logger.debug(f"[ReAct] 进入 think 模式")
                        processed = True
                    elif "<action>" in buffer:
                        idx = buffer.find("<action>")
                        buffer = buffer[idx + 8:]
                        current_mode = "action"
                        logger.debug(f"[ReAct] 进入 action 模式")
                        processed = True
                    elif "<answer>" in buffer:
                        idx = buffer.find("<answer>")
                        buffer = buffer[idx + 8:]
                        current_mode = "answer"
                        logger.debug(f"[ReAct] 进入 answer 模式")
                        processed = True
                        
                elif current_mode == "think":
                    if "</think>" in buffer:
                        idx = buffer.find("</think>")
                        think_content += buffer[:idx]
                        buffer = buffer[idx + 8:]
                        current_mode = None
                        
                        final_thought = think_content.strip()
                        logger.info(f"[ReAct] 思考完成: {final_thought[:100]}...")
                        
                        # 记录思考步骤
                        step = AgentStep(
                            step_type="thought",
                            content=final_thought
                        )
                        context.steps.append(step)
                        
                        yield {"type": "thought", "data": final_thought}
                        think_content = ""  # 重置
                        processed = True
                    else:
                        # 流式输出思考内容
                        if len(buffer) > 15:
                            send_chunk = buffer[:-15]
                            think_content += send_chunk
                            yield {"type": "thinking", "data": send_chunk}
                            buffer = buffer[-15:]
                        break
                        
                elif current_mode == "action":
                    if "</action>" in buffer:
                        idx = buffer.find("</action>")
                        action_content += buffer[:idx]
                        action_str = action_content.strip()
                        buffer = buffer[idx + 9:]
                        current_mode = None
                        
                        logger.info(f"[ReAct] 收到 action: {action_str}")
                        
                        # 解析并执行工具
                        try:
                            action_data = json.loads(action_str)
                            tool_name = action_data.get("tool")
                            tool_input = action_data.get("input", {})
                            
                            logger.info(f"[ReAct] 执行工具: {tool_name}, 参数: {tool_input}")
                            
                            yield {
                                "type": "action",
                                "data": {
                                    "tool": tool_name,
                                    "input": tool_input
                                }
                            }
                            
                            # 执行工具
                            result = await self.tools.execute(tool_name, **tool_input)
                            
                            logger.info(f"[ReAct] 工具结果: success={result.success}, output={result.output[:200]}...")
                            
                            # 记录行动步骤
                            step = AgentStep(
                                step_type="action",
                                content=action_str,
                                tool_name=tool_name,
                                tool_input=tool_input,
                                tool_output=result.output,
                                success=result.success
                            )
                            context.steps.append(step)
                            
                            yield {
                                "type": "observation",
                                "data": {
                                    "tool": tool_name,
                                    "success": result.success,
                                    "output": result.output[:2000]
                                }
                            }
                            
                            # 将工具结果添加到对话历史
                            context.messages.append({
                                "role": "assistant",
                                "content": full_response
                            })
                            context.messages.append({
                                "role": "user",
                                "content": f"<observation>\n{result.output}\n</observation>\n\n请根据工具返回的信息继续思考。如果信息足够，请给出最终回答；如果需要更多信息，可以继续使用工具。"
                            })
                            
                            logger.info(f"[ReAct] 工具执行完成，返回以开始新迭代")
                            # 返回以开始新迭代
                            return
                            
                        except json.JSONDecodeError as e:
                            logger.error(f"[ReAct] 工具调用 JSON 解析失败: {e}, 内容: {action_str}")
                            # 不返回错误，继续处理
                            yield {
                                "type": "thought",
                                "data": f"工具调用格式错误，尝试直接回答"
                            }
                            action_content = ""
                            processed = True
                            continue
                        except Exception as e:
                            logger.error(f"[ReAct] 工具执行失败: {e}")
                            yield {
                                "type": "observation",
                                "data": {
                                    "tool": tool_name if 'tool_name' in dir() else "unknown",
                                    "success": False,
                                    "output": f"工具执行失败: {str(e)}"
                                }
                            }
                            action_content = ""
                            processed = True
                            continue
                        
                    else:
                        # 累积 action 内容
                        if len(buffer) > 15:
                            action_content += buffer[:-15]
                            buffer = buffer[-15:]
                        break
                        
                elif current_mode == "answer":
                    if "</answer>" in buffer:
                        idx = buffer.find("</answer>")
                        answer_content += buffer[:idx]
                        buffer = buffer[idx + 9:]
                        current_mode = None
                        
                        final_answer = answer_content.strip()
                        logger.info(f"[ReAct] 回答完成: {final_answer[:100]}...")
                        
                        # 记录回答步骤
                        step = AgentStep(
                            step_type="answer",
                            content=final_answer
                        )
                        context.steps.append(step)
                        
                        yield {"type": "answer", "data": final_answer}
                        return
                    else:
                        # 流式输出回答内容
                        if len(buffer) > 15:
                            send_chunk = buffer[:-15]
                            answer_content += send_chunk
                            yield {"type": "content", "data": send_chunk}
                            buffer = buffer[-15:]
                        break
                
                if not processed:
                    break
        
        # 处理剩余缓冲区
        logger.info(f"[ReAct] 处理剩余缓冲区, current_mode={current_mode}, buffer长度={len(buffer)}")
        logger.debug(f"[ReAct] 完整响应: {full_response}")
        
        # 如果还在某个模式中，处理剩余内容
        if current_mode == "think" and buffer.strip():
            think_content += buffer
            final_thought = re.sub(r'</think>.*', '', think_content).strip()
            if final_thought:
                yield {"type": "thought", "data": final_thought}
        elif current_mode == "answer" and buffer.strip():
            answer_content += buffer
            final_answer = re.sub(r'</answer>.*', '', answer_content).strip()
            if final_answer:
                yield {"type": "answer", "data": final_answer}
        elif current_mode == "action" and buffer.strip():
            # action 模式但没有结束标签
            action_content += buffer
            action_str = action_content.strip()
            logger.warning(f"[ReAct] action 模式未正常结束，尝试解析: {action_str}")
            
            # 尝试提取 JSON
            json_match = re.search(r'\{[^{}]*"tool"[^{}]*\}', action_str)
            if json_match:
                try:
                    action_data = json.loads(json_match.group())
                    tool_name = action_data.get("tool")
                    tool_input = action_data.get("input", {})
                    
                    logger.info(f"[ReAct] 从未结束的 action 中提取到工具调用: {tool_name}")
                    
                    yield {
                        "type": "action",
                        "data": {"tool": tool_name, "input": tool_input}
                    }
                    
                    result = await self.tools.execute(tool_name, **tool_input)
                    
                    yield {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": result.success,
                            "output": result.output[:2000]
                        }
                    }
                    
                    # 更新上下文继续迭代
                    context.messages.append({
                        "role": "assistant",
                        "content": full_response
                    })
                    context.messages.append({
                        "role": "user", 
                        "content": f"<observation>\n{result.output}\n</observation>\n\n请根据工具返回的信息，使用<answer>标签给出最终回答。"
                    })
                    return
                except Exception as e:
                    logger.error(f"[ReAct] 解析未结束的 action 失败: {e}")
        
        # 检查完整响应中是否有未被解析的 action（裸 JSON）
        if not answer_content:
            # 尝试检测裸 JSON 格式的 action
            json_pattern = r'\{[^{}]*"tool"\s*:\s*"[^"]+"\s*,\s*"input"\s*:\s*\{[^{}]*\}[^{}]*\}'
            json_matches = re.findall(json_pattern, full_response)
            
            if json_matches:
                logger.warning(f"[ReAct] 检测到裸 JSON action: {json_matches[0][:100]}...")
                try:
                    action_data = json.loads(json_matches[0])
                    tool_name = action_data.get("tool")
                    tool_input = action_data.get("input", {})
                    
                    yield {
                        "type": "action",
                        "data": {"tool": tool_name, "input": tool_input}
                    }
                    
                    result = await self.tools.execute(tool_name, **tool_input)
                    
                    yield {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": result.success,
                            "output": result.output[:2000]
                        }
                    }
                    
                    # 更新上下文继续迭代
                    context.messages.append({
                        "role": "assistant",
                        "content": full_response
                    })
                    context.messages.append({
                        "role": "user",
                        "content": f"<observation>\n{result.output}\n</observation>\n\n请根据工具返回的信息，使用<answer>标签给出最终回答。"
                    })
                    return
                except Exception as e:
                    logger.error(f"[ReAct] 解析裸 JSON action 失败: {e}")
            
            # 如果还是没有答案，清理响应作为答案
            clean_response = re.sub(r'</?(?:think|action|answer|observation)>', '', full_response)
            # 移除 JSON 对象
            clean_response = re.sub(r'\{[^{}]*"tool"[^{}]*\}', '', clean_response).strip()
            if clean_response:
                logger.warning(f"[ReAct] 未找到标准格式，使用清理后的响应作为答案")
                yield {"type": "answer", "data": clean_response}
    
    async def _run_iteration(
        self,
        context: AgentContext,
        system_prompt: str,
    ) -> List[Dict[str, Any]]:
        """
        执行一次非流式迭代
        """
        events = []
        
        # 调用 LLM
        response = await self.llm.chat(context.messages, system_prompt)
        content = response["content"]
        
        # 解析响应
        parsed = self._parse_response(content)
        
        # 处理思考
        if parsed["thought"]:
            events.append({"type": "thought", "data": parsed["thought"]})
            context.steps.append(AgentStep(
                step_type="thought",
                content=parsed["thought"]
            ))
        
        # 处理行动
        if parsed["action"]:
            tool_name = parsed["action"].get("tool")
            tool_input = parsed["action"].get("input", {})
            
            events.append({
                "type": "action",
                "data": {"tool": tool_name, "input": tool_input}
            })
            
            # 执行工具
            result = await self.tools.execute(tool_name, **tool_input)
            
            events.append({
                "type": "observation",
                "data": {
                    "tool": tool_name,
                    "success": result.success,
                    "output": result.output[:2000]
                }
            })
            
            context.steps.append(AgentStep(
                step_type="action",
                content=json.dumps(parsed["action"]),
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=result.output,
                success=result.success
            ))
            
            # 更新对话历史
            context.messages.append({
                "role": "assistant",
                "content": content
            })
            context.messages.append({
                "role": "user",
                "content": f"<observation>\n{result.output}\n</observation>\n\n请根据工具返回的信息继续。"
            })
        
        # 处理回答
        if parsed["answer"]:
            events.append({"type": "answer", "data": parsed["answer"]})
            context.final_answer = parsed["answer"]
            context.state = AgentState.DONE
            context.steps.append(AgentStep(
                step_type="answer",
                content=parsed["answer"]
            ))
        
        return events


def create_react_agent(
    llm_service: LLMService,
    tool_registry: ToolRegistry,
    max_iterations: int = None,
) -> ReActAgent:
    """创建 ReAct Agent 实例"""
    if max_iterations is None:
        max_iterations = settings.react_max_iterations
    logger.info(f"[ReAct] 创建 Agent, 最大迭代次数: {max_iterations}")
    return ReActAgent(llm_service, tool_registry, max_iterations)
