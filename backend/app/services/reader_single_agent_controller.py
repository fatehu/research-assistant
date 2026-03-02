from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from app.services.reader_single_agent_prompts import (
    PIPELINE_VERSION,
    build_first_turn_prompt,
    build_iterative_turn_prompt,
    build_step_result_digest,
)
from app.services.reader_single_agent_validator import HARD_GATES, ReaderSingleAgentValidator

ModelInferFn = Callable[[str, Dict[str, Any], int, str], Awaitable[Dict[str, Any]]]


class ReaderSingleAgentController:
    def __init__(
        self,
        *,
        validator: Optional[ReaderSingleAgentValidator] = None,
        max_steps: int = 12,
        max_repair_rounds: int = 2,
    ) -> None:
        self.validator = validator or ReaderSingleAgentValidator()
        self.max_steps = max(1, int(max_steps))
        self.max_repair_rounds = max(0, int(max_repair_rounds))

    async def run(
        self,
        *,
        page_meta: Mapping[str, Any],
        docmind_blocks: Sequence[Mapping[str, Any]],
        rendered_page_image: str,
        component_whitelist: Sequence[str],
        model_infer: Optional[ModelInferFn],
    ) -> Dict[str, Any]:
        step = 1
        remaining_repairs = int(self.max_repair_rounds)
        repair_rounds_used = 0
        previous_step_result: Dict[str, Any] = {}
        previous_validation: Dict[str, Any] = {}
        ownership_baseline: Dict[str, str] = {}
        step_metrics: List[Dict[str, Any]] = []
        all_fixes: List[Dict[str, Any]] = []

        if not callable(model_infer):
            return self._finalize_fallback(
                reason="model_unavailable",
                docmind_blocks=docmind_blocks,
                component_whitelist=component_whitelist,
                ownership_baseline=ownership_baseline,
                repair_rounds_used=repair_rounds_used,
                step_metrics=step_metrics,
                all_fixes=all_fixes,
            )

        while step <= self.max_steps:
            is_first_step = step == 1
            phase = "first" if is_first_step else "repair"
            if is_first_step:
                prompt_bundle = build_first_turn_prompt(
                    page_meta=page_meta,
                    docmind_blocks=[dict(row) for row in list(docmind_blocks or []) if isinstance(row, Mapping)],
                    rendered_page_image=str(rendered_page_image or ""),
                    component_whitelist=[str(item).strip() for item in list(component_whitelist or []) if str(item).strip()],
                    max_steps=self.max_steps,
                    max_repair_rounds=self.max_repair_rounds,
                )
            else:
                must_fix = [
                    gate_name
                    for gate_name in HARD_GATES
                    if not bool((previous_validation.get("gates") or {}).get(gate_name, {}).get("passed"))
                ]
                do_not_change = [
                    gate_name
                    for gate_name in HARD_GATES
                    if bool((previous_validation.get("gates") or {}).get(gate_name, {}).get("passed"))
                ]
                prompt_bundle = build_iterative_turn_prompt(
                    current_step=step,
                    remaining_repair_rounds=remaining_repairs,
                    previous_step_result_digest=build_step_result_digest(previous_step_result),
                    validator_result=previous_validation,
                    must_fix=must_fix,
                    do_not_change=do_not_change,
                    component_whitelist=[str(item).strip() for item in list(component_whitelist or []) if str(item).strip()],
                    max_steps=self.max_steps,
                )

            started_at = time.perf_counter()
            model_output: Dict[str, Any] = {}
            model_error = ""
            try:
                model_output = await model_infer(
                    str(prompt_bundle.get("system_prompt") or ""),
                    dict(prompt_bundle.get("user_prompt") or {}),
                    int(step),
                    phase,
                )
            except Exception as exc:  # pragma: no cover - caller/network errors are expected in production
                model_error = str(exc)
                model_output = {}

            latency_ms = int((time.perf_counter() - started_at) * 1000)
            usage = dict(model_output.get("usage") or {}) if isinstance(model_output, dict) else {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            model_status = str(model_output.get("status") or "").strip().lower() if isinstance(model_output, dict) else ""

            step_result = dict(model_output.get("step_result") or {}) if isinstance(model_output, dict) else {}
            model_failed = not bool(step_result)
            if model_failed:
                reason = model_error or "model_unavailable"
                step_metrics.append(
                    {
                        "step_index": int(step),
                        "phase": phase,
                        "latency_ms": int(latency_ms),
                        "prompt_tokens": int(prompt_tokens),
                        "completion_tokens": int(completion_tokens),
                        "total_tokens": int(total_tokens),
                        "failed_gates": ["model_unavailable"],
                        "model_status": model_status,
                        "model_error": reason,
                    }
                )
                return self._finalize_fallback(
                    reason=reason,
                    docmind_blocks=docmind_blocks,
                    component_whitelist=component_whitelist,
                    ownership_baseline=ownership_baseline,
                    repair_rounds_used=repair_rounds_used,
                    step_metrics=step_metrics,
                    all_fixes=all_fixes,
                )
            validation = self.validator.validate(
                step_result=step_result,
                docmind_blocks=docmind_blocks,
                component_whitelist=component_whitelist,
                previous_ownership=ownership_baseline or None,
            )

            if not ownership_baseline:
                ownership_baseline = dict(validation.get("ownership_map") or {})

            repair_result = self.validator.deterministic_repair(
                step_result=step_result,
                docmind_blocks=docmind_blocks,
                component_whitelist=component_whitelist,
                previous_ownership=ownership_baseline or None,
            )
            repaired_step_result = dict(repair_result.get("step_result") or {})
            fixes_applied = [
                row
                for row in list(repair_result.get("fixes_applied") or [])
                if isinstance(row, dict)
            ]
            all_fixes.extend(fixes_applied)

            revalidation = self.validator.validate(
                step_result=repaired_step_result,
                docmind_blocks=docmind_blocks,
                component_whitelist=component_whitelist,
                previous_ownership=ownership_baseline or None,
            )

            failed_gates = [
                gate_name
                for gate_name in HARD_GATES
                if not bool((revalidation.get("gates") or {}).get(gate_name, {}).get("passed"))
            ]

            step_metrics.append(
                {
                    "step_index": int(step),
                    "phase": phase,
                    "latency_ms": int(latency_ms),
                    "prompt_tokens": int(prompt_tokens),
                    "completion_tokens": int(completion_tokens),
                    "total_tokens": int(total_tokens),
                    "failed_gates": failed_gates,
                    "model_status": model_status,
                    "model_error": model_error,
                }
            )

            logger.info(
                "[ReaderSingleAgentV2] step={} phase={} status={} latency_ms={} prompt_tokens={} "
                "completion_tokens={} total_tokens={} failed_gates={} model_error={}",
                int(step),
                phase,
                model_status,
                int(latency_ms),
                int(prompt_tokens),
                int(completion_tokens),
                int(total_tokens),
                ",".join(failed_gates),
                model_error,
            )

            ai_declares_done = model_status == "done"
            if bool(revalidation.get("passed")) and ai_declares_done:
                return {
                    "status": "done",
                    "degraded_reason": "",
                    "pipeline_version": PIPELINE_VERSION,
                    "step_result": repaired_step_result,
                    "validation_report": revalidation,
                    "repair_report": {
                        "max_steps": int(self.max_steps),
                        "max_repair_rounds": int(self.max_repair_rounds),
                        "repair_rounds_used": int(repair_rounds_used),
                        "steps_executed": int(step),
                        "step_metrics": step_metrics,
                        "fixes_applied": all_fixes,
                    },
                }

            previous_step_result = repaired_step_result
            previous_validation = revalidation

            if step >= self.max_steps:
                reason = "ai_not_done" if bool(revalidation.get("passed")) and not ai_declares_done else "max_steps_exhausted"
                return self._finalize_fallback(
                    reason=reason,
                    docmind_blocks=docmind_blocks,
                    component_whitelist=component_whitelist,
                    ownership_baseline=ownership_baseline,
                    repair_rounds_used=repair_rounds_used,
                    step_metrics=step_metrics,
                    all_fixes=all_fixes,
                )

            if remaining_repairs <= 0:
                reason = "ai_not_done" if bool(revalidation.get("passed")) and not ai_declares_done else "repair_rounds_exhausted"
                return self._finalize_fallback(
                    reason=reason,
                    docmind_blocks=docmind_blocks,
                    component_whitelist=component_whitelist,
                    ownership_baseline=ownership_baseline,
                    repair_rounds_used=repair_rounds_used,
                    step_metrics=step_metrics,
                    all_fixes=all_fixes,
                )

            remaining_repairs -= 1
            repair_rounds_used += 1
            step += 1

        return self._finalize_fallback(
            reason="max_steps_exhausted",
            docmind_blocks=docmind_blocks,
            component_whitelist=component_whitelist,
            ownership_baseline=ownership_baseline,
            repair_rounds_used=repair_rounds_used,
            step_metrics=step_metrics,
            all_fixes=all_fixes,
        )

    def _finalize_fallback(
        self,
        *,
        reason: str,
        docmind_blocks: Sequence[Mapping[str, Any]],
        component_whitelist: Sequence[str],
        ownership_baseline: Mapping[str, str],
        repair_rounds_used: int,
        step_metrics: List[Dict[str, Any]],
        all_fixes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        baseline_step_result = self.validator.build_deterministic_baseline_step_result(
            docmind_blocks=docmind_blocks,
            component_whitelist=component_whitelist,
            previous_ownership=ownership_baseline or None,
        )
        baseline_validation = self.validator.validate(
            step_result=baseline_step_result,
            docmind_blocks=docmind_blocks,
            component_whitelist=component_whitelist,
            previous_ownership=ownership_baseline or None,
        )

        return {
            "status": "fallback",
            "degraded_reason": str(reason or "validator_non_converged"),
            "pipeline_version": PIPELINE_VERSION,
            "step_result": baseline_step_result,
            "validation_report": baseline_validation,
            "repair_report": {
                "max_steps": int(self.max_steps),
                "max_repair_rounds": int(self.max_repair_rounds),
                "repair_rounds_used": int(repair_rounds_used),
                "steps_executed": int(len(step_metrics)),
                "step_metrics": step_metrics,
                "fixes_applied": all_fixes,
            },
        }


async def parse_json_dict_from_model_text(raw_text: str) -> Dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}

