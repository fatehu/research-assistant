import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.chat_background_run_service import ChatBackgroundRunManager


@pytest.mark.asyncio
async def test_chat_background_run_replays_done_event_after_completion():
    manager = ChatBackgroundRunManager()

    async def execute(publish):
        await publish("content", "hello")
        await publish("done", {"answer": "hello"})
        return {"done": {"answer": "hello"}}

    run = await manager.start(run_id="run-chat-replay", user_id=1, execute_fn=execute)

    for _ in range(100):
        snapshot = await manager.get(run["run_id"], user_id=1)
        if snapshot and snapshot["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("chat background run did not complete")

    events = []
    async for payload in manager.subscribe(run["run_id"], user_id=1, replay=True):
        events.append(payload)

    assert [event["event"] for event in events] == ["content", "done"]
    assert events[-1]["data"]["answer"] == "hello"


@pytest.mark.asyncio
async def test_chat_background_run_adds_terminal_done_if_execute_did_not_publish_one():
    manager = ChatBackgroundRunManager()

    async def execute(publish):
        await publish("content", "partial")
        return {"answer": "final"}

    run = await manager.start(run_id="run-chat-terminal", user_id=1, execute_fn=execute)

    for _ in range(100):
        snapshot = await manager.get(run["run_id"], user_id=1)
        if snapshot and snapshot["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("chat background run did not complete")

    events = []
    async for payload in manager.subscribe(run["run_id"], user_id=1, replay=True):
        events.append(payload)

    assert [event["event"] for event in events] == ["content", "done"]
    assert events[-1]["data"]["answer"] == "final"


@pytest.mark.asyncio
async def test_chat_background_run_persists_published_events():
    manager = ChatBackgroundRunManager()
    persisted = []

    async def persist_event(payload):
        persisted.append(dict(payload))

    async def execute(publish):
        await publish("phase", {"key": "answering"})
        await publish("done", {"answer": "ok"})
        return {"answer": "ok"}

    run = await manager.start(
        run_id="run-chat-persist",
        user_id=1,
        execute_fn=execute,
        persist_event_fn=persist_event,
    )

    for _ in range(100):
        snapshot = await manager.get(run["run_id"], user_id=1)
        if snapshot and snapshot["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("chat background run did not complete")

    assert [item["event"] for item in persisted] == ["phase", "done"]
    assert persisted[-1]["data"]["answer"] == "ok"
