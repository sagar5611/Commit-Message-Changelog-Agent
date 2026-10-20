"""Settings: CLI flags > SCRIBE_* env vars > git config (scribe.*) > .gitscribe file > defaults."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional

from .conventional import DEFAULT_TYPES
from .gitutil import run_git

REPO_FILE = ".gitscribe"
# A committed .gitscribe may only set these keys. Endpoint and credential settings are
# excluded on purpose so a cloned repository can't redirect your diffs to another server.
REPO_SAFE = {"model", "types", "scopes", "max_subject_length", "body", "language", "exclude", "max_diff_chars"}
CHOICES = {
    "provider": ("ollama", "openai"),
    "body": ("auto", "always", "never"),
    "hook_fallback": ("none", "heuristic"),
}


class ConfigError(Exception):
    """Invalid configuration value."""


@dataclass
class Settings:
    provider: str = "ollama"              # ollama | openai (any OpenAI-compatible server)
    model: str = "qwen2.5-coder:3b"
    base_url: str = ""                    # default depends on provider
    api_key: str = ""
    timeout: float = 90.0                 # seconds, CLI commands
    hook_timeout: float = 45.0            # seconds, inside the git hook
    temperature: float = 0.2
    context_window: int = 8192            # Ollama num_ctx
    max_diff_chars: int = 12000           # diff budget sent to the model
    max_subject_length: int = 72
    body: str = "auto"                    # auto | always | never
    types: List[str] = field(default_factory=lambda: list(DEFAULT_TYPES))
    scopes: List[str] = field(default_factory=list)
    language: str = ""                    # e.g. "German"; empty = English
    exclude: List[str] = field(default_factory=list)
    hook_fallback: str = "none"           # none | heuristic (when the model is unavailable)
    enabled: bool = True
    remote: str = "origin"
    sources: Dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    @property
    def endpoint(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        if self.provider == "openai":
            return "http://localhost:8080/v1"
        host = os.environ.get("OLLAMA_HOST", "").strip() or "localhost:11434"
        if "://" not in host:
            host = "http://" + host
        scheme, rest = host.split("://", 1)
        rest = rest.rstrip("/")
        if rest.startswith("0.0.0.0"):
            rest = "127.0.0.1" + rest[len("0.0.0.0"):]
        if not re.search(r":\d+$", rest):
            rest += ":11434"
        return f"{scheme}://{rest}"


FIELDS = [f for f in fields(Settings) if f.name != "sources"]
_GIT_KEYS = {f.name.replace("_", ""): f.name for f in FIELDS}


def git_key(name: str) -> str:
    """base_url -> scribe.baseUrl (git config keys are case-insensitive)."""
    head, *rest = name.split("_")
    return "scribe." + head + "".join(part.capitalize() for part in rest)


def _coerce(name: str, raw: str):
    default = getattr(Settings(), name)
    if isinstance(default, bool):
        value = raw.strip().lower()
        if value in ("", "1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
        raise ValueError(raw)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [item for item in re.split(r"[,\s]+", raw) if item]
    return raw.strip()


def _git_config(root: Optional[Path], *scope: str) -> Dict[str, str]:
    proc = run_git(["config", *scope, "-z", "--get-regexp", r"^scribe\."], cwd=root, check=False)
    values = {}
    for item in proc.stdout.split("\0"):
        if item:
            key, _, value = item.partition("\n")
            values[key.split(".", 1)[-1].lower()] = value
    return values


def load(root: Optional[Path] = None, **overrides) -> Settings:
    settings = Settings()
    layers = []
    repo_file = root / REPO_FILE if root else None
    if repo_file and repo_file.is_file():
        safe = {k: v for k, v in _git_config(root, "-f", str(repo_file)).items() if _GIT_KEYS.get(k) in REPO_SAFE}
        layers.append((REPO_FILE, safe))
    layers.append(("git config", _git_config(root)))
    env = {}
    for f in FIELDS:
        value = os.environ.get("SCRIBE_" + f.name.upper())
        if value is not None:
            env[f.name.replace("_", "")] = value
    layers.append(("environment", env))

    for label, values in layers:
        for key, raw in values.items():
            name = _GIT_KEYS.get(key)
            if not name:
                continue
            try:
                setattr(settings, name, _coerce(name, raw))
            except ValueError:
                raise ConfigError(f"invalid value {raw!r} for {git_key(name)} ({label})") from None
            settings.sources[name] = label
    for name, value in overrides.items():
        if value is not None:
            setattr(settings, name, value)
            settings.sources[name] = "command line"

    if settings.provider == "openai" and not settings.api_key:
        settings.api_key = os.environ.get("OPENAI_API_KEY", "")
    for name, allowed in CHOICES.items():
        if getattr(settings, name) not in allowed:
            raise ConfigError(f"{git_key(name)} must be one of: {', '.join(allowed)}")
    return settings
