"""
Reader compose agent state helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ReaderComposeAgentState:
    snapshots: List[Dict[str, Any]] = field(default_factory=list)

    def push(self, payload: Dict[str, Any]) -> str:
        snapshot = json.loads(json.dumps(payload, ensure_ascii=False))
        snapshot_id = str(snapshot.get("plan_id") or f"snapshot_{len(self.snapshots) + 1}")
        snapshot["_snapshot_id"] = snapshot_id
        self.snapshots.append(snapshot)
        return snapshot_id

    def rollback_latest(self) -> Dict[str, Any]:
        if not self.snapshots:
            return {}
        return json.loads(json.dumps(self.snapshots[-1], ensure_ascii=False))

