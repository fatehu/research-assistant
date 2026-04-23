from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, Optional

from app.config import settings
from app.services.llm_service import LLMService

_README_REPRODUCTION_INTAKE_SYSTEM_PROMPT = """You are a README-to-reproduction-intake engine.

Return STRICT JSON only. Do not include Markdown fences, commentary, or prose outside the JSON object.

Your job is to read a repository README and extract only the reproduction information that is explicitly supported by the README text itself.

Rules:
- Use the README as the primary source of truth.
- Do not invent commands, environment requirements, datasets, paths, outputs, or evaluation steps.
- If the README is ambiguous, preserve the ambiguity in `blocking_questions` instead of guessing.
- Prefer literal command strings when the README shows runnable commands.
- Prefer repo-relative file hints when the README explicitly names scripts, notebooks, configs, or directories.
- Keep evidence snippets short and directly tied to the README.
- If a category is not present, use [] or null.

Required JSON shape:
{
  "schema_version": "repo_readme_reproduction_intake_v1",
  "readme_relative_path": string|null,
  "repo_url": string|null,
  "reproduction_goal": string|null,
  "environment_requirements": {
    "languages": [string],
    "package_managers": [string],
    "system_dependencies": [string],
    "python_version": string|null,
    "hardware_hints": [string],
    "notes": [string]
  },
  "installation_steps": [
    {
      "order": integer,
      "command": string|null,
      "notes": string|null,
      "evidence_text": string|null
    }
  ],
  "run_commands": [
    {
      "label": string|null,
      "command": string,
      "purpose": string|null,
      "entrypoint_path_or_hint": string|null,
      "evidence_text": string|null
    }
  ],
  "entrypoints": [
    {
      "path_or_hint": string,
      "kind": "script"|"notebook"|"config"|"directory"|"command"|"unknown",
      "purpose": string|null,
      "evidence_text": string|null
    }
  ],
  "dataset_materials": [
    {
      "name": string|null,
      "source": string|null,
      "how_to_get": string|null,
      "required": boolean|null,
      "evidence_text": string|null
    }
  ],
  "evaluation_steps": [
    {
      "label": string|null,
      "command": string|null,
      "notes": string|null,
      "evidence_text": string|null
    }
  ],
  "expected_outputs": [
    {
      "name": string|null,
      "path_or_hint": string|null,
      "notes": string|null,
      "evidence_text": string|null
    }
  ],
  "focus_files": [string],
  "focus_directories": [string],
  "blocking_questions": [string],
  "evidence_snippets": [
    {
      "topic": string,
      "text": string
    }
  ]
}
"""


class RepoReadmeReproductionIntakeService:
    """Extract reproduction guidance from a full README using the configured LLM."""

    output_max_tokens = 4096
    timeout_seconds = 180

    async def generate(
        self,
        *,
        repo_url: Optional[str],
        readme_relative_path: str,
        readme_text: str,
    ) -> Dict[str, Any]:
        payload = {
            "repo_url": str(repo_url or "").strip() or None,
            "readme_relative_path": str(readme_relative_path or "").strip() or None,
            "readme_char_count": len(str(readme_text or "")),
            "readme_markdown": str(readme_text or ""),
        }
        messages = [
            {
                "role": "user",
                "content": (
                    "Extract the README reproduction intake JSON from this payload.\n"
                    "Use the full README text. Return JSON only.\n\n"
                    f"{json.dumps(payload, ensure_ascii=False)}"
                ),
            }
        ]
        llm = LLMService()
        response = await asyncio.wait_for(
            llm.chat(
                messages=messages,
                system_prompt=_README_REPRODUCTION_INTAKE_SYSTEM_PROMPT,
                temperature=0.0,
                max_tokens=max(int(getattr(settings, "llm_max_tokens", 4096) or 4096), self.output_max_tokens),
                source="repo_readme_reproduction_intake",
            ),
            timeout=self.timeout_seconds,
        )
        finish_reason = str(response.get("finish_reason") or "").strip().lower()
        if finish_reason == "length":
            raise ValueError("readme reproduction intake response was truncated by max_tokens before complete JSON")
        parsed = self._parse_json_object(str(response.get("content") or ""))
        if not parsed:
            raise ValueError("readme reproduction intake response is empty")
        parsed.setdefault("schema_version", "repo_readme_reproduction_intake_v1")
        parsed.setdefault("readme_relative_path", str(readme_relative_path or "").strip() or None)
        parsed.setdefault("repo_url", str(repo_url or "").strip() or None)
        return parsed

    @staticmethod
    def _parse_json_object(raw_text: str) -> Dict[str, Any]:
        text = str(raw_text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("readme reproduction intake response is not a JSON object")
        return parsed
