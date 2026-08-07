"""Turning OpenAlex records into something a model can read.

Two levers, and they work at different layers.

The first is ``select``, which happens server-side. A default 25-result page of
works is about 1.5 MB. The same page with five selected fields is 6.5 KB. That
is a factor of 233, and it costs nothing extra because OpenAlex prices by call
shape rather than by bytes. The catch is that ``select`` only accepts top-level
fields, so it cannot reach inside ``authorships`` to prune it.

The second is this module, which handles what ``select`` cannot. A single work
by a large collaboration ran to 2.88 MB, of which 93% was one author list. No
amount of field selection fixes that, because you either take ``authorships``
or you lose the authors entirely. So we take it and trim it here.

Two smaller wins worth naming. ``concepts`` is deprecated in favour of
``topics`` and still ships in every work at roughly a tenth of the payload, so
it is dropped unconditionally. And an abstract arrives as an inverted index,
a term-to-positions map that is twice the size of the prose it encodes, so
reconstructing it both halves the bytes and spares the model a puzzle.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

# Fields worth having in a summary, chosen so a work stays readable at a few
# hundred bytes. Passed to the API as ``select``.
WORK_SELECT_SUMMARY = [
    "id",
    "doi",
    "display_name",
    "publication_year",
    "type",
    "cited_by_count",
    "authorships",
    "primary_location",
    "open_access",
    "primary_topic",
    "is_retracted",
]

WORK_SELECT_DETAIL = WORK_SELECT_SUMMARY + [
    "abstract_inverted_index",
    "publication_date",
    "topics",
    "keywords",
    "referenced_works_count",
    "fwci",
    "biblio",
    "best_oa_location",
    "language",
    "sustainable_development_goals",
    "ids",
]

# For these entities three fields are around 95% of the payload and none of
# them belong in a summary.
_ENTITY_HEAVY = ("topics", "topic_share", "counts_by_year", "x_concepts", "concepts")

AUTHOR_SELECT = [
    "id",
    "orcid",
    "display_name",
    "display_name_alternatives",
    "works_count",
    "cited_by_count",
    "summary_stats",
    "last_known_institutions",
    "affiliations",
]

SOURCE_SELECT = [
    "id",
    "issn_l",
    "display_name",
    "type",
    "publisher",
    "host_organization_name",
    "is_oa",
    "is_in_doaj",
    "works_count",
    "cited_by_count",
    "summary_stats",
    "apc_usd",
    "country_code",
]

INSTITUTION_SELECT = [
    "id",
    "ror",
    "display_name",
    "country_code",
    "type",
    "homepage_url",
    "works_count",
    "cited_by_count",
    "summary_stats",
    "geo",
]

_SELECTS = {
    "works": (WORK_SELECT_SUMMARY, WORK_SELECT_DETAIL),
    "authors": (AUTHOR_SELECT, AUTHOR_SELECT),
    "sources": (SOURCE_SELECT, SOURCE_SELECT),
    "institutions": (INSTITUTION_SELECT, INSTITUTION_SELECT),
}

_MAX_AUTHORS_SUMMARY = 10
_MAX_AUTHORS_DETAIL = 25
_ABSTRACT_CHARS = 1500


def select_for(entity: str, verbosity: str) -> str | None:
    """The ``select`` value to send, or None at raw verbosity.

    Returning None for raw is deliberate: raw means "whatever OpenAlex holds",
    and a select list would quietly define that for the caller.
    """
    if verbosity == "raw":
        return None
    pair = _SELECTS.get(entity)
    if not pair:
        return None
    summary, detail = pair
    return ",".join(detail if verbosity == "detail" else summary)


# --- small helpers ---------------------------------------------------------


def _clean(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}


def _truncate(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def short_id(value: Any) -> str | None:
    """Turn ``https://openalex.org/W2741809807`` into ``W2741809807``.

    The full URL is what the API returns and what group_by keys look like, but
    the bare id is what every follow-up call wants, and it is a third the
    length.
    """
    text = str(value or "").strip()
    if not text:
        return None
    return text.rsplit("/", 1)[-1] or text


def reconstruct_abstract(inverted: Any, limit: int = _ABSTRACT_CHARS) -> str | None:
    """Rebuild prose from OpenAlex's inverted index.

    The index maps each term to the positions it occupies, so inverting it
    recovers the original word order. Positions can have gaps, which is why
    this walks the sorted keys rather than a range.
    """
    if not isinstance(inverted, dict) or not inverted:
        return None
    positions: dict[int, str] = {}
    for term, indexes in inverted.items():
        if not isinstance(indexes, (list, tuple)):
            continue
        for index in indexes:
            if isinstance(index, int):
                positions[index] = term
    if not positions:
        return None
    return _truncate(" ".join(positions[i] for i in sorted(positions)), limit)


def _authors(authorships: Any, verbosity: str) -> tuple[list[dict[str, Any]], int, bool]:
    """Compress the author list, which is the single biggest field in a work."""
    if not isinstance(authorships, list):
        return [], 0, False
    cap = _MAX_AUTHORS_DETAIL if verbosity == "detail" else _MAX_AUTHORS_SUMMARY
    rows: list[dict[str, Any]] = []
    for entry in authorships[:cap]:
        if not isinstance(entry, dict):
            continue
        author = entry.get("author") if isinstance(entry.get("author"), dict) else {}
        institutions = [
            inst.get("display_name")
            for inst in (entry.get("institutions") or [])
            if isinstance(inst, dict) and inst.get("display_name")
        ]
        rows.append(
            _clean(
                {
                    "name": author.get("display_name"),
                    "id": short_id(author.get("id")),
                    "orcid": short_id(author.get("orcid")) if author.get("orcid") else None,
                    "institutions": institutions[:2],
                    "corresponding": entry.get("is_corresponding") or None,
                }
            )
        )
    return rows, len(authorships), len(authorships) > cap


# --- works -----------------------------------------------------------------


def shape_work(raw: dict[str, Any], verbosity: str = "summary") -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    if verbosity == "raw":
        return strip_heavy(raw)

    authors, author_count, truncated = _authors(raw.get("authorships"), verbosity)
    location = raw.get("primary_location") if isinstance(raw.get("primary_location"), dict) else {}
    source = location.get("source") if isinstance(location.get("source"), dict) else {}
    oa = raw.get("open_access") if isinstance(raw.get("open_access"), dict) else {}
    topic = raw.get("primary_topic") if isinstance(raw.get("primary_topic"), dict) else {}

    shaped = _clean(
        {
            "id": short_id(raw.get("id")),
            "doi": (raw.get("doi") or "").replace("https://doi.org/", "") or None,
            "title": raw.get("display_name") or raw.get("title"),
            "year": raw.get("publication_year"),
            "date": raw.get("publication_date"),
            "type": raw.get("type"),
            "cited_by_count": raw.get("cited_by_count"),
            "fwci": raw.get("fwci"),
            "is_retracted": raw.get("is_retracted") or None,
            "authors": authors,
            "author_count": author_count or None,
            "more_authors": truncated or None,
            "venue": source.get("display_name"),
            "venue_id": short_id(source.get("id")) if source.get("id") else None,
            "open_access": oa.get("oa_status"),
            "oa_url": oa.get("oa_url"),
            "topic": topic.get("display_name"),
            "field": (topic.get("field") or {}).get("display_name")
            if isinstance(topic.get("field"), dict)
            else None,
        }
    )

    if verbosity == "detail":
        abstract = reconstruct_abstract(raw.get("abstract_inverted_index"))
        shaped.update(
            _clean(
                {
                    "abstract": abstract,
                    "language": raw.get("language"),
                    "references_count": raw.get("referenced_works_count"),
                    "topics": [
                        t.get("display_name")
                        for t in (raw.get("topics") or [])
                        if isinstance(t, dict) and t.get("display_name")
                    ][:5],
                    "keywords": [
                        k.get("display_name")
                        for k in (raw.get("keywords") or [])
                        if isinstance(k, dict) and k.get("display_name")
                    ][:8],
                    "sdgs": [
                        s.get("display_name")
                        for s in (raw.get("sustainable_development_goals") or [])
                        if isinstance(s, dict) and s.get("display_name")
                    ][:3],
                }
            )
        )
    return shaped


def shape_author(raw: dict[str, Any], verbosity: str = "summary") -> dict[str, Any]:
    if verbosity == "raw":
        return strip_heavy(raw)
    stats = raw.get("summary_stats") if isinstance(raw.get("summary_stats"), dict) else {}
    last_known = [
        inst.get("display_name")
        for inst in (raw.get("last_known_institutions") or [])
        if isinstance(inst, dict) and inst.get("display_name")
    ]
    return _clean(
        {
            "id": short_id(raw.get("id")),
            "orcid": short_id(raw.get("orcid")) if raw.get("orcid") else None,
            "name": raw.get("display_name"),
            "works_count": raw.get("works_count"),
            "cited_by_count": raw.get("cited_by_count"),
            "h_index": stats.get("h_index"),
            "i10_index": stats.get("i10_index"),
            "institutions": last_known[:3],
        }
    )


def shape_source(raw: dict[str, Any], verbosity: str = "summary") -> dict[str, Any]:
    if verbosity == "raw":
        return strip_heavy(raw)
    stats = raw.get("summary_stats") if isinstance(raw.get("summary_stats"), dict) else {}
    return _clean(
        {
            "id": short_id(raw.get("id")),
            "issn_l": raw.get("issn_l"),
            "name": raw.get("display_name"),
            "type": raw.get("type"),
            "publisher": raw.get("host_organization_name") or raw.get("publisher"),
            "is_oa": raw.get("is_oa"),
            "in_doaj": raw.get("is_in_doaj"),
            "works_count": raw.get("works_count"),
            "h_index": stats.get("h_index"),
            "apc_usd": raw.get("apc_usd"),
            "country": raw.get("country_code"),
        }
    )


def shape_institution(raw: dict[str, Any], verbosity: str = "summary") -> dict[str, Any]:
    if verbosity == "raw":
        return strip_heavy(raw)
    stats = raw.get("summary_stats") if isinstance(raw.get("summary_stats"), dict) else {}
    geo = raw.get("geo") if isinstance(raw.get("geo"), dict) else {}
    return _clean(
        {
            "id": short_id(raw.get("id")),
            "ror": short_id(raw.get("ror")) if raw.get("ror") else None,
            "name": raw.get("display_name"),
            "type": raw.get("type"),
            "country": raw.get("country_code"),
            "city": geo.get("city"),
            "works_count": raw.get("works_count"),
            "cited_by_count": raw.get("cited_by_count"),
            "h_index": stats.get("h_index"),
            "homepage": raw.get("homepage_url"),
        }
    )


def shape_generic(raw: dict[str, Any], verbosity: str = "summary") -> dict[str, Any]:
    """Fallback for topics, keywords, publishers and funders."""
    if verbosity == "raw":
        return strip_heavy(raw)
    return _clean(
        {
            "id": short_id(raw.get("id")),
            "name": raw.get("display_name"),
            "description": _truncate(raw.get("description") or "", 200) or None,
            "works_count": raw.get("works_count"),
            "cited_by_count": raw.get("cited_by_count"),
        }
    )


_SHAPERS = {
    "works": shape_work,
    "authors": shape_author,
    "sources": shape_source,
    "institutions": shape_institution,
}


def shape_entity(entity: str, raw: dict[str, Any], verbosity: str = "summary") -> dict[str, Any]:
    return _SHAPERS.get(entity, shape_generic)(raw, verbosity)


# --- aggregations ----------------------------------------------------------


def shape_groups(groups: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Flatten a group_by response.

    ``key`` is usually a full URI while ``key_display_name`` is the label, so
    both are kept: the label to read, the short id to filter on next.
    """
    if not isinstance(groups, list):
        return []
    rows = []
    for group in groups[:limit]:
        if not isinstance(group, dict):
            continue
        key = group.get("key")
        rows.append(
            _clean(
                {
                    "value": group.get("key_display_name") or key,
                    "id": short_id(key) if str(key or "").startswith("http") else None,
                    "count": group.get("count"),
                }
            )
        )
    return rows


