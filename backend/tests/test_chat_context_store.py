import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.chat_context_store import (
    ConversationItemEntry,
    ConversationItemStreamStore,
    ConversationTurnEntry,
    ConversationTurnStore,
    HistoryLog,
    ToolLedgerEntry,
    ToolLedgerStore,
    build_context_snapshot_payload,
)


def test_history_log_and_snapshot_payload_are_structured():
    history = HistoryLog()
    history.add("manual_compact", "compacted_messages=4")
    history_payload = history.to_payload()

    snapshot = build_context_snapshot_payload(
        mode="manual",
        context_state={"version": "conversation_context_state.v3", "active_topic": "注意力机制"},
        compacted_history={
            "version": "conversation_compacted_history.v2",
            "history_anchors": "开场目标",
            "replacement_history": [{"role": "system", "content": "继续围绕注意力机制展开。"}],
        },
        summary_text="summary",
        compacted_message_count=4,
        up_to_message_id=12,
    )

    assert history_payload["version"] == "conversation_history_log.v1"
    assert history_payload["events"][0]["title"] == "manual_compact"
    assert snapshot["version"] == "conversation_context_snapshot.v1"
    assert snapshot["mode"] == "manual"
    assert snapshot["compacted_history"]["history_anchors"] == "开场目标"


def test_tool_ledger_store_compacts_and_replays_entries():
    store = ToolLedgerStore()
    store.append(
        ToolLedgerEntry(
            entry_id="call-1",
            kind="tool_call",
            tool_name="knowledge_search",
            tool_call_id="tool-call-1",
            iteration=1,
            status="started",
            arguments={"query": "attention mechanism"},
        )
    )
    store.append(
        ToolLedgerEntry(
            entry_id="result-1",
            kind="tool_result",
            tool_name="knowledge_search",
            tool_call_id="tool-call-1",
            iteration=1,
            status="succeeded",
            summary="检索到 Bahdanau 2014 相关资料。",
            success=True,
            metadata={"source_kind": "knowledge_base_search", "source_labels": ["来源1"]},
        )
    )
    store.compact(keep_last=1)

    payload = store.to_payload()

    assert payload["version"] == "conversation_tool_ledger.v1"
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["kind"] == "tool_result"
    assert payload["entries"][0]["metadata"]["source_kind"] == "knowledge_base_search"
    restored = ToolLedgerStore.from_payload(payload)
    assert restored.entries[0].tool_name == "knowledge_search"
    assert restored.entries[0].metadata == {"source_kind": "knowledge_base_search", "source_labels": ["来源1"]}


def test_turn_store_compacts_and_replays_entries():
    store = ConversationTurnStore()
    store.upsert(
        ConversationTurnEntry(
            turn_id="turn:1",
            status="running",
            user_message_id=1,
            user_content="解释注意力机制。",
            started_at="2026-04-02T09:00:00",
        )
    )
    store.upsert(
        ConversationTurnEntry(
            turn_id="turn:2",
            status="completed",
            user_message_id=2,
            assistant_message_id=3,
            assistant_summary="注意力机制是一种动态聚焦机制。",
            iteration_count=2,
            tool_call_count=1,
            tool_result_count=1,
            started_at="2026-04-02T09:01:00",
            completed_at="2026-04-02T09:01:10",
        )
    )
    store.compact(keep_last=1)

    payload = store.to_payload()

    assert payload["version"] == "conversation_turn_store.v1"
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["turn_id"] == "turn:2"
    restored = ConversationTurnStore.from_payload(payload)
    assert restored.entries[0].assistant_summary == "注意力机制是一种动态聚焦机制。"


def test_turn_store_from_payload_normalizes_none_like_strings():
    restored = ConversationTurnStore.from_payload(
        {
            "version": "conversation_turn_store.v1",
            "entries": [
                {
                    "turn_id": "turn:2",
                    "status": "completed",
                    "run_id": "None",
                    "user_content": "继续解释。",
                    "assistant_summary": "None",
                    "error_message": "null",
                    "started_at": "2026-04-03T06:00:00",
                    "completed_at": "None",
                }
            ],
        }
    )

    assert restored.entries[0].run_id is None
    assert restored.entries[0].assistant_summary is None
    assert restored.entries[0].error_message is None
    assert restored.entries[0].completed_at is None


def test_item_stream_store_compacts_and_replays_entries():
    store = ConversationItemStreamStore()
    store.append(
        ConversationItemEntry(
            item_id="item-1",
            kind="user_message",
            turn_id="turn:1",
            role="user",
            content="解释注意力机制。",
            message_id=1,
        )
    )
    store.append(
        ConversationItemEntry(
            item_id="item-2",
            kind="tool_call",
            turn_id="turn:1",
            role="tool",
            tool_name="knowledge_search",
            tool_call_id="call-1",
            iteration=1,
            status="started",
            arguments={"query": "attention mechanism"},
        )
    )
    store.compact(keep_last=1)

    payload = store.to_payload()

    assert payload["version"] == "conversation_item_stream.v1"
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["kind"] == "tool_call"
    assert payload["entries"][0]["turn_id"] == "turn:1"
    restored = ConversationItemStreamStore.from_payload(payload)
    assert restored.entries[0].tool_name == "knowledge_search"
    assert restored.entries[0].turn_id == "turn:1"


