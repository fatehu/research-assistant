import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.reader_single_agent_prompts import (
    build_first_turn_prompt,
    build_iterative_turn_prompt,
)


def test_first_turn_prompt_template_contains_required_contract():
    prompt = build_first_turn_prompt(
        page_meta={"paper_id": 7, "page": 1},
        docmind_blocks=[{"layout_id": "l1", "source_text": "Intro"}],
        rendered_page_image="data:image/jpeg;base64,abc",
        component_whitelist=["ParagraphProse", "SectionHeading"],
    )

    assert isinstance(prompt, dict)
    assert "ReaderAgent-Orchestrator" in str(prompt.get("system_prompt") or "")
    payload = dict(prompt.get("user_prompt") or {})
    assert payload.get("run_config", {}).get("pipeline_version") == "single_agent_v2"
    assert payload.get("run_config", {}).get("step") == 1
    assert payload.get("inputs", {}).get("rendered_page_image") == "data:image/jpeg;base64,abc"
    assert payload.get("inputs", {}).get("component_whitelist") == ["ParagraphProse", "SectionHeading"]
    assert "required_output_schema" in payload
    rules = [str(item) for item in list(payload.get("rules") or [])]
    assert any("Every component must include zone_type, column_id, region, display, order_key" in item for item in rules)
    assert any("contiguous prose statements" in item for item in rules)


def test_iterative_turn_prompt_template_contains_delta_contract():
    prompt = build_iterative_turn_prompt(
        current_step=3,
        remaining_repair_rounds=1,
        previous_step_result_digest={"classification": []},
        validator_result={"passed": False},
        must_fix=["full_coverage", "id_integrity"],
        do_not_change=["whitelist_only"],
        component_whitelist=["ParagraphProse"],
    )

    assert isinstance(prompt, dict)
    assert "repair mode" in str(prompt.get("system_prompt") or "").lower()
    payload = dict(prompt.get("user_prompt") or {})
    assert payload.get("run_config", {}).get("step") == 3
    assert payload.get("run_config", {}).get("remaining_repair_rounds") == 1
    assert payload.get("inputs", {}).get("must_fix") == ["full_coverage", "id_integrity"]
    assert payload.get("inputs", {}).get("do_not_change") == ["whitelist_only"]
    assert "required_output_schema" in payload
    rules = [str(item) for item in list(payload.get("rules") or [])]
    assert any("mandatory layout fields" in item for item in rules)

