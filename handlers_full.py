"""The two tools behind ``profile: full``.

Both are here because they can spend real money fast. Classification is the
single most expensive call OpenAlex offers, and harvesting multiplies a cheap
call by however many pages the model asks for.
"""

from __future__ import annotations

import math
from typing import Any

from . import config as config_mod
from . import pricing, shaping
from .budget import tracker
from .client import get_client
from .errors import ConfigError
from .runtime import (
    bad,
    clamp_per_page,
    cost_block,
    entity_for,
    ok,
    session_id,
    tool,
    verbosity_for,
)


class ClassificationDisabledError(ConfigError):
    kind = "classification_disabled"
    next_step = (
        "Text classification is off by default because it costs $0.01 a call, "
        "a hundred times a list call, and ten calls exhaust the entire "
        "anonymous daily budget. If the text is already a published work, "
        "openalex_get returns its topics for free. To enable it anyway the "
        "user must set "
        "plugins.entries.openalex.budget.allow_text_classification: true. Do "
        "not attempt to work around this."
    )


@tool
def openalex_classify(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)

    title = str(args.get("title") or "").strip()
    abstract = str(args.get("abstract") or "").strip()
    if not title and not abstract:
        return bad("Give a title, an abstract, or both.")

    if not cfg.budget.allow_text_classification:
        raise ClassificationDisabledError("Text classification is disabled by local policy.")

    params = {k: v for k, v in {"title": title, "abstract": abstract}.items() if v}
    client = get_client(cfg)
    raw = client.get(
        "text/topics",
        params=params,
        session_id=sess,
        budget_usd=cfg.budget.usd_per_session,
        call_class=pricing.TEXT,
    )

    def _topic(node: Any) -> dict[str, Any]:
        if not isinstance(node, dict):
            return {}
        return {
            k: v
            for k, v in {
                "id": shaping.short_id(node.get("id")),
                "name": node.get("display_name"),
                "score": round(node["score"], 4)
                if isinstance(node.get("score"), (int, float))
                else None,
                "subfield": (node.get("subfield") or {}).get("display_name")
                if isinstance(node.get("subfield"), dict)
                else None,
                "field": (node.get("field") or {}).get("display_name")
                if isinstance(node.get("field"), dict)
                else None,
                "domain": (node.get("domain") or {}).get("display_name")
                if isinstance(node.get("domain"), dict)
                else None,
            }.items()
            if v is not None
        }

    return ok(
        {
            "primary_topic": _topic(raw.get("primary_topic")),
            "topics": [_topic(t) for t in (raw.get("topics") or [])][:5],
        },
        cfg,
        cost=cost_block(cfg, sess, call_class=pricing.TEXT),
    )


@tool
def openalex_harvest(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)
    entity = entity_for(args)
    verbosity = verbosity_for(args, cfg)

    filter_expr = str(args.get("filter") or "").strip()
    search = str(args.get("search") or "").strip()
    if not filter_expr and not search:
        return bad(
            "Give a filter or a search to define the set.",
            "Harvesting all of OpenAlex through the API is neither possible "
            "nor sensible. The bulk snapshot at s3://openalex is free.",
        )

    per_page = clamp_per_page(args.get("per_page"), 200)
    try:
        max_records = max(1, int(args.get("max_records") or 200))
    except (TypeError, ValueError):
        max_records = 200

    pages = math.ceil(max_records / per_page)
    call_class = pricing.SEARCH if search else pricing.LIST
    estimated = pages * pricing.cost_of(call_class)

    # Check the whole run up front. Stopping halfway leaves the caller with
    # partial data and a spent budget, which is the worst of both.
    tracker.check(estimated, limit=cfg.budget.usd_per_session, session_id=sess)

    params: dict[str, Any] = {"per_page": per_page, "cursor": "*"}
    if filter_expr:
        params["filter"] = filter_expr
    if search:
        params["search"] = search
    select = shaping.select_for(entity, verbosity)
    if select:
        params["select"] = select

    client = get_client(cfg)
    records: list[dict[str, Any]] = []
    total: int | None = None
    pages_fetched = 0

    while len(records) < max_records:
        raw = client.get(
            entity,
            params=params,
            cacheable=False,
            session_id=sess,
            budget_usd=cfg.budget.usd_per_session,
        )
        pages_fetched += 1
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        if total is None:
            total = meta.get("count")

        batch = [r for r in (raw.get("results") or []) if isinstance(r, dict)]
        records.extend(shaping.shape_entity(entity, row, verbosity) for row in batch)

        cursor = meta.get("next_cursor")
        if not cursor or not batch:
            break
        params["cursor"] = cursor

    records = records[:max_records]
    return ok(
        {
            "entity": entity,
            "total_matching": total,
            "retrieved": len(records),
            "pages_fetched": pages_fetched,
            "estimated_cost_usd": round(estimated, 6),
            "complete": total is not None and len(records) >= total,
            "results": records,
        },
        cfg,
        cost=cost_block(cfg, sess, call_class=call_class),
    )
