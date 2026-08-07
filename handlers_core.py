"""The seven core tools. Free first, cheap second, expensive last."""

from __future__ import annotations

from typing import Any

from . import config as config_mod
from . import fields_data, pricing, shaping
from .budget import tracker
from .client import get_client, unwrap_rate_limit
from .errors import AuthError, NotFoundError, OpenAlexError
from .runtime import (
    bad,
    clamp_per_page,
    cost_block,
    entity_for,
    normalize_id,
    normalize_sort,
    ok,
    session_id,
    small,
    tool,
    verbosity_for,
)

# The leading letter of an OpenAlex id tells you which collection it lives in.
_ID_ENTITY = {
    "W": "works",
    "A": "authors",
    "S": "sources",
    "I": "institutions",
    "T": "topics",
    "P": "publishers",
    "F": "funders",
    "K": "keywords",
    "C": "concepts",
}

# Which external identifier belongs to which entity, so a DOI does not get
# looked up under /authors.
_PREFIX_ENTITY = {
    "doi": "works",
    "pmid": "works",
    "pmcid": "works",
    "mag": "works",
    "orcid": "authors",
    "ror": "institutions",
    "issn": "sources",
}


def _infer_entity(identifier: str, fallback: str) -> str:
    text = identifier.strip()
    if ":" in text:
        prefix = text.split(":", 1)[0].lower()
        if prefix in _PREFIX_ENTITY:
            return _PREFIX_ENTITY[prefix]
        if prefix == "openalex":
            text = text.split(":", 1)[1]
    if text and text[0].upper() in _ID_ENTITY and text[1:].isdigit():
        return _ID_ENTITY[text[0].upper()]
    return fallback


# --- openalex_resolve (free) ----------------------------------------------


@tool
def openalex_resolve(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)
    query = str(args.get("query") or "").strip()
    if not query:
        return bad("No query given.")

    entity = str(args.get("entity") or "").strip().lower()
    path = f"autocomplete/{entity}" if entity in config_mod.ENTITIES else "autocomplete"

    client = get_client(cfg)
    raw = client.get(
        path,
        params={"q": query},
        session_id=sess,
        budget_usd=cfg.budget.usd_per_session,
        call_class=pricing.SINGLETON,
    )

    matches = []
    for row in (raw.get("results") or [])[:10]:
        if not isinstance(row, dict):
            continue
        matches.append(
            {
                k: v
                for k, v in {
                    "id": shaping.short_id(row.get("id")),
                    "name": row.get("display_name"),
                    "type": row.get("entity_type"),
                    "hint": row.get("hint"),
                    "works_count": row.get("works_count"),
                    "cited_by_count": row.get("cited_by_count"),
                    "filter_key": row.get("filter_key"),
                    "external_id": row.get("external_id"),
                }.items()
                if v not in (None, "", [], {})
            }
        )

    return ok(
        {
            "query": query,
            "matches": matches,
            "cost": "free",
            "next_step": (
                "Filter on the id rather than searching the name. For example "
                "openalex_count(filter='authorships.author.id:<id>', "
                "group_by='publication_year'). The filter_key field names the "
                "filter to use."
            )
            if matches
            else "No match. Try a shorter or differently spelled query.",
        },
        cfg,
    )


# --- openalex_get (free) ---------------------------------------------------


@tool
def openalex_get(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)
    verbosity = verbosity_for(args, cfg)

    raw_ids = [part.strip() for part in str(args.get("id") or "").split(",") if part.strip()]
    if not raw_ids:
        return bad("No identifier given.")
    raw_ids = raw_ids[:20]

    fallback = entity_for(args)
    client = get_client(cfg)
    records: list[dict[str, Any]] = []
    missing: list[str] = []

    for original in raw_ids:
        identifier = normalize_id(original)
        entity = _infer_entity(identifier, fallback)
        select = shaping.select_for(entity, verbosity)
        try:
            record = client.get(
                f"{entity}/{identifier}",
                params={"select": select} if select else {},
                session_id=sess,
                budget_usd=cfg.budget.usd_per_session,
            )
        except NotFoundError:
            missing.append(original)
            continue
        shaped = shaping.shape_entity(entity, record, verbosity)
        if original != identifier:
            shaped["queried_as"] = original
        records.append(shaped)

    payload: dict[str, Any] = {}
    if len(records) == 1 and not missing:
        payload["record"] = records[0]
    else:
        payload["records"] = records
        payload["found"] = len(records)
    if missing:
        payload["not_found"] = missing
        payload["note"] = (
            "No record for these. Check the identifier form: OpenAlex needs a "
            "prefix such as 'doi:' on an external id, and a bare DOI returns "
            "nothing."
        )

    return ok(payload, cfg, cost=cost_block(cfg, sess, call_class=pricing.SINGLETON))


# --- openalex_count (cheap) ------------------------------------------------