def test_item_stream_store_canonical_history_uses_latest_compact_boundary():
    store = ConversationItemStreamStore.from_payload(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "user_message",
                    "turn_id": "turn:100",
                    "role": "user",
                    "content": "旧问题",
                    "message_id": 100,
                },
                {
                    "item_id": "item-2",
                    "kind": "assistant_message",
                    "turn_id": "turn:100",
                    "role": "assistant",
                    "content": "旧回答",
                    "message_id": 101,
                },
                {
                    "item_id": "item-3",
                    "kind": "compact_boundary",
                    "role": "system",
                    "message_id": 101,
                    "metadata": {
                        "compact_boundary_message_id": 101,
                        "replacement_history": [
                            {"role": "system", "content": "此前已经解释旧问题。"},
                        ],
                    },
                },
                {
                    "item_id": "item-4",
                    "kind": "user_message",
                    "turn_id": "turn:200",
                    "role": "user",
                    "content": "新问题",
                    "message_id": 102,
                },
                {
                    "item_id": "item-5",
                    "kind": "assistant_message",
                    "turn_id": "turn:200",
                    "role": "assistant",
                    "content": "新回答",
                    "message_id": 103,
                },
            ],
        }
    )

    canonical = store.canonical_history()

    assert canonical.boundary_message_id == 101
    assert canonical.replacement_history[0]["content"] == "此前已经解释旧问题。"
    assert [entry.content for entry in canonical.active_entries if entry.role in {"user", "assistant"}] == [
        "新问题",
        "新回答",
    ]


def test_item_stream_store_canonical_history_keeps_current_turn_when_requested():
    store = ConversationItemStreamStore.from_payload(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "user_message",
                    "turn_id": "turn:100",
                    "role": "user",
                    "content": "旧问题",
                    "message_id": 100,
                },
                {
                    "item_id": "item-2",
                    "kind": "user_message",
                    "turn_id": "turn:200",
                    "role": "user",
                    "content": "当前问题",
                    "message_id": 200,
                },
                {
                    "item_id": "item-3",
                    "kind": "compact_boundary",
                    "turn_id": "turn:200",
                    "role": "system",
                    "message_id": 200,
                    "metadata": {
                        "compact_boundary_message_id": 200,
                        "keep_turn_id": "turn:200",
                        "replacement_history": [
                            {"role": "system", "content": "此前已经压缩旧历史。"},
                        ],
                    },
                },
                {
                    "item_id": "item-4",
                    "kind": "tool_result",
                    "turn_id": "turn:200",
                    "role": "tool",
                    "tool_name": "knowledge_search",
                    "tool_call_id": "call-1",
                    "summary": "工具结果",
                },
            ],
        }
    )

    canonical = store.canonical_history()

    assert canonical.keep_turn_id == "turn:200"
    assert [entry.content for entry in canonical.active_entries if entry.role == "user"] == ["当前问题"]


def test_item_stream_store_canonical_history_uses_newest_surviving_replacement_checkpoint():
    store = ConversationItemStreamStore.from_payload(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "compact_boundary",
                    "role": "system",
                    "message_id": 90,
                    "metadata": {
                        "compact_boundary_message_id": 90,
                        "replacement_history": [
                            {"role": "system", "content": "最早的替代历史。"},
                        ],
                    },
                },
                {
                    "item_id": "item-2",
                    "kind": "user_message",
                    "turn_id": "turn:100",
                    "role": "user",
                    "content": "中间问题",
                    "message_id": 100,
                },
                {
                    "item_id": "item-3",
                    "kind": "compact_boundary",
                    "role": "system",
                    "message_id": 100,
                    "metadata": {
                        "compact_boundary_message_id": 100,
                        "replacement_history": [
                            {"role": "system", "content": "中间替代历史。"},
                        ],
                    },
                },
                {
                    "item_id": "item-4",
                    "kind": "user_message",
                    "turn_id": "turn:200",
                    "role": "user",
                    "content": "当前问题",
                    "message_id": 200,
                },
                {
                    "item_id": "item-5",
                    "kind": "compact_boundary",
                    "turn_id": "turn:200",
                    "role": "system",
                    "message_id": 200,
                    "metadata": {
                        "compact_boundary_message_id": 200,
                        "keep_turn_id": "turn:200",
                    },
                },
                {
                    "item_id": "item-6",
                    "kind": "assistant_message",
                    "turn_id": "turn:200",
                    "role": "assistant",
                    "content": "当前回答",
                    "message_id": 201,
                },
            ],
        }
    )

    canonical = store.canonical_history()

    assert canonical.boundary_message_id == 200
    assert canonical.keep_turn_id == "turn:200"
    assert canonical.replacement_checkpoint_item_id == "item-3"
    assert canonical.replacement_history == [{"role": "system", "content": "中间替代历史。"}]
    assert [entry.content for entry in canonical.active_entries if entry.role in {"user", "assistant"}] == [
        "当前问题",
        "当前回答",
    ]


