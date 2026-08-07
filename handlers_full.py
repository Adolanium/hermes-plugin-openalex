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
    normalize_id,
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


@tool
def openalex_fulltext(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Two-step by design: report availability for free, download only on confirm.

    The user asked for content that is never returned automatically but is
    reachable when someone actually wants it. So the default call costs
    nothing and answers "is there full text, and what would it cost", and only
    an explicit confirm spends the $0.01.

    That also means a speculative call by the model is free, which is the
    behaviour you want from a tool whose accidental use is expensive.
    """
    cfg = config_mod.load()
    sess = session_id(kwargs)

    identifier = normalize_id(str(args.get("id") or ""))
    if not identifier:
        return bad("No work identifier given.")

    client = get_client(cfg)
    # Free singleton: does this work have content at all, and where.
    record = client.get(
        f"works/{identifier}",
        params={"select": "id,display_name,has_content,content_urls,open_access"},
        session_id=sess,
        budget_usd=cfg.budget.usd_per_session,
    )

    available = record.get("has_content") if isinstance(record.get("has_content"), dict) else {}
    urls = record.get("content_urls") if isinstance(record.get("content_urls"), dict) else {}
    oa = record.get("open_access") if isinstance(record.get("open_access"), dict) else {}
    formats = sorted(k for k, v in available.items() if v)

    base: dict[str, Any] = {
        "id": shaping.short_id(record.get("id")),
        "title": record.get("display_name"),
        "available_formats": formats,
        "oa_url": oa.get("oa_url"),
    }

    if not formats:
        return ok(
            {
                **base,
                "full_text_available": False,
                "note": (
                    "OpenAlex holds no full text for this work. That is common: "
                    "it has content for roughly 54 million works, a fraction of "
                    "the index. If oa_url is present the paper is still readable "
                    "there, and Hermes can fetch it with its own web tools for "
                    "nothing."
                ),
            },
            cfg,
            cost=cost_block(cfg, sess, call_class=pricing.SINGLETON),
        )

    if not args.get("confirm"):
        return ok(
            {
                **base,
                "full_text_available": True,
                "download_cost_usd": pricing.cost_of(pricing.CONTENT),
                "next_step": (
                    "Nothing was downloaded and nothing was spent. Downloading "
                    f"costs ${pricing.cost_of(pricing.CONTENT):.2f}, a hundred "
                    "times a list call. Call again with confirm=true only if "
                    "the user actually asked for the full text. If they just "
                    "want to read it, hand them the oa_url instead."
                ),
            },
            cfg,
            cost=cost_block(cfg, sess, call_class=pricing.SINGLETON),
        )

    wanted = str(args.get("format") or "text").strip().lower()
    url = urls.get("grobid_xml") or urls.get("pdf")
    if wanted == "pdf" or (not urls.get("grobid_xml") and urls.get("pdf")):
        # A PDF is binary and useless to a model, so hand back the link rather
        # than spending $0.01 to deliver bytes it cannot read.
        return ok(
            {
                **base,
                "full_text_available": True,
                "pdf_url": urls.get("pdf"),
                "note": (
                    "Only a PDF is available for this work, and a PDF is binary. "
                    "Nothing was spent. Fetch the pdf_url with Hermes's own "
                    "tools and read it there."
                ),
            },
            cfg,
            cost=cost_block(cfg, sess, call_class=pricing.SINGLETON),
        )

    xml = client.get_content(url, session_id=sess, budget_usd=cfg.budget.usd_per_session)
    text = shaping.tei_to_text(xml)

    return ok(
        {
            **base,
            "source_url": url,
            "characters": len(text),
            "full_text": text,
        },
        cfg,
        cost=cost_block(cfg, sess, call_class=pricing.CONTENT),
    )
