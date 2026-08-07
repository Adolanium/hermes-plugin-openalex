"""The HTTP layer: one client, one rate limiter, one place that knows the price.

Built on httpx rather than pyalex. pyalex is a decent library but it discards
the response object after parsing, which means the ``X-RateLimit-*`` headers
never reach the caller. On a metered API those headers are the whole point:
they carry the price of the call just made and the balance remaining. It also
retries 429 blindly, which is exactly wrong for the half of 429s that mean the
wallet is empty until midnight.

httpx is already a pinned core dependency of Hermes, so this plugin adds no
install step of its own.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any

from . import config as config_mod
from . import pricing
from .budget import tracker
from .cache import TTLCache
from .errors import (
    BudgetExhaustedError,
    OpenAlexError,
    ThrottledError,
    TransportError,
    UpstreamError,
    classify_http,
    redact,
)

logger = logging.getLogger(__name__)

BASE = "https://api.openalex.org"

_RETRY_STATUSES = {500, 502, 503, 504}


class RateLimiter:
    """Paces requests below the per-second ceiling, across threads.

    OpenAlex throttles anonymous traffic at ten requests per second and says so
    in the error text. Hermes runs subagents in one process, so two of them
    reaching for OpenAlex at once would sail past that without a shared limiter.
    """

    def __init__(self, per_second: float = 8.0) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> float:
        if self._interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait <= 0:
                self._next_allowed = now + self._interval
                return 0.0
            self._next_allowed += self._interval
        time.sleep(wait)
        return wait


def _header_float(response: Any, name: str) -> float | None:
    try:
        raw = response.headers.get(name)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class Meter:
    """What the last call cost and what the account has left.

    Populated from response headers so the numbers are OpenAlex's own rather
    than this plugin's arithmetic.
    """

    def __init__(self) -> None:
        self.last_cost_usd: float | None = None
        self.daily_limit_usd: float | None = None
        self.daily_remaining_usd: float | None = None
        self.prepaid_remaining_usd: float | None = None
        self.reset_seconds: float | None = None
        self._lock = threading.Lock()

    def update(self, response: Any) -> float | None:
        cost = _header_float(response, "x-ratelimit-cost-usd")
        with self._lock:
            if cost is not None:
                self.last_cost_usd = cost
            for attr, header in (
                ("daily_limit_usd", "x-ratelimit-limit-usd"),
                ("daily_remaining_usd", "x-ratelimit-remaining-usd"),
                ("prepaid_remaining_usd", "x-ratelimit-prepaid-remaining-usd"),
                ("reset_seconds", "x-ratelimit-reset"),
            ):
                value = _header_float(response, header)
                if value is not None:
                    setattr(self, attr, value)
        return cost

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                k: v
                for k, v in {
                    "last_call_cost_usd": self.last_cost_usd,
                    "account_daily_budget_usd": self.daily_limit_usd,
                    "account_daily_remaining_usd": self.daily_remaining_usd,
                    "account_prepaid_remaining_usd": self.prepaid_remaining_usd,
                    "budget_resets_in_seconds": self.reset_seconds,
                }.items()
                if v is not None
            }


class OpenAlexClient:
    def __init__(self, cfg: config_mod.OpenAlexConfig) -> None:
        self.cfg = cfg
        self.limiter = RateLimiter(cfg.rate_limit_per_second)
        self.cache = TTLCache(
            max_entries=cfg.cache.max_entries,
            ttl_seconds=cfg.cache.ttl_seconds if cfg.cache.enabled else 0,
        )
        self.meter = Meter()
        self._client: Any = None
        self._client_lock = threading.Lock()

    def _http(self) -> Any:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import httpx

                    headers = {
                        "User-Agent": self.cfg.user_agent,
                        "Accept": "application/json",
                    }
                    if self.cfg.api_key:
                        # Bearer is undocumented but verified to authenticate,
                        # and keeps the key out of URLs, proxy logs and any
                        # traceback that echoes a request line. The documented
                        # query-param form is the fallback in _auth_params.
                        headers["Authorization"] = f"Bearer {self.cfg.api_key}"
                    self._client = httpx.Client(
                        timeout=self.cfg.timeout_seconds,
                        follow_redirects=False,
                        headers=headers,
                    )
        return self._client

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    # -- requests -----------------------------------------------------------

    def _cache_key(self, path: str, params: dict[str, Any]) -> str:
        safe = {k: v for k, v in sorted(params.items()) if k != "api_key"}
        return f"{path}?{json.dumps(safe, sort_keys=True, default=str)}"

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        cacheable: bool = True,
        session_id: str | None = None,
        budget_usd: float | None = None,
        call_class: str | None = None,
    ) -> dict[str, Any]:
        """Fetch, charging the session ledger for what the call costs.

        The budget is checked before the request leaves, because a refused call
        costs nothing while a rejected one still costs $0.0001.
        """
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        price_class = call_class or pricing.classify_request(path, params)
        cost = pricing.cost_of(price_class)

        cache_key = ""
        if cacheable and self.cfg.cache.enabled:
            cache_key = self._cache_key(path, params)
            hit = self.cache.get(cache_key)
            if hit is not None:
                tracker.record_cache_hit(cost, session_id)
                return hit

        if cost > 0 and budget_usd is not None:
            tracker.check(cost, limit=budget_usd, session_id=session_id)

        result, actual = self._send_with_retries(path, params)
        tracker.record(
            predicted=cost,
            actual=actual,
            call_class=price_class,
            session_id=session_id,
        )

        if cache_key:
            self.cache.set(cache_key, result)
        return result

    def _send_with_retries(
        self, path: str, params: dict[str, Any]
    ) -> tuple[dict[str, Any], float | None]:
        import httpx

        url = f"{BASE}/{path.lstrip('/')}"
        attempts = self.cfg.retries + 1
        last_error: OpenAlexError | None = None

        for attempt in range(attempts):
            self.limiter.acquire()
            try:
                response = self._http().get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = TransportError(
                    f"Request timed out after {self.cfg.timeout_seconds:.0f}s: "
                    f"{redact(str(exc), self.cfg.api_key)}"
                )
            except httpx.HTTPError as exc:
                last_error = TransportError(
                    f"Network error reaching OpenAlex: {redact(str(exc), self.cfg.api_key)}"
                )
            else:
                actual = self.meter.update(response)
                parsed, body_text = _decode(response)

                if response.status_code < 300:
                    if not isinstance(parsed, dict):
                        raise UpstreamError(
                            f"OpenAlex returned a non-JSON success body ({len(body_text)} bytes).",
                            status=response.status_code,
                        )
                    return parsed, actual

                error = classify_http(
                    response.status_code,
                    body_text,
                    parsed,
                    retry_after=_header_float(response, "retry-after"),
                    cost_required=_header_float(response, "x-ratelimit-cost-required-usd"),
                )

                # An exhausted budget will not clear for hours. Retrying it is
                # pure latency, so surface it immediately.
                if isinstance(error, BudgetExhaustedError):
                    raise error
                if (
                    not isinstance(error, ThrottledError)
                    and response.status_code not in _RETRY_STATUSES
                ):
                    raise error
                last_error = error

            if attempt < attempts - 1:
                delay = _backoff(attempt, last_error)
                logger.debug(
                    "openalex: retrying %s after %s (attempt %d/%d, sleeping %.2fs)",
                    path,
                    last_error.kind if last_error else "unknown",
                    attempt + 1,
                    attempts,
                    delay,
                )
                time.sleep(delay)

        raise last_error or UpstreamError("Request failed for an unknown reason.")


def _backoff(attempt: int, error: OpenAlexError | None) -> float:
    """Exponential with jitter, but honour a short server-supplied wait.

    The jitter matters because subagents that started together would otherwise
    retry in lockstep and hit the same per-second wall again.
    """
    if isinstance(error, ThrottledError):
        suggested = error.details.get("retry_after_seconds")
        if isinstance(suggested, (int, float)) and 0 < suggested <= 5:
            return float(suggested) + random.uniform(0, 0.2)
    return (2**attempt) * 0.5 + random.uniform(0, 0.3)


def _decode(response: Any) -> tuple[Any, str]:
    """Return ``(parsed_or_None, raw_text)``.

    OpenAlex answers 404 and 500 with HTML rather than JSON, so nothing here
    may assume a parseable body just because the request reached the server.
    """
    text = ""
    try:
        text = response.text or ""
    except Exception:
        text = ""
    try:
        return response.json(), text
    except Exception:
        return None, text


# --- process-wide client ---------------------------------------------------

_client: OpenAlexClient | None = None
_fingerprint: tuple | None = None
_singleton_lock = threading.Lock()


def _fp(cfg: config_mod.OpenAlexConfig) -> tuple:
    return (
        cfg.api_key,
        cfg.timeout_seconds,
        cfg.user_agent,
        cfg.rate_limit_per_second,
        cfg.retries,
        cfg.cache.enabled,
        cfg.cache.ttl_seconds,
        cfg.cache.max_entries,
    )


def get_client(cfg: config_mod.OpenAlexConfig | None = None) -> OpenAlexClient:
    global _client, _fingerprint
    cfg = cfg or config_mod.load()
    fingerprint = _fp(cfg)
    with _singleton_lock:
        if _client is None or _fingerprint != fingerprint:
            if _client is not None:
                _client.close()
            _client = OpenAlexClient(cfg)
            _fingerprint = fingerprint
        else:
            _client.cfg = cfg
        return _client


def reset_client() -> None:
    global _client, _fingerprint
    with _singleton_lock:
        if _client is not None:
            _client.close()
        _client = None
        _fingerprint = None
