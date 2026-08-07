"""What each kind of call costs, in USD.

OpenAlex prices by call shape rather than by response size, and the spread is
enormous: an id lookup is free, a list call is $0.0001, a search is ten times
that, and text classification is a hundred times a list call. On the anonymous
$0.10 daily budget, ten classification calls are the entire day.

That spread is the whole reason this plugin can do something a thin wrapper
cannot. The cost is knowable before the request goes out, so the budget guard
refuses in advance rather than discovering the wall afterwards, and the tool
descriptions can steer the model toward the cheap path for the same answer.

Prices verified live against the API. ``/rate-limit`` reports the live table in
``endpoint_costs_usd``, so :func:`refresh_from_account` replaces these defaults
when a key is configured rather than trusting a constant that may age out.
"""

from __future__ import annotations

import threading

# Call classes, in the API's own vocabulary.
SINGLETON = "singleton"
LIST = "list"
SEARCH = "search"
TEXT = "text"
CONTENT = "content"
SEMANTIC = "semantic"

_DEFAULTS: dict[str, float] = {
    SINGLETON: 0.0,
    LIST: 0.0001,
    SEARCH: 0.001,
    TEXT: 0.01,
    CONTENT: 0.01,
    SEMANTIC: 0.001,
}

# Anonymous and free-key daily budgets. The published llms.txt says $0.01 for
# anonymous, which is wrong by a factor of ten. The live header says $0.10.
ANON_DAILY_USD = 0.10
KEYED_DAILY_USD = 1.00

_lock = threading.Lock()
_prices: dict[str, float] = dict(_DEFAULTS)


def cost_of(call_class: str) -> float:
    with _lock:
        return _prices.get(call_class, _DEFAULTS.get(call_class, 0.0001))


def refresh_from_account(endpoint_costs: dict | None) -> bool:
    """Adopt the live price table from ``/rate-limit``.

    Returns True when anything actually changed, so the caller can say so
    rather than logging a no-op every startup.
    """
    if not isinstance(endpoint_costs, dict):
        return False
    changed = False
    with _lock:
        for key, value in endpoint_costs.items():
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if _prices.get(key) != price:
                _prices[key] = price
                changed = True
    return changed


def reset() -> None:
    global _prices
    with _lock:
        _prices = dict(_DEFAULTS)


def snapshot() -> dict[str, float]:
    with _lock:
        return dict(_prices)


def classify_request(path: str, params: dict) -> str:
    """Work out which price applies before sending the request.

    The rules that matter, all measured rather than assumed:

    * A singleton fetch by prefixed id is free, but the same record fetched by
      its full URL form is billed as a list call. So ``doi:10.7717/peerj.4375``
      costs nothing and ``https://doi.org/10.7717/peerj.4375`` costs $0.0001.
    * ``/autocomplete`` is free.
    * ``group_by`` is billed at list price even when a search term is present.
      That is the ten-times saving the count tool is built around.
    * Any ``search`` parameter, or any ``*.search:`` filter, is search-priced.
    """
    path = path.strip("/")

    if path.startswith("autocomplete"):
        return SINGLETON
    if path.startswith("text"):
        return TEXT

    # Split once only. An identifier can contain slashes of its own, because
    # a DOI looks like 10.7717/peerj.4375, so anything after the first
    # separator is the identifier rather than a further path segment. Counting
    # segments here would price a free DOI lookup as a billed list call.
    collection, _, identifier = path.partition("/")

    if identifier:
        # 'random' is singleton-shaped and nonetheless billed.
        if identifier == "random":
            return LIST
        # The URL forms carry a scheme and are billed, while prefixed ids are free.
        if identifier.startswith(("http://", "https://")):
            return LIST
        return SINGLETON

    # group_by short-circuits search pricing, so check it before the search
    # parameters. This ordering is the point of the whole function.
    if params.get("group_by"):
        return LIST

    if params.get("search"):
        return SEARCH
    filter_value = str(params.get("filter") or "")
    if ".search:" in filter_value or filter_value.startswith("default.search:"):
        return SEARCH

    return LIST
