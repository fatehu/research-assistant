#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _compact_text(value: Any, limit: int = 200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _require_ok(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _long_tail(repetitions: int = 40) -> str:
    seed = (
        "请把当前对话继续保持在 chat 上下文管理主题里，"
        "围绕 thread、turn、item、canonical history、compact boundary、replacement history 展开。"
    )
    return " ".join([seed for _ in range(repetitions)])


@dataclass
class EvalCaseResult:
    name: str
    ok: bool
    duration_seconds: float
    details: Dict[str, Any]
    error: Optional[str] = None


class ChatEvalClient:
    def __init__(self, base_url: str, email: str, password: str, timeout_seconds: int = 600) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.timeout_seconds = max(int(timeout_seconds), 30)
        self.session = requests.Session()
        self.user: Dict[str, Any] | None = None

    def login(self) -> Dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/v1/auth/login",
            json={"email": self.email, "password": self.password},
            timeout=(10, 60),
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "").strip()
        _require_ok(bool(token), "login missing access_token")
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.user = dict(payload.get("user") or {})
        return payload

    def register(self, username: Optional[str] = None) -> Dict[str, Any]:
        normalized_username = str(username or self.email.split("@", 1)[0]).strip() or "chat_eval_user"
        response = self.session.post(
            f"{self.base_url}/api/v1/auth/register",
            json={
                "email": self.email,
                "username": normalized_username,
                "password": self.password,
                "full_name": normalized_username,
            },
            timeout=(10, 60),
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "").strip()
        _require_ok(bool(token), "register missing access_token")
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.user = dict(payload.get("user") or {})
        return payload

    def create_conversation(self, title: str) -> Dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/v1/chat/conversations",
            json={"title": title},
            timeout=(10, 60),
        )
        response.raise_for_status()
        return response.json()

    def get_conversation(self, conversation_id: int) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/api/v1/chat/conversations/{conversation_id}",
            timeout=(10, 120),
        )
        response.raise_for_status()
        return response.json()

    def preview(
        self,
        *,
        message: str,
        conversation_id: Optional[int],
        use_tools: Optional[bool] = None,
        chat_preference_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"message": message, "conversation_id": conversation_id}
        if use_tools is not None:
            payload["use_tools"] = bool(use_tools)
        if chat_preference_overrides:
            payload["chat_preference_overrides"] = dict(chat_preference_overrides)
        response = self.session.post(
            f"{self.base_url}/api/v1/chat/context-preview",
            json=payload,
            timeout=(10, 180),
        )
        response.raise_for_status()
        return response.json()

    def compact(self, conversation_id: int) -> Dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/v1/chat/conversations/{conversation_id}/compact",
            timeout=(10, 180),
        )
        response.raise_for_status()
        return response.json()

    def send_stream(
        self,
        *,
        message: str,
        conversation_id: Optional[int],
        use_tools: Optional[bool] = None,
        send_plan_id: Optional[str] = None,
        chat_preference_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "message": message,
            "conversation_id": conversation_id,
            "stream": True,
        }
        if use_tools is not None:
            payload["use_tools"] = bool(use_tools)
        if send_plan_id:
            payload["send_plan_id"] = send_plan_id
        if chat_preference_overrides:
            payload["chat_preference_overrides"] = dict(chat_preference_overrides)
        response = self.session.post(
            f"{self.base_url}/api/v1/chat/send",
            json=payload,
            stream=True,
            timeout=(10, self.timeout_seconds),
        )
        response.raise_for_status()

        event_counts: Dict[str, int] = {}
        content_chunks: List[str] = []
        action_events: List[Dict[str, Any]] = []
        observation_events: List[Dict[str, Any]] = []
        context_debug_events: List[Dict[str, Any]] = []
        done_payload: Dict[str, Any] | None = None
        started_conversation_id: Optional[int] = None
        started_turn_id: Optional[str] = None

        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = str(raw_line).strip()
            if not line.startswith("data: "):
                continue
            payload_str = line[6:]
            try:
                packet = json.loads(payload_str)
            except Exception:
                continue
            event = str(packet.get("event") or "").strip()
            data = packet.get("data")
            if not event:
                continue
            event_counts[event] = int(event_counts.get(event, 0)) + 1
            if event == "start" and isinstance(data, dict):
                if data.get("conversation_id") is not None:
                    started_conversation_id = int(data["conversation_id"])
                if isinstance(data.get("turn_id"), str) and data["turn_id"].strip():
                    started_turn_id = data["turn_id"].strip()
            elif event == "content":
                content_chunks.append(str(data or ""))
            elif event == "action" and isinstance(data, dict):
                action_events.append(dict(data))
            elif event == "observation" and isinstance(data, dict):
                observation_events.append(dict(data))
            elif event == "context_debug" and isinstance(data, dict):
                context_debug_events.append(dict(data))
            elif event == "done" and isinstance(data, dict):
                done_payload = dict(data)

        full_content = "".join(content_chunks)
        _require_ok(done_payload is not None, "stream finished without done payload")
        return {
            "conversation_id": started_conversation_id or conversation_id,
            "turn_id": started_turn_id,
            "event_counts": event_counts,
            "content": full_content,
            "done_payload": done_payload,
            "action_events": action_events,
            "observation_events": observation_events,
            "context_debug_events": context_debug_events,
        }