# --- heavy-field stripping and budget fitting ------------------------------

_DROP_ALWAYS = ("concepts", "x_concepts")
_DROP_HEAVY = (
    "abstract_inverted_index",
    "referenced_works",
    "related_works",
    "counts_by_year",
    "topic_share",
    "locations",
    "cited_by_percentile_year",
    "mesh",
    "apc_list",
    "apc_paid",
    "indexed_in",
    "datasets",
    "versions",
)


def strip_heavy(obj: Any) -> Any:
    """Drop the fields that are large and rarely worth their weight.

    Applies even at raw verbosity. ``concepts`` goes because OpenAlex
    deprecated it in favour of ``topics`` and it is about a tenth of every
    work. Whatever is removed is announced through ``_omitted`` so nobody
    wonders whether the record really had no references.
    """
    if isinstance(obj, list):
        return [strip_heavy(item) for item in obj]
    if not isinstance(obj, dict):
        return obj

    out: dict[str, Any] = {}
    omitted: list[str] = []
    for key, value in obj.items():
        if key in _DROP_ALWAYS:
            omitted.append(f"{key} (deprecated, superseded by topics)")
            continue
        if key in _DROP_HEAVY:
            if isinstance(value, list):
                out[f"{key}_count"] = len(value)
            omitted.append(key)
            continue
        out[key] = strip_heavy(value)

    if omitted:
        out["_omitted"] = sorted(set(omitted))
    return out


