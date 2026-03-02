from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_checker_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "check_mojibake.py"
    spec = importlib.util.spec_from_file_location("check_mojibake", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def test_check_mojibake_should_pass_clean_utf8_file(tmp_path: Path):
    checker = _load_checker_module()
    target = tmp_path / "backend" / "app"
    target.mkdir(parents=True, exist_ok=True)
    (target / "ok.py").write_text("msg = '论文阅读正常文本'\n", encoding="utf-8")

    issues = checker.scan_paths(
        root=tmp_path,
        paths=["backend/app"],
        excludes=[],
        strict=True,
    )
    assert issues == []


def test_check_mojibake_should_detect_semantic_mojibake_term(tmp_path: Path):
    checker = _load_checker_module()
    target = tmp_path / "backend" / "app"
    target.mkdir(parents=True, exist_ok=True)
    (target / "bad.py").write_text(
        "message = '宸蹭粠收藏夹移除'\n",
        encoding="utf-8",
    )

    issues = checker.scan_paths(
        root=tmp_path,
        paths=["backend/app"],
        excludes=[],
        strict=False,
    )
    assert issues
    assert any(item.rule == "mojibake_term" for item in issues)


def test_check_mojibake_should_detect_utf8_bom(tmp_path: Path):
    checker = _load_checker_module()
    target = tmp_path / "backend" / "app"
    target.mkdir(parents=True, exist_ok=True)
    (target / "bom.py").write_bytes(b"\xef\xbb\xbf" + "value = 1\n".encode("utf-8"))

    issues = checker.scan_paths(
        root=tmp_path,
        paths=["backend/app"],
        excludes=[],
        strict=False,
    )
    assert issues
    assert any(item.rule == "utf8_bom" for item in issues)


def test_check_mojibake_should_ignore_whitelist_marker_line(tmp_path: Path):
    checker = _load_checker_module()
    target = tmp_path / "scripts"
    target.mkdir(parents=True, exist_ok=True)
    (target / "guide.md").write_text(
        "提交前重点检查是否出现 `锟`、`�` 等异常字符。\n",
        encoding="utf-8",
    )

    issues = checker.scan_paths(
        root=tmp_path,
        paths=["scripts"],
        excludes=[],
        strict=False,
    )
    assert issues == []
