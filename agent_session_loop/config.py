"""Configuration helpers for agent-session-loop."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

APP_NAME = "agent-session-loop"
HARNESS_CONFIG_NAME = "harnesses.json"
DEFAULT_HARNESS_EXECUTABLES = {
    "codex": "codex",
    "claude": "claude",
    "copilot": "copilot",
}
NATIVE_MODEL_DEFAULT = "native CLI default"
MODEL_SOURCE_FLAG = "flag"
MODEL_SOURCE_UNRESOLVED = "unresolved"
MODEL_SOURCE_CODEX_CONFIG = "codex_config"
MODEL_SOURCE_COPILOT_SETTINGS = "copilot_settings"
MODEL_SOURCE_CONTROL = "control"
MODEL_SOURCE_SPARK = "spark"
EFFORT_SOURCE_FLAG = "flag"
EFFORT_SOURCE_CONTROL = "control"


class ConfigError(Exception):
    """Raised when optional user configuration cannot be used safely."""


def default_state_root() -> Path:
    """Return the default root for operational runner metadata."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home).expanduser().resolve() / APP_NAME
    return Path("~/.local/state").expanduser().resolve() / APP_NAME


def default_config_path() -> Path:
    """Return the optional user harness configuration path."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return (
            Path(xdg_config_home).expanduser().resolve()
            / APP_NAME
            / HARNESS_CONFIG_NAME
        )
    return Path("~/.config").expanduser().resolve() / APP_NAME / HARNESS_CONFIG_NAME


def default_harness_executable(harness: str) -> str:
    """Return the default executable name for a supported harness."""
    return DEFAULT_HARNESS_EXECUTABLES[harness]


def _coerce_config_path(path: str | Path | None = None) -> Path:
    if path is None:
        return default_config_path()
    return Path(path).expanduser().resolve()


def _harness_entries(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    entries = raw.get("harnesses", raw)
    if not isinstance(entries, Mapping):
        raise ConfigError("harnesses.json field 'harnesses' must be an object")
    return entries


def load_harness_config(path: str | Path | None = None) -> dict[str, dict[str, str]]:
    """Load optional harness executable overrides.

    Supported shapes:

    - {"codex": {"executable": "/path/to/codex"}}
    - {"codex": "/path/to/codex"}
    - {"harnesses": {"codex": {"executable": "/path/to/codex"}}}
    """
    config_path = _coerce_config_path(path)
    if not config_path.exists():
        return {}

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"invalid JSON in {config_path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"cannot read {config_path}: {error}") from error

    if not isinstance(raw, Mapping):
        raise ConfigError(f"{config_path} must contain a JSON object")

    normalized: dict[str, dict[str, str]] = {}
    for harness, entry in _harness_entries(raw).items():
        if harness not in DEFAULT_HARNESS_EXECUTABLES:
            continue

        if isinstance(entry, str):
            executable = entry
        elif isinstance(entry, Mapping):
            executable = entry.get("executable", "")
        else:
            raise ConfigError(
                f"{config_path} entry for {harness!r} must be a string or object"
            )

        if executable is None or not isinstance(executable, str):
            raise ConfigError(
                f"{config_path} executable for {harness!r} must be a string"
            )
        executable = executable.strip()
        if not executable:
            raise ConfigError(
                f"{config_path} executable for {harness!r} must not be empty"
            )
        normalized[harness] = {"executable": executable}

    return normalized


def configured_harness_executable(
    harness: str,
    path: str | Path | None = None,
) -> str:
    """Return the configured executable for a harness, falling back to defaults."""
    configured = load_harness_config(path).get(harness)
    if configured is not None:
        return configured["executable"]
    return default_harness_executable(harness)


def harness_config_payload(path: str | Path | None = None) -> dict[str, Any]:
    """Return metadata about the optional harness configuration file."""
    config_path = _coerce_config_path(path)
    configured = load_harness_config(config_path)
    return {
        "path": str(config_path),
        "present": config_path.exists(),
        "configured_harnesses": sorted(configured),
    }


def _unresolved_native_model_payload() -> dict[str, str]:
    return {
        "model": NATIVE_MODEL_DEFAULT,
        "model_source": MODEL_SOURCE_UNRESOLVED,
    }


def _string_key(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _codex_native_model_payload() -> dict[str, str]:
    config_path = Path("~/.codex/config.toml").expanduser()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError, UnicodeDecodeError:
        return _unresolved_native_model_payload()
    if not isinstance(raw, Mapping):
        return _unresolved_native_model_payload()
    model = _string_key(raw, "model")
    if model is None:
        return _unresolved_native_model_payload()
    return {"model": model, "model_source": MODEL_SOURCE_CODEX_CONFIG}


def _copilot_native_model_payload() -> dict[str, str]:
    config_path = Path("~/.copilot/settings.json").expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError, UnicodeDecodeError:
        return _unresolved_native_model_payload()
    if not isinstance(raw, Mapping):
        return _unresolved_native_model_payload()
    model = _string_key(raw, "model")
    if model is None:
        return _unresolved_native_model_payload()
    return {"model": model, "model_source": MODEL_SOURCE_COPILOT_SETTINGS}


def native_model_payload(harness: str) -> dict[str, str]:
    """Return best-effort metadata for the harness native model default."""
    if harness == "codex":
        return _codex_native_model_payload()
    if harness == "copilot":
        return _copilot_native_model_payload()
    return _unresolved_native_model_payload()


def selected_model_payload(harness: str, model: str | None) -> dict[str, str]:
    """Return model metadata for a requested or native-default harness model."""
    if model is not None:
        return {"model": model, "model_source": MODEL_SOURCE_FLAG}
    return native_model_payload(harness)


def selected_effort_payload(effort: str | None) -> dict[str, str | None]:
    """Return effort metadata for an explicitly requested harness effort."""
    return {
        "effort": effort,
        "effort_source": EFFORT_SOURCE_FLAG if effort is not None else None,
    }


def model_effort_payload(
    harness: str,
    *,
    model: str | None,
    effort: str | None,
) -> dict[str, str | None]:
    """Return selected model and effort metadata for reporting surfaces."""
    return {
        **selected_model_payload(harness, model),
        **selected_effort_payload(effort),
    }