def run_case(name: str, fn) -> EvalCaseResult:
    started = time.time()
    try:
        details = fn()
        return EvalCaseResult(
            name=name,
            ok=True,
            duration_seconds=round(time.time() - started, 2),
            details=details if isinstance(details, dict) else {"value": details},
        )
    except Exception as exc:
        return EvalCaseResult(
            name=name,
            ok=False,
            duration_seconds=round(time.time() - started, 2),
            details={},
            error=f"{type(exc).__name__}: {exc}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run /chat evaluation flows against a live stack.")
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--report-path", default="")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--fail-on-live-mid-run-miss", action="store_true")
    parser.add_argument("--register-if-missing", action="store_true")
    args = parser.parse_args()

    client = ChatEvalClient(
        base_url=args.base_url,
        email=args.email,
        password=args.password,
        timeout_seconds=args.timeout_seconds,
    )

    try:
        login_payload = client.login()
    except requests.HTTPError as exc:
        if args.register_if_missing and exc.response is not None and exc.response.status_code == 401:
            login_payload = client.register()
        else:
            raise
    user_id = login_payload.get("user", {}).get("id")
    tag = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    report: Dict[str, Any] = {
        "started_at": _now_iso(),
        "base_url": args.base_url,
        "email": args.email,
        "user_id": user_id,
        "tag": tag,
        "cases": [],
        "created_conversations": [],
    }

    shared: Dict[str, Any] = {}

    def case_direct_preview_and_first_turn() -> Dict[str, Any]:
        title = f"[chat-eval][direct][{tag}] 首轮直答"
        conversation = client.create_conversation(title)
        conversation_id = int(conversation["id"])
        report["created_conversations"].append({"id": conversation_id, "title": title})
        shared["main_conversation_id"] = conversation_id

        message = "请用中文用三句话解释 thread、turn、item 的区别，不要使用工具。"
        preview = client.preview(message=message, conversation_id=conversation_id, use_tools=False)
        _require_ok(preview.get("preview_mode") == "direct", "preview_mode should be direct")
        send_plan = dict(preview.get("send_plan") or {})
        _require_ok(bool(send_plan.get("plan_id")), "preview missing send_plan.plan_id")

        streamed = client.send_stream(
            message=message,
            conversation_id=conversation_id,
            use_tools=False,
            send_plan_id=str(send_plan["plan_id"]),
        )
        _require_ok(bool(streamed["content"].strip()), "direct first turn content is empty")
        conversation_after = client.get_conversation(conversation_id)
        _require_ok(len(list(conversation_after.get("turn_store", {}).get("entries") or [])) >= 1, "turn_store missing first turn")
        _require_ok(len(list(conversation_after.get("item_stream", {}).get("entries") or [])) >= 2, "item_stream missing first turn items")
        return {
            "conversation_id": conversation_id,
            "preview_mode": preview.get("preview_mode"),
            "send_plan_id": send_plan.get("plan_id"),
            "answer_excerpt": _compact_text(streamed["content"]),
            "event_counts": streamed["event_counts"],
        }

    def case_followup_long_dialog() -> Dict[str, Any]:
        conversation_id = int(shared["main_conversation_id"])
        followup = "继续，用一个简短例子说明 thread、turn、item 在 chat 系统里分别对应什么。"
        preview = client.preview(message=followup, conversation_id=conversation_id, use_tools=False)
        send_plan = dict(preview.get("send_plan") or {})
        streamed = client.send_stream(
            message=followup,
            conversation_id=conversation_id,
            use_tools=False,
            send_plan_id=str(send_plan.get("plan_id") or ""),
        )
        _require_ok(bool(streamed["content"].strip()), "followup content is empty")

        long_messages = []
        for index in range(1, 5):
            long_message = (
                f"第 {index} 轮长对话追问：请继续围绕 chat 上下文管理解释当前系统为什么要区分事实层、派生层、展示层。"
                f" 同时把下面这段背景也纳入考虑，但不要逐句复述：{_long_tail(28)}"
            )
            long_messages.append(long_message)
            streamed = client.send_stream(
                message=long_message,
                conversation_id=conversation_id,
                use_tools=False,
            )
            _require_ok(bool(streamed["content"].strip()), f"long followup {index} content is empty")

        preview_after = client.preview(
            message="现在总结上面对话里最重要的三条上下文管理结论。",
            conversation_id=conversation_id,
            use_tools=False,
        )
        context_debug = dict(preview_after.get("context_debug") or {})
        conversation_after = client.get_conversation(conversation_id)
        turn_entries = list(conversation_after.get("turn_store", {}).get("entries") or [])
        item_entries = list(conversation_after.get("item_stream", {}).get("entries") or [])
        _require_ok(len(turn_entries) >= 6, "long dialog turn count did not grow as expected")
        _require_ok(len(item_entries) >= 12, "long dialog item count did not grow as expected")
        return {
            "conversation_id": conversation_id,
            "turn_count": len(turn_entries),
            "item_count": len(item_entries),
            "preview_message_count_sent": context_debug.get("message_count_sent"),
            "preview_context_truncated": context_debug.get("context_truncated"),
        }

    def case_manual_compact_and_old_send_plan() -> Dict[str, Any]:
        conversation_id = int(shared["main_conversation_id"])
        preview = client.preview(
            message="在 compact 之后，再帮我用一句话提醒什么是 canonical replay。",
            conversation_id=conversation_id,
            use_tools=False,
        )
        stale_plan_id = str((preview.get("send_plan") or {}).get("plan_id") or "")
        _require_ok(bool(stale_plan_id), "missing stale send plan before compact")

        compacted = client.compact(conversation_id)
        compacted_history = dict(compacted.get("compacted_history") or {})
        _require_ok(bool(list(compacted_history.get("replacement_history") or [])), "manual compact missing replacement_history")
        item_stream = dict(compacted.get("item_stream") or {})
        boundary_entries = [
            item
            for item in list(item_stream.get("entries") or [])
            if str((item or {}).get("kind") or "").strip().lower() == "compact_boundary"
        ]
        _require_ok(bool(boundary_entries), "manual compact missing compact_boundary item")

        streamed = client.send_stream(
            message="在 compact 之后，再帮我用一句话提醒什么是 canonical replay。",
            conversation_id=conversation_id,
            use_tools=False,
            send_plan_id=stale_plan_id,
        )
        _require_ok(bool(streamed["content"].strip()), "post-compact send with stale plan returned empty content")
        conversation_after = client.get_conversation(conversation_id)
        history_events = list(conversation_after.get("history_log", {}).get("events") or [])
        _require_ok(any(str(item.get("title") or "") == "manual_compact" for item in history_events), "history_log missing manual_compact")
        return {
            "conversation_id": conversation_id,
            "stale_plan_id": stale_plan_id,
            "replacement_history_count": len(list(compacted_history.get("replacement_history") or [])),
            "history_event_count": len(history_events),
            "answer_excerpt": _compact_text(streamed["content"]),
        }

    def case_live_mid_run_compaction_probe() -> Dict[str, Any]:
        title = f"[chat-eval][mid-run][{tag}] mid-run compact probe"
        conversation = client.create_conversation(title)
        conversation_id = int(conversation["id"])
        report["created_conversations"].append({"id": conversation_id, "title": title})
        probe_message = (
            "请把下面主题做成一个长篇、多阶段、强上下文依赖的回答："
            "比较 Codex、Claude 风格的 chat 上下文管理，并解释 thread、turn、item、tool ledger、canonical replay 的关系。"
            "如果系统判断可用工具有帮助，可以自行决定使用工具；如果没有工具，也请至少分多个阶段进行。"
            f" 这里是一段很长的背景材料，请纳入考虑：{_long_tail(90)}"
        )
        preview = client.preview(message=probe_message, conversation_id=conversation_id, use_tools=True)
        send_plan_id = str((preview.get("send_plan") or {}).get("plan_id") or "")
        streamed = client.send_stream(
            message=probe_message,
            conversation_id=conversation_id,
            use_tools=True,
            send_plan_id=send_plan_id or None,
        )
        _require_ok(bool(streamed["content"].strip()), "mid-run probe returned empty content")
        conversation_after = client.get_conversation(conversation_id)
        item_entries = list(conversation_after.get("item_stream", {}).get("entries") or [])
        history_events = list(conversation_after.get("history_log", {}).get("events") or [])
        snapshots = list(conversation_after.get("context_snapshots") or [])
        mid_run_boundaries = [
            item
            for item in item_entries
            if str((item or {}).get("kind") or "").strip().lower() == "compact_boundary"
            and str((item or {}).get("status") or "").strip().lower() == "mid_run"
        ]
        observed = bool(mid_run_boundaries) or any(str(item.get("title") or "") == "mid_run_compact" for item in history_events) or any(
            str((item or {}).get("mode") or "").strip().lower() == "mid_run"
            for item in snapshots
            if isinstance(item, dict)
        )
        if args.fail_on_live_mid_run_miss:
            _require_ok(observed, "live mid-run compact not observed under current runtime thresholds")
        return {
            "conversation_id": conversation_id,
            "preview_mode": preview.get("preview_mode"),
            "observed_mid_run_compact": observed,
            "mid_run_boundary_count": len(mid_run_boundaries),
            "history_log_titles": [item.get("title") for item in history_events],
            "snapshot_modes": [item.get("mode") for item in snapshots if isinstance(item, dict)],
            "event_counts": streamed["event_counts"],
            "answer_excerpt": _compact_text(streamed["content"]),
        }

    cases = [
        ("direct_preview_and_first_turn", case_direct_preview_and_first_turn),
        ("followup_long_dialog", case_followup_long_dialog),
        ("manual_compact_and_old_send_plan", case_manual_compact_and_old_send_plan),
        ("live_mid_run_compaction_probe", case_live_mid_run_compaction_probe),
    ]

    for case_name, case_fn in cases:
        result = run_case(case_name, case_fn)
        report["cases"].append(
            {
                "name": result.name,
                "ok": result.ok,
                "duration_seconds": result.duration_seconds,
                "details": result.details,
                "error": result.error,
            }
        )

    report["finished_at"] = _now_iso()
    report["all_ok"] = all(bool(item.get("ok")) for item in report["cases"])
    report["failed_cases"] = [item["name"] for item in report["cases"] if not item.get("ok")]

    report_path = args.report_path.strip()
    if not report_path:
        report_dir = Path("tmp/chat-eval")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = str(report_dir / f"chat-eval-report-{tag}.json")

    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport saved to {path}")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
