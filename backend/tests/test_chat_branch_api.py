import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import chat as chat_api


def test_branch_conversation_metadata_remaps_ids_and_clears_runs():
    metadata = {
        "context_state": {
            "version": "conversation_context_state.v3",
            "evidence_ledger": [
                {
                    "entry_id": "ev-1",
                    "summary": "evidence",
                    "turn_ids": ["turn:10"],
                }
            ],
        },
        "compacted_history": {
            "version": "conversation_compacted_history.v2",
            "compact_boundary_message_id": 10,
            "up_to_message_id": 11,
        },
        "turn_store": {
            "version": "conversation_turn_store.v1",
            "entries": [
                {
                    "turn_id": "turn:10",
                    "status": "completed",
                    "user_message_id": 10,
                    "assistant_message_id": 11,
                    "run_id": "old-run",
                }
            ],
        },
        "tool_ledger": {
            "version": "conversation_tool_ledger.v1",
            "entries": [
                {
                    "entry_id": "tool-1",
                    "kind": "tool_result",
                    "tool_name": "knowledge_search",
                    "turn_id": "turn:10",
                    "run_id": "old-run",
                }
            ],
        },
        "item_stream": {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-user",
                    "kind": "user_message",
                    "turn_id": "turn:10",
                    "role": "user",
                    "message_id": 10,
                    "run_id": "old-run",
                },
                {
                    "item_id": "item-assistant",
                    "kind": "assistant_message",
                    "turn_id": "turn:10",
                    "role": "assistant",
                    "message_id": 11,
                    "run_id": "old-run",
                },
            ],
        },
        "context_snapshots": [
            {
                "version": "conversation_context_snapshot.v1",
                "up_to_message_id": 11,
                "compacted_history": {
                    "version": "conversation_compacted_history.v2",
                    "compact_boundary_message_id": 10,
                    "up_to_message_id": 11,
                },
            }
        ],
    }

    remapped = chat_api._branch_conversation_metadata(metadata, {10: 110, 11: 111})

    turn_entry = remapped["turn_store"]["entries"][0]
    assert turn_entry["turn_id"] == "turn:110"
    assert turn_entry["user_message_id"] == 110
    assert turn_entry["assistant_message_id"] == 111
    assert turn_entry["run_id"] is None

    tool_entry = remapped["tool_ledger"]["entries"][0]
    assert tool_entry["turn_id"] == "turn:110"
    assert tool_entry["run_id"] is None

    user_item, assistant_item = remapped["item_stream"]["entries"]
    assert user_item["turn_id"] == "turn:110"
    assert user_item["message_id"] == 110
    assert user_item["run_id"] is None
    assert assistant_item["turn_id"] == "turn:110"
    assert assistant_item["message_id"] == 111
    assert assistant_item["run_id"] is None

    assert remapped["context_state"]["evidence_ledger"][0]["turn_ids"] == ["turn:110"]
    assert remapped["compacted_history"]["compact_boundary_message_id"] == 110
    assert remapped["compacted_history"]["up_to_message_id"] == 111
    assert remapped["context_snapshots"][0]["up_to_message_id"] == 111
    assert remapped["context_snapshots"][0]["compacted_history"]["compact_boundary_message_id"] == 110

    assert metadata["turn_store"]["entries"][0]["turn_id"] == "turn:10"
    assert metadata["turn_store"]["entries"][0]["run_id"] == "old-run"


def test_branch_blocking_item_detail_uses_running_state_not_started_history():
    historical_metadata = {
        "item_stream": {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "tool-start",
                    "kind": "tool_call",
                    "status": "started",
                }
            ],
        }
    }
    assert chat_api._conversation_branch_blocking_item_detail(historical_metadata) is None

    running_metadata = {
        "turn_store": {
            "version": "conversation_turn_store.v1",
            "entries": [
                {
                    "turn_id": "turn:10",
                    "status": "running",
                }
            ],
        }
    }
    blocking = chat_api._conversation_branch_blocking_item_detail(running_metadata)
    assert blocking == {"kind": "turn", "turn_id": "turn:10", "status": "running"}
