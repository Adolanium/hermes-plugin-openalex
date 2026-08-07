"""Per-session spend, in USD.

OpenAlex bills real money per request, so this ledger is the difference between
a plugin and a liability. Because the price of a call is knowable from its
shape before it is sent, the guard here refuses *in advance* rather than
discovering the wall afterwards. A refused call costs nothing, which matters
because OpenAlex bills $0.0001 even for a 400.

Two numbers are tracked and both are reported. ``predicted`` is what the
pricing table says a call should cost, and ``actual`` is what the response
headers say it did cost. They should agree, and when they diverge the plugin
says so rather than quietly trusting its own model of someone else's pricing.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .errors import LocalBudgetError

# Money is compared and displayed at sub-cent precision, so round consistently
# rather than letting float noise leak into user-visible totals.
_PRECISION = 6


@dataclass
class Ledger:
    predicted_usd: float = 0.0
    actual_usd: float = 0.0
    calls: int = 0
    free_calls: int = 0
    cache_hits: int = 0
    saved_by_cache_usd: float = 0.0
    by_class: dict[str, int] = field(default_factory=dict)

    def snapshot(self, limit: float) -> dict[str, object]:
        spent = round(self.actual_usd or self.predicted_usd, _PRECISION)
        return {
            "spent_usd": spent,
            "budget_usd": round(limit, _PRECISION),
            "remaining_usd": round(max(0.0, limit - spent), _PRECISION),
            "predicted_usd": round(self.predicted_usd, _PRECISION),
            "calls": self.calls,
            "free_calls": self.free_calls,
            "cache_hits": self.cache_hits,
            "saved_by_cache_usd": round(self.saved_by_cache_usd, _PRECISION),
            "calls_by_price_class": dict(self.by_class),
        }


class BudgetTracker:
    """One ledger per session, keyed on the task id Hermes passes to handlers."""

    def __init__(self) -> None:
        self._ledgers: dict[str, Ledger] = {}
        self._lock = threading.Lock()

    def _key(self, session_id: str | None) -> str:
        return str(session_id) if session_id else "default"

    def ledger(self, session_id: str | None = None) -> Ledger:
        with self._lock:
            return self._ledgers.setdefault(self._key(session_id), Ledger())

    def check(self, cost: float, *, limit: float, session_id: str | None = None) -> None:
        """Raise if spending ``cost`` would cross the session limit.

        Free calls never trip the guard, which is what keeps id lookups and
        autocomplete working after the budget is gone.
        """
        if cost <= 0:
            return
        led = self.ledger(session_id)
        with self._lock:
            spent = led.actual_usd or led.predicted_usd
            if spent + cost > limit + 1e-12:
                remaining = max(0.0, limit - spent)
                raise LocalBudgetError(
                    f"Session budget would be exceeded: this call costs "
                    f"${cost:.4f} but only ${remaining:.4f} of ${limit:.4f} "
                    f"remains.",
                    details={
                        "requested_usd": round(cost, _PRECISION),
                        "remaining_usd": round(remaining, _PRECISION),
                        "budget_usd": round(limit, _PRECISION),
                    },
                )

    def record(
        self,
        *,
        predicted: float,
        actual: float | None = None,
        call_class: str = "list",
        session_id: str | None = None,
    ) -> None:
        led = self.ledger(session_id)
        with self._lock:
            led.predicted_usd += predicted
            led.actual_usd += actual if actual is not None else predicted
            led.calls += 1
            if predicted <= 0 and (actual or 0) <= 0:
                led.free_calls += 1
            led.by_class[call_class] = led.by_class.get(call_class, 0) + 1

    def record_cache_hit(self, saved: float, session_id: str | None = None) -> None:
        led = self.ledger(session_id)
        with self._lock:
            led.cache_hits += 1
            led.saved_by_cache_usd += max(0.0, saved)

    def reset(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._ledgers.clear()
            else:
                self._ledgers.pop(self._key(session_id), None)


tracker = BudgetTracker()
