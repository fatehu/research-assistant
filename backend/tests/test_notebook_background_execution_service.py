import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.notebook_background_execution_service import NotebookBackgroundExecutionManager


@pytest.mark.asyncio
async def test_background_execution_wait_returns_completed_result_payload():
    manager = NotebookBackgroundExecutionManager()
    finalized = []

    async def finalize(snapshot, result_payload):
        finalized.append((snapshot, result_payload))

    execution = await manager.start(
        notebook_id="nb-bg-fast",
        user_id=1,
        cell_id="cell-fast",
        description="fast task",
        execute_fn=lambda: {"success": True, "outputs": [{"output_type": "stream", "content": "done"}]},
        cancel_fn=None,
        finalize_fn=finalize,
    )

    snapshot = await manager.wait(
        execution_id=execution["execution_id"],
        notebook_id="nb-bg-fast",
        user_id=1,
        timeout_seconds=1,
        include_result=True,
    )

    assert snapshot["status"] == "completed"
    assert snapshot["success"] is True
    assert snapshot["result_payload"]["outputs"][0]["content"] == "done"
    assert finalized[0][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_background_execution_wait_timeout_does_not_cancel_task():
    manager = NotebookBackgroundExecutionManager()

    async def finalize(snapshot, result_payload):
        return None

    execution = await manager.start(
        notebook_id="nb-bg-slow",
        user_id=1,
        cell_id="cell-slow",
        description="slow task",
        execute_fn=lambda: (time.sleep(0.1) or {"success": True, "outputs": []}),
        cancel_fn=None,
        finalize_fn=finalize,
    )

    first_snapshot = await manager.wait(
        execution_id=execution["execution_id"],
        notebook_id="nb-bg-slow",
        user_id=1,
        timeout_seconds=0.01,
    )
    second_snapshot = await manager.wait(
        execution_id=execution["execution_id"],
        notebook_id="nb-bg-slow",
        user_id=1,
        timeout_seconds=1,
    )

    assert first_snapshot["status"] in {"pending", "running"}
    assert second_snapshot["status"] == "completed"
    assert second_snapshot["success"] is True