def _search_params(args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Split a search request into API params plus any filter fragment.

    OpenAlex exposes searching two ways and they mean different things. The
    bare ``search`` parameter maps to full text on works, which is roughly
    twice as broad as most callers expect. The ``*.search:`` filters are how
    you ask for the conventional title-and-abstract behaviour.
    """
    search = str(args.get("search") or "").strip()
    if not search:
        return {}, ""
    field = str(args.get("search_field") or "default").strip().lower()
    if field in ("", "default", "fulltext"):
        return {"search": search}, ""
    if field in ("title", "abstract", "title_and_abstract"):
        # Commas and pipes are filter syntax, so they cannot appear raw here.
        cleaned = search.replace(",", " ").replace("|", " ")
        return {}, f"{field}.search:{cleaned}"
    return {"search": search}, ""


def _merge_filter(base: str, extra: str) -> str:
    parts = [part for part in (base.strip(), extra.strip()) if part]
    return ",".join(parts)


@tool
def openalex_count(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)
    entity = entity_for(args)

    search_params, search_filter = _search_params(args)
    filter_expr = _merge_filter(str(args.get("filter") or ""), search_filter)
    group_by = str(args.get("group_by") or "").strip()

    params: dict[str, Any] = {**search_params}
    if filter_expr:
        params["filter"] = filter_expr
    if group_by:
        params["group_by"] = group_by
    # A count never needs the records themselves, and one selected field keeps
    # the response tiny even when group_by is absent.
    params["per_page"] = 1
    params["select"] = "id"

    client = get_client(cfg)
    raw = client.get(
        entity,
        params=params,
        cacheable=False,
        session_id=sess,
        budget_usd=cfg.budget.usd_per_session,
    )

    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    payload: dict[str, Any] = {
        "entity": entity,
        "total": meta.get("count"),
        "filter": filter_expr or None,
        "search": search_params.get("search") or (search_filter or None),
    }
    if group_by:
        groups = shaping.shape_groups(raw.get("group_by"))
        payload["group_by"] = group_by
        payload["groups"] = groups
        if meta.get("groups_count") == 200:
            payload["groups_truncated"] = (
                "OpenAlex caps group_by at 200 groups, so this is the head of "
                "the distribution rather than all of it. Narrow the filter to "
                "see the tail."
            )
    payload = {k: v for k, v in payload.items() if v is not None}

    return ok(payload, cfg, cost=cost_block(cfg, sess, call_class=pricing.LIST))


# --- openalex_search (expensive) -------------------------------------------


@tool
def openalex_search(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)
    entity = entity_for(args)
    verbosity = verbosity_for(args, cfg)

    search_params, search_filter = _search_params(args)
    filter_expr = _merge_filter(str(args.get("filter") or ""), search_filter)
    if not search_params and not filter_expr:
        return bad(
            "Give a search term or a filter.",
            "An unbounded list of every work in OpenAlex is not a useful answer.",
        )

    per_page = clamp_per_page(args.get("per_page"), 25)
    page = max(1, int(args.get("page") or 1))
    if page * per_page > 10_000:
        return bad(
            f"page {page} at per_page {per_page} exceeds OpenAlex's 10,000 result ceiling.",
            "Narrow the filter, or use openalex_harvest which pages with a cursor.",
        )

    params: dict[str, Any] = {**search_params, "per_page": per_page, "page": page}
    if filter_expr:
        params["filter"] = filter_expr
    sort = normalize_sort(args.get("sort"))
    if sort:
        params["sort"] = sort
    select = shaping.select_for(entity, verbosity)
    if select:
        params["select"] = select

    client = get_client(cfg)
    raw = client.get(
        entity,
        params=params,
        cacheable=False,
        session_id=sess,
        budget_usd=cfg.budget.usd_per_session,
    )

    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    results = [
        shaping.shape_entity(entity, row, verbosity)
        for row in (raw.get("results") or [])
        if isinstance(row, dict)
    ]

    payload: dict[str, Any] = {
        "entity": entity,
        "total": meta.get("count"),
        "page": page,
        "returned": len(results),
        "results": results,
    }
    if isinstance(meta.get("count"), int) and meta["count"] > len(results):
        payload["more_available"] = True
        payload["pagination_hint"] = (
            "More results exist and each page is billed again. If you only "
            "need the shape of the set, openalex_count with a group_by costs a "
            "tenth as much."
        )

    call_class = pricing.classify_request(entity, params)
    return ok(payload, cfg, cost=cost_block(cfg, sess, call_class=call_class))


# --- openalex_related (cheap) ----------------------------------------------

_RELATED_FILTERS = {"cited_by": "cites", "references": "cited_by", "related": "related_to"}


@tool
def openalex_related(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)
    verbosity = verbosity_for(args, cfg)

    identifier = normalize_id(str(args.get("id") or ""))
    if not identifier:
        return bad("No work identifier given.")

    mode = str(args.get("mode") or "cited_by").strip().lower()
    filter_field = _RELATED_FILTERS.get(mode)
    if not filter_field:
        return bad(f"Unknown mode {mode!r}. Use cited_by, references or related.")

    per_page = clamp_per_page(args.get("per_page"), 25)
    params: dict[str, Any] = {
        "filter": f"{filter_field}:{identifier}",
        "per_page": per_page,
    }
    sort = normalize_sort(args.get("sort"))
    if sort:
        params["sort"] = sort
    select = shaping.select_for("works", verbosity)
    if select:
        params["select"] = select

    client = get_client(cfg)
    raw = client.get(
        "works",
        params=params,
        session_id=sess,
        budget_usd=cfg.budget.usd_per_session,
    )

    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    results = [
        shaping.shape_work(row, verbosity)
        for row in (raw.get("results") or [])
        if isinstance(row, dict)
    ]
    return ok(
        {
            "of": identifier,
            "mode": mode,
            "total": meta.get("count"),
            "returned": len(results),
            "results": results,
        },
        cfg,
        cost=cost_block(cfg, sess, call_class=pricing.LIST),
    )


# --- openalex_account (free) -----------------------------------------------


@tool
def openalex_account(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    sess = session_id(kwargs)

    payload: dict[str, Any] = {
        "api_key_configured": cfg.has_key,
        "api_key_env": cfg.api_key_env,
        "profile": cfg.profile,
        "verbosity": cfg.verbosity,
        "prices_usd": pricing.snapshot(),
        "session": tracker.ledger(sess).snapshot(cfg.budget.usd_per_session),
    }

    client = get_client(cfg)
    payload["observed"] = client.meter.snapshot()
    payload["cache"] = client.cache.stats()

    if not cfg.has_key:
        payload["daily_budget_usd"] = pricing.ANON_DAILY_USD
        payload["note"] = (
            "No API key, so the anonymous budget is $0.10/day. A free key from "
            "https://openalex.org/settings/api raises it to $1.00/day. Id "
            "lookups and name resolution cost nothing either way."
        )
        return small(payload, cfg)

    try:
        # /rate-limit reports the live budget without spending anything, and
        # carries the authoritative price table.
        raw_info = client.get(
            "rate-limit", cacheable=False, session_id=sess, call_class=pricing.SINGLETON
        )
        info = unwrap_rate_limit(raw_info)
        payload["account"] = {
            k: v
            for k, v in {
                "daily_budget_usd": info.get("daily_budget_usd"),
                "daily_used_usd": info.get("daily_used_usd"),
                "daily_remaining_usd": info.get("daily_remaining_usd"),
                "prepaid_balance_usd": info.get("prepaid_balance_usd"),
                "prepaid_remaining_usd": info.get("prepaid_remaining_usd"),
                "resets_at": info.get("resets_at"),
                "resets_in_seconds": info.get("resets_in_seconds"),
            }.items()
            if v is not None
        }
        if pricing.refresh_from_account(info.get("endpoint_costs_usd")):
            payload["prices_usd"] = pricing.snapshot()
            payload["prices_note"] = "Price table refreshed from the live account."
    except AuthError:
        payload["account_error"] = (
            "The configured key was rejected. OpenAlex fails closed rather "
            "than falling back to anonymous, so every billable call will fail "
            "until it is fixed."
        )
    except OpenAlexError as exc:
        payload["account_error"] = exc.message

    return small(payload, cfg)


# --- openalex_fields (free, no API call) -----------------------------------


@tool
def openalex_fields(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    cfg = config_mod.load()
    entity = entity_for(args)
    kind = str(args.get("kind") or "both").strip().lower()

    payload: dict[str, Any] = {"entity": entity, "cost": "free, served locally"}

    if kind in ("filter", "both"):
        filters = fields_data.FILTERS_BY_ENTITY.get(entity)
        if filters:
            payload["filter_fields"] = filters
        else:
            payload["filter_fields"] = {
                "common": ["display_name.search", "works_count", "cited_by_count", "openalex_id"]
            }
        payload["groupable_examples"] = fields_data.GROUPABLE_HINTS

    if kind in ("select", "both") and entity == "works":
        payload["select_fields"] = fields_data.WORK_SELECT
        payload["select_note"] = (
            "select accepts top-level fields only, so nested paths like "
            "authorships.author.id are rejected. The select set and the filter "
            "set are different: authors_count is a valid filter and an invalid "
            "select."
        )

    payload["vocabularies"] = fields_data.VOCABULARIES
    payload["syntax"] = {
        "AND": "comma between filters",
        "OR": "pipe within one field, maximum 100 values",
        "NOT": "! before the value",
        "range": "publication_year:2020-2024, or cited_by_count:>100",
        "sort": "field:desc or field:asc. The -field form does not work.",
    }
    return small(payload, cfg)
