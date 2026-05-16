from __future__ import annotations

import importlib.util
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.services.smart_chunking.token_utils import estimate_tokens


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    path: str
    prompt_text: str
    config_path: str = ""
    interface_path: str = ""
    session_system_prompt: str = ""
    aliases: Tuple[str, ...] = ()
    trigger_keywords: Tuple[str, ...] = ()
    allowed_channels: Tuple[str, ...] = ()
    enforced_tool_names: Tuple[str, ...] = ()
    blocked_tool_names: Tuple[str, ...] = ()
    display_name: str = ""
    short_description: str = ""
    default_prompt: str = ""
    when_to_use: str = ""
    user_invocable: bool = True
    execution_context: str = ""
    agent: str = ""
    effort: str = ""
    allow_implicit_invocation: bool = True
    scripts: Tuple[str, ...] = ()
    stage_names: Tuple[str, ...] = ()
    stage_policies: Tuple[str, ...] = ()
    artifact_paths: Tuple[str, ...] = ()
    continue_policies: Tuple[str, ...] = ()
    default_continue_policy: str = ""


@dataclass(frozen=True)
class AgentSkillMatch:
    name: str
    description: str
    path: str
    config_path: str = ""
    interface_path: str = ""
    session_system_prompt: str = ""
    score: int = 0
    activation_reason: str = ""
    display_name: str = ""
    short_description: str = ""
    default_prompt: str = ""
    when_to_use: str = ""
    user_invocable: bool = True
    execution_context: str = ""
    agent: str = ""
    effort: str = ""
    allow_implicit_invocation: bool = True
    enforced_tool_names: Tuple[str, ...] = ()
    blocked_tool_names: Tuple[str, ...] = ()
    scripts: Tuple[str, ...] = ()
    stage_names: Tuple[str, ...] = ()
    stage_policies: Tuple[str, ...] = ()
    artifact_paths: Tuple[str, ...] = ()
    continue_policies: Tuple[str, ...] = ()
    default_continue_policy: str = ""


@dataclass(frozen=True)
class AgentSkillResolution:
    channel: str
    latest_user_text: str
    available_skills: Tuple[AgentSkillMatch, ...] = ()
    active_skills: Tuple[AgentSkillMatch, ...] = ()
    active_prompt: str = ""
    active_prompt_tokens: int = 0
    active_system_prompt: str = ""
    active_system_prompt_tokens: int = 0
    enforced_tool_names: Tuple[str, ...] = ()
    blocked_tool_names: Tuple[str, ...] = ()


