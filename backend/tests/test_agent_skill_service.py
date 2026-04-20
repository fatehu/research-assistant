import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_skill_service import AgentSkillService


def test_agent_skill_service_resolves_alias_explicitly_not_trigger_keywords(tmp_path):
    skill_dir = tmp_path / "demo-chat-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: demo-chat-skill
description: Demo workflow
allowed_channels:
  - chat
aliases:
  - 论文复现
trigger_keywords:
  - paper_id
  - run draft
---

# Demo Workflow

Use this skill for paper reproduction.
""",
        encoding="utf-8",
    )

    service = AgentSkillService(tmp_path)
    resolution = service.resolve("请帮我做论文复现，并基于 paper_id=12 生成 run draft。", channel="chat")

    assert len(resolution.available_skills) == 1
    assert len(resolution.active_skills) == 1
    assert resolution.active_skills[0].name == "demo-chat-skill"
    assert resolution.active_skills[0].activation_reason == "命中 skill 别名: 论文复现"
    assert resolution.enforced_tool_names == ()
    assert "Demo Workflow" in resolution.active_prompt
    assert resolution.active_prompt_tokens > 0


def test_agent_skill_service_activates_persisted_active_skill(tmp_path):
    skill_dir = tmp_path / "paper-reproduction"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: paper-reproduction
description: Paper workflow
allowed_channels:
  - chat
aliases:
  - 论文复现
trigger_keywords:
  - planning
---

# Paper Workflow
""",
        encoding="utf-8",
    )

    service = AgentSkillService(tmp_path)
    resolution = service.resolve(
        "继续",
        channel="chat",
        active_skill_names=["paper-reproduction"],
    )

    assert len(resolution.active_skills) == 1
    assert resolution.active_skills[0].name == "paper-reproduction"
    assert resolution.active_skills[0].activation_reason == "会话级已激活"


def test_agent_skill_service_respects_allowed_channels(tmp_path):
    skill_dir = tmp_path / "demo-chat-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: demo-chat-skill
description: Demo workflow
allowed_channels:
  - chat
aliases:
  - 论文复现
---

# Demo Workflow
""",
        encoding="utf-8",
    )

    service = AgentSkillService(tmp_path)
    resolution = service.resolve("请帮我做论文复现。", channel="codelab_agent")

    assert resolution.available_skills == ()
    assert resolution.active_skills == ()
    assert resolution.active_prompt == ""


def test_agent_skill_service_discovers_skills_from_current_working_tree(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".agents" / "skills" / "paper-reproduction"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: paper-reproduction
description: Intake workflow
allowed_channels:
  - chat
aliases:
  - intake plan
---

# Intake Workflow
""",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_SKILLS_ROOT", raising=False)
    service = AgentSkillService()
    resolution = service.resolve("请使用 intake plan。", channel="chat")

    assert service.skills_root == tmp_path / ".agents" / "skills"
    assert resolution.active_skills[0].name == "paper-reproduction"


def test_agent_skill_service_respects_env_override(tmp_path, monkeypatch):
    skill_dir = tmp_path / "custom-skills" / "paper-reproduction"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: paper-reproduction
description: Intake workflow
allowed_channels:
  - chat
aliases:
  - intake plan
---

# Intake Workflow
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(tmp_path / "custom-skills"))
    service = AgentSkillService()
    resolution = service.resolve("请使用 intake plan。", channel="chat")

    assert service.skills_root == Path(tmp_path / "custom-skills")
    assert resolution.active_skills[0].name == "paper-reproduction"


def test_agent_skill_service_exposes_enforced_tool_names(tmp_path):
    skill_dir = tmp_path / "paper-reproduction"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: paper-reproduction
description: Fixed planning workflow
allowed_channels:
  - chat
aliases:
  - intake plan
enforced_tool_names:
  - paper_research_status
  - paper_research_prepare
blocked_tool_names:
  - knowledge_search
---

# Intake Workflow
""",
        encoding="utf-8",
    )

    service = AgentSkillService(tmp_path)
    resolution = service.resolve("请使用 intake plan。", channel="chat")

    assert resolution.enforced_tool_names == ("paper_research_prepare", "paper_research_status")
    assert resolution.blocked_tool_names == ("knowledge_search",)
    assert resolution.active_skills[0].enforced_tool_names == (
        "paper_research_status",
        "paper_research_prepare",
    )


def test_agent_skill_service_loads_skill_yaml_and_renders_package_summary(tmp_path):
    skill_dir = tmp_path / "paper-reproduction"
    agents_dir = skill_dir / "agents"
    skill_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """
# Intake Workflow