def test_item_stream_store_canonical_replay_rows_include_replacement_history_and_summaries():
    store = ConversationItemStreamStore.from_payload(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "compact_boundary",
                    "role": "system",
                    "message_id": 10,
                    "metadata": {
                        "compact_boundary_message_id": 10,
                        "replacement_history": [
                            {"role": "system", "content": "此前已经建立任务背景。"},
                        ],
                    },
                },
                {
                    "item_id": "item-2",
                    "kind": "user_message",
                    "turn_id": "turn:20",
                    "role": "user",
                    "content": "解释当前问题",
                    "message_id": 20,
                },
                {
                    "item_id": "item-3",
                    "kind": "reasoning_summary",
                    "turn_id": "turn:20",
                    "role": "assistant",
                    "summary": "需要先解释核心概念，再补充背景。",
                },
                {
                    "item_id": "item-4",
                    "kind": "assistant_message",
                    "turn_id": "turn:20",
                    "role": "assistant",
                    "content": "这是当前回答。",
                    "message_id": 21,
                },
            ],
        }
    )

    replay_rows = store.canonical_replay_rows()

    assert replay_rows[0] == {"role": "system", "content": "此前已经建立任务背景。"}
    assert replay_rows[1]["role"] == "user"
    assert replay_rows[1]["content"] == "解释当前问题"
    assert replay_rows[2]["role"] == "assistant"
    assert replay_rows[2]["thought"] == "需要先解释核心概念，再补充背景。"
    assert replay_rows[3]["content"] == "这是当前回答。"


def test_item_stream_store_canonical_active_message_rows_exclude_replacement_history():
    store = ConversationItemStreamStore.from_payload(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "compact_boundary",
                    "role": "system",
                    "message_id": 10,
                    "metadata": {
                        "compact_boundary_message_id": 10,
                        "replacement_history": [
                            {"role": "system", "content": "此前已经建立任务背景。"},
                        ],
                    },
                },
                {
                    "item_id": "item-2",
                    "kind": "user_message",
                    "role": "user",
                    "content": "解释当前问题",
                    "message_id": 20,
                },
                {
                    "item_id": "item-3",
                    "kind": "assistant_message",
                    "role": "assistant",
                    "content": "这是当前回答。",
                    "message_id": 21,
                },
            ],
        }
    )

    replay_rows = store.canonical_active_message_rows()

    assert [item["role"] for item in replay_rows] == ["user", "assistant"]
    assert [item["content"] for item in replay_rows] == ["解释当前问题", "这是当前回答。"]


def test_item_stream_store_compact_preserves_latest_boundary_and_kept_turn():
    store = ConversationItemStreamStore.from_payload(
        {
            "version": "conversation_item_stream.v1",
            "entries": [
                {
                    "item_id": "item-1",
                    "kind": "user_message",
                    "turn_id": "turn:100",
                    "role": "user",
                    "content": "旧问题",
                    "message_id": 100,
                },
                {
                    "item_id": "item-2",
                    "kind": "assistant_message",
                    "turn_id": "turn:100",
                    "role": "assistant",
                    "content": "旧回答",
                    "message_id": 101,
                },
                {
                    "item_id": "item-3",
                    "kind": "user_message",
                    "turn_id": "turn:200",
                    "role": "user",
                    "content": "当前问题",
                    "message_id": 200,
                },
                {
                    "item_id": "item-4",
                    "kind": "compact_boundary",
                    "turn_id": "turn:200",
                    "role": "system",
                    "message_id": 200,
                    "metadata": {
                        "compact_boundary_message_id": 200,
                        "keep_turn_id": "turn:200",
                        "replacement_history": [
                            {"role": "system", "content": "此前已经压缩旧历史。"},
                        ],
                    },
                },
                {
                    "item_id": "item-5",
                    "kind": "tool_result",
                    "turn_id": "turn:200",
                    "role": "tool",
                    "tool_name": "knowledge_search",
                    "tool_call_id": "call-1",
                    "summary": "工具结果",
                },
                {
                    "item_id": "item-6",
                    "kind": "assistant_message",
                    "turn_id": "turn:200",
                    "role": "assistant",
                    "content": "当前回答",
                    "message_id": 201,
                },
            ],
        }
    )

    store.compact(keep_last=2)

    assert [entry.item_id for entry in store.entries] == ["item-3", "item-4", "item-5", "item-6"]
