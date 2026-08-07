"""Typed errors, and the one distinction this API makes you earn.

OpenAlex answers 429 for two completely different situations. One means "you
sent that too fast, wait a second". The other means "your money is gone until
midnight UTC". Retrying the first is correct. Retrying the second wastes up to
24 hours of an agent's patience on a wall that will not move.

Worse, the obvious way to tell them apart does not work. On a throttling 429
the API reports ``X-RateLimit-Remaining: 0`` even when budget remains, so
branching on the remaining balance misclassifies every throttle as an
exhausted wallet. ``Retry-After`` is the honest signal: about a second for a
throttle, tens of thousands of seconds for an exhausted budget.
"""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "***"

# Below this many seconds a 429 is the server asking us to slow down. Above it,
# the budget is gone. The real values are around 1 second versus a partial day,
# so anything in the middle would be a surprise worth failing loudly on.
THROTTLE_RETRY_CEILING = 120.0


def redact(text: str, api_key: str | None = None) -> str:
    """Strip the API key out of a URL or message.

    The documented auth transport is a query parameter, so the key can end up
    in any URL we build even though this plugin prefers the header form.
    """
    if not text:
        return text
    out = text
    if api_key:
        out = out.replace(api_key, _REDACTED)
    return re.sub(r"(?i)([?&]api_key=)[^&\s]+", r"\1" + _REDACTED, out)


class OpenAlexError(Exception):
    """Base. Handlers catch these and never let one escape."""

    kind = "error"
    next_step = ""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        next_step: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        if next_step:
            self.next_step = next_step
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "error_kind": self.kind,
        }
        if self.status is not None:
            payload["http_status"] = self.status
        if self.next_step:
            payload["next_step"] = self.next_step
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigError(OpenAlexError):
    kind = "config"


class AuthError(OpenAlexError):
    kind = "auth"
    next_step = (
        "The API key was rejected. OpenAlex fails closed on a bad key rather "
        "than falling back to anonymous access, so this will not fix itself. "
        "Run 'hermes openalex setup' with a key from "
        "https://openalex.org/settings/api, or unset OPENALEX_API_KEY to use "
        "the anonymous tier."
    )


class BudgetExhaustedError(OpenAlexError):
    """OpenAlex says the wallet is empty. Resets at midnight UTC."""

    kind = "budget_exhausted"
    next_step = (
        "The OpenAlex daily budget is spent and refills at midnight UTC. Do "
        "not retry this call. ID lookups (openalex_get) and name resolution "
        "(openalex_resolve) are free and still work at zero budget, so use "
        "those. A free API key raises the daily budget from $0.10 to $1.00."
    )


class LocalBudgetError(OpenAlexError):
    """Our own guard tripped before the request went out."""

    kind = "budget"
    next_step = (
        "The per-session budget in config.yaml is spent. openalex_count is "
        "ten times cheaper than openalex_search and answers most questions "
        "about how many or which distribution. openalex_get and "
        "openalex_resolve cost nothing at all."
    )


class ThrottledError(OpenAlexError):
    """Too many requests per second. Costs nothing and is worth retrying."""

    kind = "throttled"
    next_step = (
        "Sent faster than the per-second limit. The plugin already paces "
        "itself and retried. Wait a moment and try once more."
    )


class NotFoundError(OpenAlexError):
    kind = "not_found"
    next_step = (
        "No such record. Check the identifier form: OpenAlex wants a prefixed "
        "id like 'doi:10.7717/peerj.4375' or 'W2741809807'. A bare DOI with "
        "no 'doi:' prefix returns 404. Use openalex_resolve to turn a name "
        "into an id."
    )


class BadRequestError(OpenAlexError):
    """400. The message enumerates every valid field, so pass it through."""

    kind = "bad_request"
    next_step = (
        "The query was malformed. OpenAlex lists the valid field names in the "
        "error details below, so read them and correct the call rather than "
        "guessing. Note that a rejected request still costs $0.0001."
    )


class UpstreamError(OpenAlexError):
    kind = "upstream"
    next_step = (
        "OpenAlex returned a server error. The plugin already retried with "
        "backoff. Note that semantic search is in beta and currently returns "
        "500 for everyone, so avoid it."
    )


class TransportError(OpenAlexError):
    kind = "transport"
    next_step = "Could not reach OpenAlex. Check network access, then run 'hermes openalex doctor'."


def classify_http(
    status: int,
    body_text: str,
    parsed: Any,
    *,
    retry_after: float | None = None,
    cost_required: float | None = None,
) -> OpenAlexError:
    """Map a failed response onto a typed error.

    ``retry_after`` and ``cost_required`` come from the response headers and
    are what separate the two flavours of 429. Both are optional so this stays
    testable without synthesising a whole response object.
    """
    message = ""
    if isinstance(parsed, dict):
        message = str(parsed.get("message") or parsed.get("error") or "").strip()
    if not message:
        stripped = (body_text or "").strip()
        # 404 and 500 come back as HTML rather than JSON, so never assume a
        # parsed body exists just because the request reached the server.
        if stripped.startswith("<"):
            message = f"HTTP {status} (non-JSON response)"
        else:
            message = stripped[:300] or f"HTTP {status}"

    if status == 429:
        details = {}
        if retry_after is not None:
            details["retry_after_seconds"] = retry_after
        if cost_required is not None:
            details["cost_required_usd"] = cost_required
        # Retry-After is the reliable discriminator. The remaining-balance
        # headers report zero during throttling even when budget is left.
        if retry_after is not None and retry_after <= THROTTLE_RETRY_CEILING:
            return ThrottledError(message, status=status, details=details)
        return BudgetExhaustedError(message, status=status, details=details)

    if status == 401:
        return AuthError(message, status=status)
    if status == 404:
        return NotFoundError(message, status=status)
    if status in (400, 403):
        details = {}
        if isinstance(parsed, dict) and parsed.get("message"):
            details["openalex_message"] = parsed["message"]
        return BadRequestError(message, status=status, details=details)
    if status >= 500:
        return UpstreamError(message, status=status)
    return OpenAlexError(message, status=status)
