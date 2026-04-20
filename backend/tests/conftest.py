"""
测试配置。

异步事件循环由 pytest-asyncio 默认 fixture 提供，避免重复覆盖带来的兼容性问题。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import agent_tools


@pytest.fixture(autouse=True)
def _reset_shared_mcp_state_per_test():
    agent_tools.ToolRegistry.reset_shared_mcp_cache()
    yield
    agent_tools.ToolRegistry.reset_shared_mcp_cache()