class AgentSkillService:
    def __init__(self, skills_root: Optional[Path] = None):
        self.skills_root = self._resolve_skills_root(skills_root)

    def get_skill(self, skill_name: str) -> Optional[AgentSkill]:
        normalized_query = self._normalize_text(skill_name)
        if not normalized_query:
            return None
        for skill in self._load_skills():
            if self._normalize_text(skill.name) == normalized_query:
                return skill
            for alias in skill.aliases:
                if self._normalize_text(alias) == normalized_query:
                    return skill
        return None

    def render_launch_prompt(self, skill_name: str, payload: Dict[str, Any]) -> str:
        skill = self.get_skill(skill_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_name}")
        if str(payload.get("stage") or "").strip():
            rendered = self._render_stage_skill_launch(skill, payload)
            if rendered:
                return rendered
        default_prompt = str(skill.default_prompt or "").strip()
        if default_prompt:
            return default_prompt
        raise ValueError(f"skill does not provide a launch renderer: {skill.name}")

    def resolve(
        self,
        latest_user_text: str,
        *,
        channel: str = "chat",
        active_skill_names: Optional[Sequence[str]] = None,
    ) -> AgentSkillResolution:
        normalized_channel = str(channel or "chat").strip().lower() or "chat"
        normalized_text = self._normalize_text(latest_user_text)
        normalized_active_skill_names = {
            self._normalize_text(str(item or ""))
            for item in list(active_skill_names or [])
            if self._normalize_text(str(item or ""))
        }
        skills = [skill for skill in self._load_skills() if self._skill_allows_channel(skill, normalized_channel)]
        available_matches: List[AgentSkillMatch] = []
        active_prompt_parts: List[str] = []
        active_system_prompt_parts: List[str] = []
        active_matches: List[AgentSkillMatch] = []
        active_enforced_tool_sets: List[set[str]] = []
        active_blocked_tool_names: set[str] = set()

        for skill in skills:
            score, activation_reason = self._score_skill(skill, normalized_text)
            match = AgentSkillMatch(
                name=skill.name,
                description=skill.description,
                path=skill.path,
                config_path=skill.config_path,
                interface_path=skill.interface_path,
                session_system_prompt=skill.session_system_prompt,
                score=score,
                activation_reason=activation_reason,
                display_name=skill.display_name,
                short_description=skill.short_description,
                default_prompt=skill.default_prompt,
                when_to_use=skill.when_to_use,
                user_invocable=skill.user_invocable,
                execution_context=skill.execution_context,
                agent=skill.agent,
                effort=skill.effort,
                allow_implicit_invocation=skill.allow_implicit_invocation,
                enforced_tool_names=tuple(skill.enforced_tool_names),
                blocked_tool_names=tuple(skill.blocked_tool_names),
                scripts=tuple(skill.scripts),
                stage_names=tuple(skill.stage_names),
                stage_policies=tuple(skill.stage_policies),
                artifact_paths=tuple(skill.artifact_paths),
                continue_policies=tuple(skill.continue_policies),
                default_continue_policy=str(skill.default_continue_policy or ""),
            )
            available_matches.append(match)
            normalized_skill_name = self._normalize_text(skill.name)
            implicit_activation = activation_reason.startswith("命中 skill 触发词")
            explicit_activation = bool(score >= 90 and (skill.allow_implicit_invocation or not implicit_activation))
            persisted_activation = normalized_skill_name in normalized_active_skill_names
            if explicit_activation or persisted_activation:
                effective_reason = activation_reason or ("会话级已激活" if persisted_activation else "")
                match = AgentSkillMatch(
                    **{
                        **match.__dict__,
                        "activation_reason": effective_reason,
                    }
                )
                active_matches.append(match)
                active_prompt_parts.append(self._render_skill_prompt(skill))
                if skill.session_system_prompt.strip():
                    active_system_prompt_parts.append(skill.session_system_prompt.strip())
                if skill.enforced_tool_names:
                    active_enforced_tool_sets.append(
                        {str(item).strip() for item in skill.enforced_tool_names if str(item).strip()}
                    )
                active_blocked_tool_names.update(
                    str(item).strip() for item in skill.blocked_tool_names if str(item).strip()
                )

        available_matches.sort(key=lambda item: (-item.score, item.name))
        active_matches.sort(key=lambda item: (-item.score, item.name))
        active_prompt = "\n\n".join(part for part in active_prompt_parts if part.strip()).strip()
        active_system_prompt = "\n\n".join(part for part in active_system_prompt_parts if part.strip()).strip()
        enforced_tool_names: Tuple[str, ...] = ()
        if active_enforced_tool_sets:
            active_intersection = set.intersection(*active_enforced_tool_sets)
            if active_blocked_tool_names:
                active_intersection -= active_blocked_tool_names
            enforced_tool_names = tuple(sorted(active_intersection))
        blocked_tool_names = tuple(sorted(active_blocked_tool_names))
        return AgentSkillResolution(
            channel=normalized_channel,
            latest_user_text=str(latest_user_text or ""),
            available_skills=tuple(available_matches),
            active_skills=tuple(active_matches),
            active_prompt=active_prompt,
            active_prompt_tokens=estimate_tokens(active_prompt) if active_prompt else 0,
            active_system_prompt=active_system_prompt,
            active_system_prompt_tokens=estimate_tokens(active_system_prompt) if active_system_prompt else 0,
            enforced_tool_names=enforced_tool_names,
            blocked_tool_names=blocked_tool_names,
        )

    def _load_skills(self) -> List[AgentSkill]:
        if not self.skills_root.exists():
            return []
        skills: List[AgentSkill] = []
        for skill_file in sorted(self.skills_root.glob("*/SKILL.md")):
            try:
                skill = self._parse_skill_file(skill_file)
            except Exception:
                continue
            if skill is not None:
                skills.append(skill)
        return skills

    def _parse_skill_file(self, skill_file: Path) -> Optional[AgentSkill]:
        raw = skill_file.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        markdown_metadata, body = self._split_frontmatter(raw)
        config_file, config_metadata = self._read_skill_config(skill_file.parent)
        interface_file, interface_metadata = self._read_skill_interface_config(
            skill_file.parent,
            config_metadata.get("interface_metadata_path") or markdown_metadata.get("interface_metadata_path"),
        )
        metadata = self._merge_skill_metadata(markdown_metadata, config_metadata)
        interface_block = interface_metadata.get("interface") if isinstance(interface_metadata, dict) else {}
        policy_block = interface_metadata.get("policy") if isinstance(interface_metadata, dict) else {}
        interface_block = interface_block if isinstance(interface_block, dict) else {}
        policy_block = policy_block if isinstance(policy_block, dict) else {}
        name = str(metadata.get("name") or skill_file.parent.name).strip()
        if not name:
            return None
        description = str(metadata.get("description") or "").strip()
        session_system_prompt = str(
            metadata.get("session_system_prompt")
            or metadata.get("active_system_prompt")
            or metadata.get("system_prompt")
            or ""
        ).strip()
        aliases = self._normalize_list(metadata.get("aliases"))
        trigger_keywords = self._normalize_list(metadata.get("trigger_keywords"))
        allowed_channels = self._normalize_list(
            metadata.get("allowed_channels") or metadata.get("channels")
        )
        enforced_tool_names = self._normalize_list(
            metadata.get("enforced_tool_names") or metadata.get("allowed_tools")
        )
        blocked_tool_names = self._normalize_list(
            metadata.get("blocked_tool_names") or metadata.get("blocked_tools")
        )
        scripts = self._normalize_skill_scripts(metadata.get("scripts"))
        stage_names = self._normalize_skill_stage_names(metadata.get("stages"))
        stage_policies = self._normalize_skill_stage_policies(metadata.get("stages"))
        artifact_paths = self._normalize_skill_artifacts(metadata.get("artifacts"))
        continue_policies = self._normalize_skill_continue_policies(metadata.get("continue_policies"))
        default_continue_policy = str(metadata.get("default_continue_policy") or "").strip()
        when_to_use = str(metadata.get("when_to_use") or "").strip()
        user_invocable = self._normalize_bool(
            metadata.get("user_invocable", metadata.get("user-invocable")),
            default=True,
        )
        execution_context = str(
            metadata.get("execution_context")
            or metadata.get("context")
            or ""
        ).strip()
        agent = str(metadata.get("agent") or "").strip()
        effort = str(metadata.get("effort") or "").strip()
        display_name = str(interface_block.get("display_name") or "").strip()
        short_description = str(interface_block.get("short_description") or "").strip()
        default_prompt = str(interface_block.get("default_prompt") or "").strip()
        allow_implicit_invocation = self._normalize_bool(
            policy_block.get("allow_implicit_invocation"),
            default=True,
        )
        return AgentSkill(
            name=name,
            description=description,
            path=self._relative_skill_path(skill_file),
            prompt_text=body.strip(),
            config_path=self._relative_skill_path(config_file) if config_file is not None else "",
            interface_path=self._relative_skill_path(interface_file) if interface_file is not None else "",
            session_system_prompt=session_system_prompt,
            aliases=tuple(aliases),
            trigger_keywords=tuple(trigger_keywords),
            allowed_channels=tuple(allowed_channels),
            enforced_tool_names=tuple(enforced_tool_names),
            blocked_tool_names=tuple(blocked_tool_names),
            display_name=display_name,
            short_description=short_description,
            default_prompt=default_prompt,
            when_to_use=when_to_use,
            user_invocable=user_invocable,
            execution_context=execution_context,
            agent=agent,
            effort=effort,
            allow_implicit_invocation=allow_implicit_invocation,
            scripts=tuple(scripts),
            stage_names=tuple(stage_names),
            stage_policies=tuple(stage_policies),
            artifact_paths=tuple(artifact_paths),
            continue_policies=tuple(continue_policies),
            default_continue_policy=default_continue_policy,
        )

    @staticmethod
    def _split_frontmatter(raw: str) -> Tuple[Dict[str, Any], str]:
        if not raw.startswith("---\n"):
            return {}, raw
        marker = "\n---\n"
        end_index = raw.find(marker, 4)
        if end_index < 0:
            return {}, raw
        frontmatter = raw[4:end_index]
        body = raw[end_index + len(marker) :]
        parsed = yaml.safe_load(frontmatter) or {}
        return parsed if isinstance(parsed, dict) else {}, body

    @staticmethod
    def _normalize_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        normalized: List[str] = []
        for item in value if isinstance(value, Sequence) else []:
            text = str(item or "").strip()
            if text:
                normalized.append(text)
        return normalized

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(str(value or "").lower().split())

    @classmethod
    def _read_skill_config(cls, skill_dir: Path) -> Tuple[Optional[Path], Dict[str, Any]]:
        for filename in ("skill.yaml", "skill.yml"):
            candidate = skill_dir / filename
            if not candidate.is_file():
                continue
            parsed = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if isinstance(parsed, dict):
                return candidate, parsed
        return None, {}

    @classmethod
    def _read_skill_interface_config(
        cls,
        skill_dir: Path,
        relative_path_hint: Any = None,
    ) -> Tuple[Optional[Path], Dict[str, Any]]:
        candidate_paths: List[Path] = []
        hint = str(relative_path_hint or "").strip()
        if hint:
            candidate_paths.append(skill_dir / hint)
        candidate_paths.append(skill_dir / "agents" / "openai.yaml")
        candidate_paths.append(skill_dir / "agents" / "openai.yml")

        seen: set[str] = set()
        for candidate in candidate_paths:
            candidate = candidate.resolve()
            key = str(candidate)
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            parsed = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if isinstance(parsed, dict):
                return candidate, parsed
        return None, {}

    @staticmethod
    def _merge_skill_metadata(markdown_metadata: Dict[str, Any], config_metadata: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(markdown_metadata or {})
        for key, value in dict(config_metadata or {}).items():
            merged[key] = value
        return merged

    @staticmethod
    def _normalize_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    @classmethod
    def _normalize_skill_scripts(cls, value: Any) -> List[str]:
        if value is None:
            return []
        entries = value if isinstance(value, Sequence) and not isinstance(value, str) else [value]
        normalized: List[str] = []
        for item in entries:
            if isinstance(item, dict):
                candidate = item.get("path") or item.get("name")
            else:
                candidate = item
            text = str(candidate or "").strip()
            if text:
                normalized.append(text)
        return normalized

    @classmethod
    def _normalize_skill_stage_names(cls, value: Any) -> List[str]:
        if value is None:
            return []
        entries = value if isinstance(value, Sequence) and not isinstance(value, str) else [value]
        normalized: List[str] = []
        for item in entries:
            if isinstance(item, dict):
                candidate = item.get("id") or item.get("name") or item.get("title")
            else:
                candidate = item
            text = str(candidate or "").strip()
            if text:
                normalized.append(text)
        return normalized

    @classmethod
    def _normalize_skill_artifacts(cls, value: Any) -> List[str]:
        if value is None:
            return []
        entries = value if isinstance(value, Sequence) and not isinstance(value, str) else [value]
        normalized: List[str] = []
        for item in entries:
            if isinstance(item, dict):
                candidate = item.get("path") or item.get("relative_path") or item.get("name")
            else:
                candidate = item
            text = str(candidate or "").strip()
            if text:
                normalized.append(text)
        return normalized

    @classmethod
    def _normalize_skill_stage_policies(cls, value: Any) -> List[str]:
        if value is None:
            return []
        entries = value if isinstance(value, Sequence) and not isinstance(value, str) else [value]
        normalized: List[str] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            stage_name = str(item.get("id") or item.get("name") or item.get("title") or "").strip()
            continue_policy = str(item.get("continue_policy") or "").strip()
            if stage_name and continue_policy:
                normalized.append(f"{stage_name}={continue_policy}")
        return normalized

    @classmethod
    def _normalize_skill_continue_policies(cls, value: Any) -> List[str]:
        if value is None:
            return []
        entries = value if isinstance(value, Sequence) and not isinstance(value, str) else [value]
        normalized: List[str] = []
        for item in entries:
            if isinstance(item, dict):
                candidate = item.get("id") or item.get("name") or item.get("mode")
            else:
                candidate = item
            text = str(candidate or "").strip()
            if text:
                normalized.append(text)
        return normalized

    def _relative_skill_path(self, skill_file: Path) -> str:
        try:
            return str(skill_file.resolve().relative_to(self.skills_root.resolve()))
        except ValueError:
            pass
        for candidate_root in AgentSkillService._skill_search_roots():
            try:
                return str(skill_file.resolve().relative_to(candidate_root.resolve()))
            except ValueError:
                continue
        return str(skill_file)

    @staticmethod
    def _skill_allows_channel(skill: AgentSkill, channel: str) -> bool:
        if not skill.allowed_channels:
            return True
        allowed = {item.strip().lower() for item in skill.allowed_channels if item.strip()}
        return channel in allowed

    def _score_skill(self, skill: AgentSkill, normalized_text: str) -> Tuple[int, str]:
        if not normalized_text:
            return 0, ""
        skill_name = self._normalize_text(skill.name)
        if skill_name and skill_name in normalized_text:
            return 100, f"命中 skill 名称: {skill.name}"
        for alias in skill.aliases:
            normalized_alias = self._normalize_text(alias)
            if normalized_alias and normalized_alias in normalized_text:
                return 90, f"命中 skill 别名: {alias}"
        for keyword in skill.trigger_keywords:
            normalized_keyword = self._normalize_text(keyword)
            if normalized_keyword and normalized_keyword in normalized_text:
                return 90, f"命中 skill 触发词: {keyword}"
        return 0, ""

    @staticmethod
    def _render_skill_prompt(skill: AgentSkill) -> str:
        lines = [
            f"### {skill.name}",
        ]
        if skill.description:
            lines.append(skill.description)
        if skill.stage_names:
            lines.append(f"Stages: {' -> '.join(skill.stage_names)}")
        if skill.stage_policies:
            lines.append(f"Stage policies: {', '.join(skill.stage_policies)}")
        if skill.artifact_paths:
            lines.append(f"Key artifacts: {', '.join(skill.artifact_paths)}")
        if skill.scripts:
            lines.append(f"Helper scripts: {', '.join(skill.scripts)}")
        if skill.default_continue_policy or skill.continue_policies:
            active = skill.default_continue_policy or "unspecified"
            modes = ", ".join(skill.continue_policies) if skill.continue_policies else active
            lines.append(f"Continue policy: default={active}; modes={modes}")
        lines.append(skill.prompt_text.strip())
        return "\n".join(line for line in lines if line.strip()).strip()

    def _render_stage_skill_launch(self, skill: AgentSkill, payload: Dict[str, Any]) -> str:
        stage = str(payload.get("stage") or "").strip()
        if not stage:
            raise ValueError("skill launch missing stage")
        paper_id = self._positive_int_or_none(payload.get("paper_id"))
        project_id_raw = payload.get("project_id")
        project_id = self._positive_int_or_none(project_id_raw)
        goal = str(payload.get("goal") or "").strip() or None
        preferred_draft_id = str(payload.get("preferred_draft_id") or "").strip() or None
        script_rel = next((item for item in skill.scripts if item.endswith("render_stage_prompt.py")), "")
        if script_rel and paper_id is not None:
            skill_dir = self.skills_root / Path(skill.path).parent
            script_path = skill_dir / script_rel
            if script_path.is_file():
                rendered = self._render_stage_launch_via_script(
                    script_path=script_path,
                    stage=stage,
                    paper_id=paper_id,
                    project_id=project_id,
                    goal=goal,
                    preferred_draft_id=preferred_draft_id,
                )
                if rendered:
                    return rendered

        header = []
        if paper_id is not None:
            header.append(f"paper_id={paper_id}")
        if project_id is not None:
            header.append(f"project_id={project_id}")
        if goal:
            header.append(f"goal={goal}")
        if preferred_draft_id:
            header.append(f"preferred_draft_id={preferred_draft_id}")
        body = [
            f"请使用 {skill.name} skill，继续 {stage} 阶段。",
            "优先读取当前状态和已归档产物，再决定下一步。",
        ]
        return "\n".join(header + body).strip()

    @staticmethod
    def _positive_int_or_none(value: Any) -> Optional[int]:
        if value is None or not str(value).strip():
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _render_stage_launch_via_script(
        *,
        script_path: Path,
        stage: str,
        paper_id: Optional[int],
        project_id: Optional[int],
        goal: Optional[str],
        preferred_draft_id: Optional[str],
    ) -> str:
        spec = importlib.util.spec_from_file_location(
            f"_agent_skill_stage_prompt_{abs(hash(str(script_path)))}",
            str(script_path),
        )
        if spec is None or spec.loader is None:
            return ""
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        builder = getattr(module, "build_prompt", None) or getattr(module, "_build_prompt", None)
        if not callable(builder):
            return ""
        prompt = builder(
            stage=stage,
            paper_id=paper_id,
            project_id=project_id,
            goal=goal,
            preferred_draft_id=preferred_draft_id,
        )
        return str(prompt or "").strip()

    @classmethod
    def _resolve_skills_root(cls, skills_root: Optional[Path]) -> Path:
        if skills_root is not None:
            return Path(skills_root).expanduser()
        env_root = str(os.getenv("AGENT_SKILLS_ROOT") or "").strip()
        if env_root:
            return Path(env_root).expanduser()
        for root in cls._skill_search_roots():
            candidate = root / ".agents" / "skills"
            if candidate.exists():
                return candidate
        return Path.cwd() / ".agents" / "skills"

    @staticmethod
    def _skill_search_roots() -> Tuple[Path, ...]:
        file_path = Path(__file__).resolve()
        ordered_candidates: List[Path] = [Path.cwd().resolve()]
        ordered_candidates.extend(parent.resolve() for parent in Path.cwd().resolve().parents)
        ordered_candidates.extend(parent.resolve() for parent in file_path.parents)

        unique_roots: List[Path] = []
        seen: set[str] = set()
        for candidate in ordered_candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            unique_roots.append(candidate)
        return tuple(unique_roots)


_agent_skill_service: Optional[AgentSkillService] = None


def get_agent_skill_service() -> AgentSkillService:
    global _agent_skill_service
    if _agent_skill_service is None:
        _agent_skill_service = AgentSkillService()
    return _agent_skill_service