_CONTAINER_KEYS = ("results", "works", "authors", "groups", "matches", "items")


def _find_containers(payload: Any) -> list[tuple]:
    found: list[tuple] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _CONTAINER_KEYS and isinstance(value, list):
                    found.append((node, key, value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    found.sort(key=lambda entry: len(entry[2]), reverse=True)
    return found


def _walk_records(payload: Any) -> Iterable[dict[str, Any]]:
    for _parent, _key, items in _find_containers(payload):
        for item in items:
            if isinstance(item, dict):
                yield item


def fit(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Shrink a payload until it fits, degrading in a deliberate order.

    Prose goes before facts, detail before identity, and the tail of a result
    list before its head. Truncating the serialized JSON is never an option:
    a model handed a half-closed object either fails to parse it or silently
    misreads the fragment.
    """
    if max_chars <= 0:
        return payload

    def size(obj: Any) -> int:
        return len(json.dumps(obj, default=str))

    if size(payload) <= max_chars:
        return payload

    trimmed = json.loads(json.dumps(payload, default=str))
    notes: list[str] = []

    # 1. Abstracts. The most expensive prose and the easiest to re-fetch.
    for record in _walk_records(trimmed):
        record.pop("abstract", None)
    notes.append("abstracts dropped")
    if size(trimmed) <= max_chars:
        return _finish(trimmed, notes)

    # 2. Author lists past the first few, and secondary classification.
    for record in _walk_records(trimmed):
        if isinstance(record.get("authors"), list) and len(record["authors"]) > 3:
            record["authors"] = record["authors"][:3]
            record["more_authors"] = True
        for key in ("keywords", "topics", "sdgs"):
            record.pop(key, None)
    notes.append("author lists capped at 3, keywords and topics dropped")
    if size(trimmed) <= max_chars:
        return _finish(trimmed, notes)

    # 3. Fewer records, halving the longest list each round. Dropped counts are
    #    accumulated and reported once at the end: appending a note per round
    #    per container grows the payload while trying to shrink it, which on a
    #    tight budget can cost more than the records being removed.
    containers = _find_containers(trimmed)
    dropped_by_key: dict[str, int] = {}
    while size(trimmed) > max_chars and any(len(items) > 1 for _, _, items in containers):
        progressed = False
        for _parent, key, items in containers:
            if len(items) <= 1:
                continue
            keep = max(1, len(items) // 2)
            dropped_by_key[key] = dropped_by_key.get(key, 0) + (len(items) - keep)
            del items[keep:]
            progressed = True
        if not progressed:
            break
    for key, dropped in dropped_by_key.items():
        notes.append(f"{dropped} {key} omitted to fit the size budget")
    if size(trimmed) <= max_chars:
        return _finish(trimmed, notes)

    # 4. Last resort. Empty the collections rather than return unparseable JSON.
    for parent, key, items in _find_containers(trimmed):
        if items:
            notes.append(f"all {len(items)} {key} dropped, the payload could not be fitted")
            parent[key] = []
    if size(trimmed) > max_chars:
        notes.append(
            "result still exceeds the size budget. Narrow the query, lower "
            "per_page, or raise plugins.entries.openalex.max_result_chars"
        )
    return _finish(trimmed, notes)


def _finish(payload: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    if notes:
        # Deduplicate while preserving order. The same degradation can be
        # recorded more than once when several containers are trimmed.
        payload["_truncation"] = list(dict.fromkeys(notes))
    return payload


def envelope(
    data: dict[str, Any],
    *,
    max_chars: int,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, **data}
    if cost:
        payload["cost"] = cost
    return fit(payload, max_chars)
