"""
测试配置 - 提供 pytest-asyncio 事件循环和通用 fixtures
"""
import pytest


@pytest.fixture
def event_loop():
    """为异步测试创建事件循环"""
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
