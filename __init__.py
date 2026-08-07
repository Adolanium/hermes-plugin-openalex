"""OpenAlex for Hermes Agent.

Registration only. Everything real lives in the sibling modules, so a mistake
here cannot take a tool down with it.

Hermes loads this as ``hermes_plugins.openalex`` with ``__path__`` pointed at
the plugin directory, which is why the relative imports below work from
~/.hermes/plugins/openalex/ without any sys.path surgery.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import handlers_core, handlers_full, schemas
from .budget import tracker

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

TOOLSET = "openalex"

_HANDLERS: dict[str, Callable[..., str]] = {
    "openalex_resolve": handlers_core.openalex_resolve,
    "openalex_get": handlers_core.openalex_get,
    "openalex_count": handlers_core.openalex_count,
    "openalex_search": handlers_core.openalex_search,
    "openalex_related": handlers_core.openalex_related,
    "openalex_account": handlers_core.openalex_account,
    "openalex_fields": handlers_core.openalex_fields,
    "openalex_classify": handlers_full.openalex_classify,
    "openalex_harvest": handlers_full.openalex_harvest,
    "openalex_fulltext": handlers_full.openalex_fulltext,
}


def _visible(name: str) -> bool:
    """Should this tool be offered to the model right now?

    Unlike a keyless API, OpenAlex works anonymously for every tool, just on a
    tenth of the budget, so the gating all lives in the config rather than
    depending on whether a key is present.

    The registry caches this for about 30 seconds, so it is cheap to call.
    """
    try:
        return name in config_mod.load().visible_tools()
    except Exception:
        return True  # fail open rather than silently losing the toolset


def _make_check(name: str) -> Callable[[], bool]:
    def check() -> bool:
        return _visible(name)

    return check


def _on_session_boundary(**kwargs: Any) -> None:
    """Give each session a fresh spend ledger.

    Without this a long-lived gateway would carry one conversation's spending
    into the next and start refusing calls for reasons unrelated to the
    current work.
    """
    task_id = kwargs.get("task_id") or kwargs.get("session_id")
    try:
        tracker.reset(str(task_id) if task_id else None)
    except Exception:  # pragma: no cover - a hook must never break the session
        logger.debug("openalex: budget reset failed", exc_info=True)


def _register_skills(ctx: Any) -> None:
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.is_dir():
        return
    descriptions = {
        "query-syntax": "OpenAlex filters, facets and cost-aware querying",
        "lit-review": "Cheap literature mapping with OpenAlex",
    }
    for child in sorted(skills_dir.iterdir()):
        skill_file = child / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            ctx.register_skill(child.name, skill_file, description=descriptions.get(child.name, ""))
        except Exception as exc:
            logger.warning("openalex: could not register skill %s: %s", child.name, exc)


def register(ctx: Any) -> None:
    """Entry point. Hermes calls this once, at startup, on every frontend."""
    for name, handler in _HANDLERS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schemas.ALL_SCHEMAS[name],
            handler=handler,
            check_fn=_make_check(name),
            emoji=schemas.EMOJI.get(name, "📚"),
        )

    ctx.register_hook("on_session_start", _on_session_boundary)
    ctx.register_hook("on_session_reset", _on_session_boundary)

    _register_skills(ctx)

    try:
        from .cli import build_parser, run_command

        ctx.register_cli_command(
            name="openalex",
            help="OpenAlex: key, budget, lookups, diagnostics",
            setup_fn=build_parser,
            handler_fn=run_command,
            description=(
                "Configure and drive the OpenAlex plugin from the terminal. "
                "Start with 'hermes openalex doctor'."
            ),
        )
    except Exception as exc:
        logger.warning("openalex: CLI command not registered: %s", exc)

    try:
        from .slash import handle_slash

        ctx.register_command(
            "openalex",
            handle_slash,
            description="OpenAlex lookup, counts and budget",
            args_hint="<id | query | budget>",
        )
    except Exception as exc:
        logger.warning("openalex: slash command not registered: %s", exc)

    cfg = config_mod.load()
    logger.info(
        "openalex plugin ready: profile=%s, %d tools visible, key=%s",
        cfg.profile,
        sum(1 for name in _HANDLERS if _visible(name)),
        "configured" if cfg.has_key else "anonymous",
    )
