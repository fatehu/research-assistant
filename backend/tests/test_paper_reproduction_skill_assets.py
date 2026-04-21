from pathlib import Path
import re

import yaml


def _paper_reproduction_skill_root() -> Path:
    file_path = Path(__file__).resolve()
    for parent in file_path.parents:
        candidate = parent / ".agents" / "skills" / "paper-reproduction"
        if candidate.exists():
            return candidate
    raise AssertionError("paper-reproduction skill root not found from test path")


def _load_skill_yaml(skill_root: Path) -> dict:
    return yaml.safe_load((skill_root / "skill.yaml").read_text(encoding="utf-8"))


def _extract_skill_md_relative_paths(skill_root: Path) -> set[str]:
    text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    return {
        match.strip()
        for match in re.findall(r"`((?:agents|references|scripts|templates)/[^`]+)`", text)
    }


def _relative_files(base_dir: Path, pattern: str) -> set[str]:
    return {
        path.relative_to(_paper_reproduction_skill_root()).as_posix()
        for path in base_dir.glob(pattern)
        if path.is_file()
    }


def test_paper_reproduction_skill_assets_are_consistent():
    skill_root = _paper_reproduction_skill_root()
    skill_yaml = _load_skill_yaml(skill_root)
    skill_md_paths = _extract_skill_md_relative_paths(skill_root)

    declared_scripts = {
        str(item.get("path") or "").strip()
        for item in list(skill_yaml.get("scripts") or [])
        if str(item.get("path") or "").strip()
    }
    declared_references = {
        str(item.get("path") or "").strip()
        for item in list(skill_yaml.get("references") or [])
        if str(item.get("path") or "").strip()
    }
    declared_interface_metadata = str(skill_yaml.get("interface_metadata_path") or "").strip()

    actual_scripts = _relative_files(skill_root / "scripts", "*.py")
    actual_references = _relative_files(skill_root / "references", "*.md")
    actual_templates = _relative_files(skill_root / "templates", "*.json")

    skill_md_scripts = {path for path in skill_md_paths if path.startswith("scripts/")}
    skill_md_references = {path for path in skill_md_paths if path.startswith("references/")}
    skill_md_templates = {path for path in skill_md_paths if path.startswith("templates/")}

    assert declared_interface_metadata == "agents/openai.yaml"
    assert (skill_root / declared_interface_metadata).exists()

    assert actual_scripts == declared_scripts
    assert actual_scripts == skill_md_scripts

    assert actual_references == declared_references
    assert actual_references == skill_md_references

    assert actual_templates == skill_md_templates

    for relative_path in actual_scripts | actual_references | actual_templates | {declared_interface_metadata}:
        assert (skill_root / relative_path).exists(), f"missing skill asset: {relative_path}"