Use this skill package.
""",
        encoding="utf-8",
    )
    (skill_dir / "skill.yaml").write_text(
        """name: paper-reproduction
description: Intake workflow
allowed_channels:
  - chat
aliases:
  - intake plan
enforced_tool_names:
  - paper_research_status
blocked_tool_names:
  - knowledge_search
continue_policies:
  - manual
  - ask_to_continue
  - auto_continue
default_continue_policy: ask_to_continue
scripts:
  - path: scripts/render_stage_prompt.py
when_to_use: Use when the user wants to continue the archived paper workflow.
interface_metadata_path: agents/openai.yaml
user_invocable: true
execution_context: inline
agent: primary
effort: high
stages:
  - id: planning
    continue_policy: manual
  - id: execution
    continue_policy: auto_continue
artifacts:
  - path: planning/paper_intake_result.json
  - path: drafts/run_drafts.json
""",
        encoding="utf-8",
    )
    (agents_dir / "openai.yaml").write_text(
        """interface:
  display_name: "Paper Workflow"
  short_description: "Plan, prepare, and run paper repro workflows"
  default_prompt: "Use $paper-reproduction to continue the next paper workflow stage."

policy:
  allow_implicit_invocation: true
""",
        encoding="utf-8",
    )

    service = AgentSkillService(tmp_path)
    resolution = service.resolve("请使用 intake plan。", channel="chat")

    assert resolution.active_skills[0].name == "paper-reproduction"
    assert resolution.enforced_tool_names == ("paper_research_status",)
    assert resolution.blocked_tool_names == ("knowledge_search",)
    assert "Stages: planning -> execution" in resolution.active_prompt
    assert "Stage policies: planning=manual, execution=auto_continue" in resolution.active_prompt
    assert "Key artifacts: planning/paper_intake_result.json, drafts/run_drafts.json" in resolution.active_prompt
    assert "Helper scripts: scripts/render_stage_prompt.py" in resolution.active_prompt
    assert "Continue policy: default=ask_to_continue; modes=manual, ask_to_continue, auto_continue" in resolution.active_prompt
    assert resolution.active_skills[0].config_path == "paper-reproduction/skill.yaml"
    assert resolution.active_skills[0].interface_path == "paper-reproduction/agents/openai.yaml"
    assert resolution.active_skills[0].display_name == "Paper Workflow"
    assert resolution.active_skills[0].short_description == "Plan, prepare, and run paper repro workflows"
    assert resolution.active_skills[0].default_prompt == "Use $paper-reproduction to continue the next paper workflow stage."
    assert resolution.active_skills[0].when_to_use == "Use when the user wants to continue the archived paper workflow."
    assert resolution.active_skills[0].user_invocable is True
    assert resolution.active_skills[0].execution_context == "inline"
    assert resolution.active_skills[0].agent == "primary"
    assert resolution.active_skills[0].effort == "high"
    assert resolution.active_skills[0].allow_implicit_invocation is True
    assert resolution.active_skills[0].stage_names == ("planning", "execution")
    assert resolution.active_skills[0].stage_policies == ("planning=manual", "execution=auto_continue")
    assert resolution.active_skills[0].continue_policies == ("manual", "ask_to_continue", "auto_continue")
    assert resolution.active_skills[0].default_continue_policy == "ask_to_continue"


def test_agent_skill_service_respects_openai_yaml_policy_for_implicit_invocation(tmp_path):
    skill_dir = tmp_path / "explicit-only-skill"
    agents_dir = skill_dir / "agents"
    agents_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """
# Explicit Skill
""",
        encoding="utf-8",
    )
    (skill_dir / "skill.yaml").write_text(
        """name: explicit-only-skill
description: Explicit only
allowed_channels:
  - chat
aliases:
  - 显式技能
trigger_keywords:
  - planning
""",
        encoding="utf-8",
    )
    (agents_dir / "openai.yaml").write_text(
        """interface:
  display_name: "Explicit Skill"
  short_description: "Explicit only skill"
  default_prompt: "Use $explicit-only-skill explicitly."

policy:
  allow_implicit_invocation: false
""",
        encoding="utf-8",
    )

    service = AgentSkillService(tmp_path)
    implicit_resolution = service.resolve("请继续 planning。", channel="chat")
    explicit_resolution = service.resolve("请使用 explicit-only-skill 继续 planning。", channel="chat")

    assert implicit_resolution.active_skills == ()
    assert len(explicit_resolution.active_skills) == 1
    assert explicit_resolution.active_skills[0].allow_implicit_invocation is False
    assert explicit_resolution.active_skills[0].interface_path == "explicit-only-skill/agents/openai.yaml"
