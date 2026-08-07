"""Configuration, read from ``plugins.entries.openalex`` in config.yaml.

Every setting has a working default. An install where the user did nothing but
run the plugin is fully functional, and an install with no API key at all still
does free id lookups and name resolution.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

PLUGIN_ID = "openalex"

CORE_TOOLS: set[str] = {
    "openalex_resolve",
    "openalex_get",
    "openalex_count",
    "openalex_search",
    "openalex_related",
    "openalex_account",
    "openalex_fields",
    "openalex_fulltext",
}

FULL_ONLY_TOOLS: set[str] = {
    "openalex_classify",
    "openalex_harvest",
}

ALL_TOOLS: set[str] = CORE_TOOLS | FULL_ONLY_TOOLS

# These need no API key and keep working even when the daily budget is gone,
# because OpenAlex only meters the billable call classes.
FREE_TOOLS: set[str] = {"openalex_resolve", "openalex_get", "openalex_account"}

VALID_PROFILES = ("core", "full")
VALID_VERBOSITY = ("summary", "detail", "raw")

ENTITIES = (
    "works",
    "authors",
    "sources",
    "institutions",
    "topics",
    "publishers",
    "funders",
    "keywords",
)

DEFAULT_USER_AGENT = (
    "hermes-plugin-openalex/0.1.0 (+https://github.com/Adolanium/hermes-plugin-openalex)"
)


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = True
    ttl_seconds: int = 3600
    max_entries: int = 512


@dataclass(frozen=True)
class BudgetConfig:
    # Half of the anonymous daily allowance, which is 500 list calls or 50
    # searches. Enough for real work, small enough that a runaway loop is
    # capped well before the daily wall.
    usd_per_session: float = 0.05
    # Text classification costs $0.01 a call, a hundred times a list call and
    # a fifth of the default session budget. Off unless asked for.
    allow_text_classification: bool = False


@dataclass(frozen=True)
class OpenAlexConfig:
    api_key: str | None = None
    api_key_env: str = "OPENALEX_API_KEY"
    profile: str = "core"
    verbosity: str = "summary"
    max_result_chars: int = 24_000
    # The anonymous ceiling is 10 requests per second. Sitting just under it
    # leaves room for anything else on the same egress IP.
    rate_limit_per_second: float = 8.0
    timeout_seconds: float = 30.0
    retries: int = 2
    reconstruct_abstracts: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    cache: CacheConfig = field(default_factory=CacheConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    tools_enabled: list[str] = field(default_factory=list)
    tools_disabled: list[str] = field(default_factory=list)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def visible_tools(self) -> set[str]:
        """Exactly the tools the model is offered.

        This is the single source of truth for visibility, so the CLI cannot
        report a different set from the one registration actually exposes.
        """
        base = set(CORE_TOOLS) if self.profile == "core" else set(ALL_TOOLS)
        for name in self.tools_enabled:
            if name in ALL_TOOLS:
                base.add(name)
        for name in self.tools_disabled:
            base.discard(name)
        # Text classification needs its own opt-in on top of the profile,
        # because at $0.01 a call it is a hundred times a list call. A tool
        # that would always refuse is noise in the schema.
        if not self.budget.allow_text_classification:
            base.discard("openalex_classify")
        return base


_lock = threading.Lock()
_cached: OpenAlexConfig | None = None
_cached_at: float = 0.0
_TTL_SECONDS = 10.0


def _raw_plugin_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        cfg = load_config() or {}
    except Exception:
        return {}
    entries = (cfg.get("plugins") or {}).get("entries") or {}
    entry = entries.get(PLUGIN_ID)
    return entry if isinstance(entry, dict) else {}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _choice(value: Any, allowed: tuple, default: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else default


def resolve_api_key(raw: dict[str, Any]) -> tuple[str | None, str]:
    env_name = str(raw.get("api_key_env") or "OPENALEX_API_KEY").strip() or "OPENALEX_API_KEY"
    from_env = os.environ.get(env_name, "").strip()
    if from_env:
        return from_env, env_name
    literal = str(raw.get("api_key") or "").strip()
    if literal:
        return literal, env_name
    return None, env_name


def load(refresh: bool = False) -> OpenAlexConfig:
    global _cached, _cached_at
    with _lock:
        now = time.monotonic()
        if not refresh and _cached is not None and (now - _cached_at) < _TTL_SECONDS:
            return _cached

        raw = _raw_plugin_config()
        api_key, env_name = resolve_api_key(raw)

        cache_raw = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
        budget_raw = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
        tools_raw = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}

        cfg = OpenAlexConfig(
            api_key=api_key,
            api_key_env=env_name,
            profile=_choice(
                os.environ.get("HERMES_OPENALEX_PROFILE") or raw.get("profile"),
                VALID_PROFILES,
                "core",
            ),
            verbosity=_choice(
                os.environ.get("HERMES_OPENALEX_VERBOSITY") or raw.get("verbosity"),
                VALID_VERBOSITY,
                "summary",
            ),
            max_result_chars=_as_int(raw.get("max_result_chars"), 24_000, minimum=2_000),
            rate_limit_per_second=_as_float(raw.get("rate_limit_per_second"), 8.0, minimum=0.0),
            timeout_seconds=_as_float(raw.get("timeout_seconds"), 30.0, minimum=1.0),
            retries=_as_int(raw.get("retries"), 2, minimum=0),
            reconstruct_abstracts=_as_bool(raw.get("reconstruct_abstracts"), True),
            user_agent=str(raw.get("user_agent") or DEFAULT_USER_AGENT),
            cache=CacheConfig(
                enabled=_as_bool(cache_raw.get("enabled"), True),
                ttl_seconds=_as_int(cache_raw.get("ttl_seconds"), 3600, minimum=0),
                max_entries=_as_int(cache_raw.get("max_entries"), 512, minimum=1),
            ),
            budget=BudgetConfig(
                usd_per_session=_as_float(budget_raw.get("usd_per_session"), 0.05, minimum=0.0),
                allow_text_classification=_as_bool(
                    budget_raw.get("allow_text_classification"), False
                ),
            ),
            tools_enabled=_as_str_list(tools_raw.get("enabled")),
            tools_disabled=_as_str_list(tools_raw.get("disabled")),
        )

        _cached = cfg
        _cached_at = now
        return cfg


def reset() -> None:
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0
