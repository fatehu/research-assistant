from __future__ import annotations

import json
from typing import Dict, Optional


def normalize_provider_name(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    aliases = {
        "dashscope": "aliyun",
        "aliyun": "aliyun",
        "aliyun_qwen35_flash": "aliyun",
        "aliyun_qwen35_plus": "aliyun",
        "aliyun_qwen_plus": "aliyun",
        "aliyun_qwen_max": "aliyun",
        "deepseek_test": "deepseek",
        "azure_openai": "openai",
        "azure-openai": "openai",
        "openai_compat": "openai",
    }
    return aliases.get(normalized, normalized)


def normalize_model_window_key(key: str) -> str:
    raw = str(key or "").strip().lower()
    if not raw:
        return ""
    if ":" not in raw:
        return raw
    provider, model_name = raw.split(":", 1)
    normalized_provider = normalize_provider_name(provider)
    normalized_model_name = str(model_name or "").strip().lower()
    if not normalized_provider:
        return normalized_model_name
    if not normalized_model_name:
        return normalized_provider
    return f"{normalized_provider}:{normalized_model_name}"


def parse_model_window_overrides(raw: str) -> Dict[str, int]:
    text = str(raw or "{}").strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    normalized: Dict[str, int] = {}
    for key, value in parsed.items():
        normalized_key = normalize_model_window_key(str(key or "").strip())
        if isinstance(value, dict):
            provider_key = normalize_provider_name(str(key or "").strip())
            for nested_key, nested_value in value.items():
                try:
                    window = int(nested_value)
                except Exception:
                    continue
                nested_name = str(nested_key or "").strip()
                if not nested_name or window <= 0:
                    continue
                if nested_name in {"*", "default"}:
                    normalized[provider_key] = window
                    continue
                normalized_nested_key = normalize_model_window_key(f"{provider_key}:{nested_name}")
                if normalized_nested_key:
                    normalized[normalized_nested_key] = window
            continue
        try:
            window = int(value)
        except Exception:
            continue
        if normalized_key and window > 0:
            normalized[normalized_key] = window
    return normalized


def builtin_model_context_windows(*, deepseek_test_alias: str, deepseek_test_window: int) -> Dict[str, int]:
    return {
        "openai:gpt-4.1": 1_047_576,
        "openai:gpt-4o": 128_000,
        "openai:gpt-5": 400_000,
        "openai:gpt-5.1": 400_000,
        "openai:o1": 200_000,
        "openai:o3": 200_000,
        "deepseek:deepseek-chat": 128_000,
        "deepseek:deepseek-reasoner": 128_000,
        f"deepseek:{deepseek_test_alias}": deepseek_test_window,
        "aliyun:qwen-max": 32_768,
        "aliyun:qwen-plus": 1_000_000,
        "aliyun:qwen-plus-us": 1_000_000,
        "aliyun:qwen-flash": 1_000_000,
        "aliyun:qwen-flash-us": 1_000_000,
        "aliyun:qwen-turbo": 1_000_000,
        "aliyun:qwen3.5-flash": 1_000_000,
        "aliyun:qwen3.5-plus": 1_000_000,
        "aliyun:qwen3.6-flash": 1_000_000,
        "aliyun:qwen3.6-plus": 1_000_000,
        "aliyun:qwen3-max": 262_144,
        "aliyun:qwen3-max-preview": 262_144,
        "aliyun:qwen3.6-max-preview": 262_144,
    }


def resolve_model_context_window(
    *,
    provider: str,
    model_name: str,
    overrides_json: str = "{}",
    deepseek_test_alias: str = "deepseek-chat-test",
    deepseek_test_window: int = 4096,
) -> Optional[int]:
    normalized_provider = normalize_provider_name(provider)
    normalized_model = str(model_name or "").strip().lower()
    overrides = parse_model_window_overrides(overrides_json)
    for key in (
        normalize_model_window_key(f"{normalized_provider}:{normalized_model}"),
        normalized_model,
        normalized_provider,
    ):
        if key and key in overrides:
            return overrides[key]

    builtin = builtin_model_context_windows(
        deepseek_test_alias=str(deepseek_test_alias or "deepseek-chat-test").strip().lower(),
        deepseek_test_window=max(int(deepseek_test_window or 4096), 1024),
    )
    for key in (
        normalize_model_window_key(f"{normalized_provider}:{normalized_model}"),
        normalized_model,
        normalized_provider,
    ):
        if key and key in builtin:
            return builtin[key]

    heuristics: list[tuple[str, int]] = []
    if normalized_provider == "openai":
        heuristics = [
            ("gpt-4.1", 1_047_576),
            ("gpt-4o", 128_000),
            ("gpt-5.1", 400_000),
            ("gpt-5", 400_000),
            ("o3", 200_000),
            ("o1", 200_000),
        ]
    elif normalized_provider == "deepseek":
        heuristics = [("deepseek", 128_000)]
    elif normalized_provider == "aliyun":
        heuristics = [
            ("qwen3.6-plus", 1_000_000),
            ("qwen3.6-flash", 1_000_000),
            ("qwen3.5-plus", 1_000_000),
            ("qwen3.5-flash", 1_000_000),
            ("qwen-plus-us", 1_000_000),
            ("qwen-plus", 1_000_000),
            ("qwen-flash-us", 1_000_000),
            ("qwen-flash", 1_000_000),
            ("qwen-turbo", 1_000_000),
            ("qwen3-max-preview", 262_144),
            ("qwen3-max", 262_144),
            ("qwen-max", 32_768),
        ]
    elif normalized_provider == "ollama":
        heuristics = [("", 32_768)]

    for needle, window in heuristics:
        if not needle or needle in normalized_model:
            return window
    return None
