"""``/openalex``, the in-conversation command.

Dispatch is forgiving. Something that looks like an identifier gets a free
lookup, and anything else gets a free-ish count rather than a search, because
a slash command should not quietly spend money. If you want the records
themselves, ask the agent.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .runtime import normalize_id

_USAGE = (
    "**/openalex**: scholarly lookup\n"
    "```\n"
    "/openalex W2741809807         fetch a work by id           free\n"
    "/openalex 10.7717/peerj.4375  fetch by DOI                 free\n"
    "/openalex bengio              resolve a name to an id      free\n"
    "/openalex budget              session spend and balance    free\n"
    "```\n"
    "Counts and searches cost money, so ask the agent for those and it will "
    "pick the cheapest call that answers the question."
)

# Anything that looks like an OpenAlex id, a DOI, an ORCID or a ROR.
_LOOKS_LIKE_ID = re.compile(
    r"^(?:[WASITCPFK]\d{4,}|10\.\d{4,}/\S+|\d{4}-\d{4}-\d{4}-\d{3}[\dX]|"
    r"(?:doi|orcid|ror|issn|pmid|pmcid|mag|openalex):\S+|https?://\S+)$",
    re.IGNORECASE,
)


def _fmt_work(record: dict[str, Any]) -> str:
    lines = [f"**{record.get('title') or record.get('name') or record.get('id')}**"]

    meta = " · ".join(
        str(p)
        for p in [
            record.get("year"),
            record.get("venue"),
            f"{record['cited_by_count']:,} citations" if record.get("cited_by_count") else None,
            record.get("open_access"),
        ]
        if p
    )
    if meta:
        lines.append(meta)

    authors = record.get("authors") or []
    if authors:
        names = ", ".join(a.get("name", "") for a in authors[:5] if a.get("name"))
        if record.get("more_authors"):
            names += f" and {record.get('author_count', 0) - len(authors[:5])} more"
        lines.append(f"\n{names}")

    if record.get("doi"):
        lines.append(f"\nhttps://doi.org/{record['doi']}")
    if record.get("oa_url"):
        lines.append(record["oa_url"])
    if record.get("abstract"):
        lines.append(f"\n_{record['abstract'][:400]}_")
    if record.get("is_retracted"):
        lines.append("\n**RETRACTED**")
    return "\n".join(lines)


def _fmt_entity(record: dict[str, Any]) -> str:
    lines = [f"**{record.get('name') or record.get('id')}**"]
    bits = [
        f"{record['works_count']:,} works" if record.get("works_count") else None,
        f"{record['cited_by_count']:,} citations" if record.get("cited_by_count") else None,
        f"h-index {record['h_index']}" if record.get("h_index") else None,
        record.get("country"),
    ]
    line = " · ".join(str(b) for b in bits if b)
    if line:
        lines.append(line)
    if record.get("institutions"):
        lines.append(", ".join(record["institutions"]))
    return "\n".join(lines)


def _fmt_matches(result: dict[str, Any]) -> str:
    matches = result.get("matches") or []
    if not matches:
        return f"No match for `{result.get('query')}`."
    lines = [f"**{len(matches)} matches** for `{result.get('query')}`  (free)"]
    for match in matches[:8]:
        works = f"  {match['works_count']:,} works" if match.get("works_count") else ""
        hint = f"  _{match['hint']}_" if match.get("hint") else ""
        lines.append(f"  `{match.get('id')}`  {match.get('name')}{works}{hint}")
    return "\n".join(lines)


def handle_slash(raw_args: str) -> str:
    argument = (raw_args or "").strip()
    if not argument or argument.lower() in {"help", "-h", "--help", "?"}:
        return _USAGE

    try:
        if argument.lower() in {"budget", "cost", "account"}:
            from .handlers_core import openalex_account

            result = json.loads(openalex_account({}))
            session = result.get("session") or {}
            account = result.get("account") or {}
            lines = [
                "**OpenAlex budget**",
                f"session spent: ${session.get('spent_usd', 0):.4f} of "
                f"${session.get('budget_usd', 0):.4f}",
                f"calls: {session.get('calls', 0)} "
                f"({session.get('free_calls', 0)} free, "
                f"{session.get('cache_hits', 0)} cache hits)",
            ]
            if account.get("daily_remaining_usd") is not None:
                lines.append(
                    f"account today: ${account['daily_remaining_usd']:.4f} of "
                    f"${account.get('daily_budget_usd', 0):.4f} left"
                )
            elif not result.get("api_key_configured"):
                lines.append("no API key, anonymous tier at $0.10/day")
            return "\n".join(lines)

        if _LOOKS_LIKE_ID.match(argument):
            from .handlers_core import openalex_get

            result = json.loads(openalex_get({"id": normalize_id(argument), "verbosity": "detail"}))
            if not result.get("ok"):
                return _error_text(result)
            record = result.get("record") or (result.get("records") or [{}])[0]
            if not record:
                return f"No record for `{argument}`."
            return _fmt_work(record) if record.get("title") else _fmt_entity(record)

        from .handlers_core import openalex_resolve

        result = json.loads(openalex_resolve({"query": argument}))
        if not result.get("ok"):
            return _error_text(result)
        return _fmt_matches(result)

    except Exception as exc:  # pragma: no cover - a slash command must not throw
        return f"OpenAlex command failed: {type(exc).__name__}: {exc}"


def _error_text(result: dict[str, Any]) -> str:
    parts = [f"**OpenAlex error:** {result.get('error')}"]
    if result.get("next_step"):
        parts.append(f"\n{result['next_step']}")
    return "\n".join(parts)
