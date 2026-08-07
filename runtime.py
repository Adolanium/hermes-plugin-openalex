"""Shared plumbing every handler sits on.

The registry contract is strict: a handler takes ``(args, **kwargs)``, returns
a JSON string, and never raises. A handler that raises takes the turn down with
it, so every one of them is wrapped here once rather than trusting nine
separate try/except blocks to stay correct.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from typing import Any

from . import config as config_mod
from . import pricing
from .budget import tracker
from .client import get_client
from .errors import OpenAlexError, redact
from .shaping import envelope, fit

logger = logging.getLogger(__name__)


def session_id(kwargs: dict[str, Any]) -> str | None:
    for key in ("task_id", "session_id", "session_key"):
        value = kwargs.get(key)
        if value:
            return str(value)
    return None


def tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., str]:
    @functools.wraps(fn)
    def wrapper(args: dict[str, Any], **kwargs: Any) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            result = fn(args, **kwargs)
        except OpenAlexError as exc:
            result = exc.to_payload()
        except Exception as exc:  # pragma: no cover - the safety net
            logger.exception("openalex tool %s failed unexpectedly", fn.__name__)
            key = None
            try:
                key = config_mod.load().api_key
            except Exception:
                pass
            result = {
                "ok": False,
                "error": redact(f"{type(exc).__name__}: {exc}", key),
                "error_kind": "internal",
                "next_step": (
                    "This is a bug in the OpenAlex plugin rather than a problem "
                    "with the request. Report it at "
                    "https://github.com/Adolanium/hermes-plugin-openalex/issues "
                    "and continue without this data."
                ),
            }
        try:
            return json.dumps(result, default=str)
        except Exception:
            return json.dumps({"ok": False, "error": "Result was not serializable."})

    return wrapper


def verbosity_for(args: dict[str, Any], cfg: config_mod.OpenAlexConfig) -> str:
    requested = str(args.get("verbosity") or "").strip().lower()
    return requested if requested in config_mod.VALID_VERBOSITY else cfg.verbosity


def entity_for(args: dict[str, Any], default: str = "works") -> str:
    entity = str(args.get("entity") or default).strip().lower()
    return entity if entity in config_mod.ENTITIES else default


def cost_block(
    cfg: config_mod.OpenAlexConfig,
    sess: str | None,
    *,
    call_class: str,
) -> dict[str, Any]:
    """The cost footer on every result.

    Both the session ledger and the account's own remaining balance are shown.
    Seeing the number fall is what makes an agent ration itself rather than
    discovering the wall.
    """
    led = tracker.ledger(sess)
    block: dict[str, Any] = {
        "this_call_usd": pricing.cost_of(call_class),
        "price_class": call_class,
        **led.snapshot(cfg.budget.usd_per_session),
    }
    try:
        block.update(get_client(cfg).meter.snapshot())
    except Exception:
        pass
    return block


def ok(
    data: dict[str, Any],
    cfg: config_mod.OpenAlexConfig,
    *,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return envelope(data, max_chars=cfg.max_result_chars, cost=cost)


def small(data: dict[str, Any], cfg: config_mod.OpenAlexConfig) -> dict[str, Any]:
    return fit({"ok": True, **data}, cfg.max_result_chars)


def bad(message: str, next_step: str = "") -> dict[str, Any]:
    payload = {"ok": False, "error": message, "error_kind": "bad_request"}
    if next_step:
        payload["next_step"] = next_step
    return payload


# --- input helpers ---------------------------------------------------------

_ID_PREFIXES = (
    "openalex:",
    "doi:",
    "pmid:",
    "pmcid:",
    "mag:",
    "orcid:",
    "ror:",
    "issn:",
    "wikidata:",
)


def normalize_id(value: str) -> str:
    """Coerce an identifier into the free, prefixed form.

    OpenAlex charges for the full-URL form of an id and nothing for the
    prefixed form, and a bare DOI with no prefix 404s. Normalising here means
    the model can pass whichever shape it happens to have and still get the
    free path.
    """
    text = str(value or "").strip()
    if not text:
        return text

    lowered = text.lower()
    if lowered.startswith(_ID_PREFIXES):
        return text
    # Bare OpenAlex id, e.g. W2741809807.
    if len(text) > 1 and text[0].upper() in "WASITCPFK" and text[1:].isdigit():
        return text.upper()

    for host, prefix in (
        ("doi.org/", "doi:"),
        ("orcid.org/", "orcid:"),
        ("ror.org/", "ror:"),
        ("openalex.org/", ""),
    ):
        if host in lowered:
            tail = text[lowered.index(host) + len(host) :]
            return f"{prefix}{tail}" if prefix else tail.upper()

    if text.startswith("10.") and "/" in text:
        return f"doi:{text}"
    if lowered.startswith("pmc"):
        return f"pmcid:{text}"
    if text.isdigit():
        return f"pmid:{text}"
    return text


def clamp_per_page(value: Any, default: int = 25) -> int:
    """OpenAlex allows 1 to 200, despite the spec and llms.txt both saying 100."""
    try:
        return max(1, min(200, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_sort(value: Any) -> str | None:
    """OpenAlex only accepts ``field:asc`` / ``field:desc``.

    Its own OpenAPI description advertises a ``-field`` form for descending,
    which 400s. Rewrite it rather than passing through a documented syntax
    that does not work.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("-"):
        return f"{text[1:]}:desc"
    return text
