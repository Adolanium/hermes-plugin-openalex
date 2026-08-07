"""Tool schemas: what the model reads before deciding to call anything.

OpenAlex charges real money and the spread between call types is a factor of a
hundred. An id lookup is free, a list call is $0.0001, a search is ten times
that, and text classification is a hundred times a list call. On the anonymous
$0.10 daily budget, ten classification calls are the whole day.

So these descriptions do one job above all others: route the model to the
cheapest call that answers the question. The order matters too, because free
tools are described first.
"""

from __future__ import annotations

_ENTITY_ENUM = [
    "works",
    "authors",
    "sources",
    "institutions",
    "topics",
    "publishers",
    "funders",
    "keywords",
]

# --- free -----------------------------------------------------------------

OPENALEX_RESOLVE = {
    "name": "openalex_resolve",
    "description": (
        "Turn a name into an OpenAlex id. FREE, and it keeps working even when "
        "the daily budget is gone.\n\n"
        "Use this first, always. Searching for an author, journal or "
        "institution by name is expensive and imprecise. Resolving the name to "
        "an id and then filtering on that id is free and exact. 'Papers by "
        "Yoshua Bengio' should be resolve then filter, never a text search.\n\n"
        "Returns the id, a disambiguating hint, the work count, and the filter "
        "key to use in the next call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Name or partial name, e.g. 'bengio', 'nature neuro', 'max planck'.",
            },
            "entity": {
                "type": "string",
                "enum": _ENTITY_ENUM,
                "description": "Which kind of thing to resolve. Omit to search across all types.",
            },
        },
        "required": ["query"],
    },
}

OPENALEX_GET = {
    "name": "openalex_get",
    "description": (
        "Fetch one record by identifier. FREE, and it keeps working at zero "
        "budget.\n\n"
        "Accepts an OpenAlex id (W2741809807), a DOI, an ORCID, a ROR, an "
        "ISSN, a PMID, or the URL form of any of those. The plugin normalizes "
        "whatever you pass into the free lookup form, so do not worry about "
        "which shape you have.\n\n"
        "Because it costs nothing, prefer fetching several records by id over "
        "one search that returns them. Ten id lookups are free, one search is "
        "$0.001."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "Identifier, or a comma-separated list of up to 20. "
                    "e.g. 'W2741809807', '10.7717/peerj.4375', "
                    "'orcid:0000-0002-1298-3089'."
                ),
            },
            "entity": {
                "type": "string",
                "enum": _ENTITY_ENUM,
                "description": "Entity type. Usually inferable from the id, defaults to works.",
            },
            "verbosity": {
                "type": "string",
                "enum": ["summary", "detail", "raw"],
                "description": (
                    "'summary' is the default. 'detail' adds the abstract, "
                    "topics and keywords. 'raw' returns everything OpenAlex "
                    "holds and can be very large, so use it only when a "
                    "specific field is missing from the shaped output."
                ),
            },
        },
        "required": ["id"],
    },
}

OPENALEX_ACCOUNT = {
    "name": "openalex_account",
    "description": (
        "Report the OpenAlex budget: the account's daily allowance and what is "
        "left of it, plus this session's spend broken down by call type. Free.\n\n"
        "Call it before starting anything broad, or when a call fails in a way "
        "that might be budget related."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

OPENALEX_FIELDS = {
    "name": "openalex_fields",
    "description": (
        "List the valid filter, sort and select field names for an entity, and "
        "the vocabularies (work types, languages, SDGs, institution types).\n\n"
        "Use it when you are unsure whether a filter exists rather than "
        "guessing: a rejected query still costs $0.0001, and OpenAlex has 206 "
        "filter fields on works alone. Note the filter set and the select set "
        "are not the same, so check the one you need."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "enum": _ENTITY_ENUM,
                "description": "Which entity's fields to list.",
                "default": "works",
            },
            "kind": {
                "type": "string",
                "enum": ["filter", "select", "both"],
                "description": "Which field set. They differ.",
                "default": "both",
            },
        },
        "required": [],
    },
}

# --- cheap ----------------------------------------------------------------

OPENALEX_COUNT = {
    "name": "openalex_count",
    "description": (
        "Count matching records and break them down by any field, for "
        "$0.0001. This is TEN TIMES CHEAPER than openalex_search and returns "
        "roughly five hundred times less data.\n\n"
        "Reach for this first. It answers most real questions outright: how "
        "many papers on a topic, how the count splits by year, which "
        "institutions or journals or countries dominate, how open access "
        "breaks down, which topics an author works on.\n\n"
        "The saving is real and specific: adding a group_by to a search drops "
        "the price from $0.001 to $0.0001 because OpenAlex bills grouped "
        "queries at list price even when a search term is present.\n\n"
        "Only call openalex_search when you genuinely need the individual "
        "records rather than the shape of them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "enum": _ENTITY_ENUM,
                "description": "What to count.",
                "default": "works",
            },
            "filter": {
                "type": "string",
                "description": (
                    "OpenAlex filter expression. Comma is AND, pipe is OR "
                    "within a field, ! negates, > and < compare. e.g. "
                    "'publication_year:>2020,is_oa:true', "
                    "'authorships.author.id:A5023888391'. Load the "
                    "openalex:query-syntax skill for the full filter list."
                ),
            },
            "search": {
                "type": "string",
                "description": (
                    "Free-text search to combine with the grouping. Still "
                    "billed at the cheap list price when group_by is present."
                ),
            },
            "group_by": {
                "type": "string",
                "description": (
                    "Field to break the count down by, e.g. 'publication_year', "
                    "'authorships.institutions.country_code', 'open_access.oa_status', "
                    "'primary_topic.field.id', 'type'. Omit for a bare total. "
                    "Capped at 200 groups by the API."
                ),
            },
        },
        "required": [],
    },
}

OPENALEX_RELATED = {
    "name": "openalex_related",
    "description": (
        "Traverse the citation graph from one work, for $0.0001.\n\n"
        "mode='cited_by' finds works citing this one. mode='references' finds "
        "works it cites. mode='related' finds OpenAlex's own related-works "
        "list.\n\n"
        "This is a plain list call rather than a search, so it is ten times "
        "cheaper than finding the same papers by searching for them. Use it to "
        "walk a literature forwards or backwards from a known paper."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The work to traverse from."},
            "mode": {
                "type": "string",
                "enum": ["cited_by", "references", "related"],
                "description": "Direction of travel.",
                "default": "cited_by",
            },
            "per_page": {"type": "integer", "description": "1 to 200. Default 25.", "default": 25},
            "sort": {
                "type": "string",
                "description": "e.g. 'cited_by_count:desc', 'publication_date:desc'.",
            },
            "verbosity": {"type": "string", "enum": ["summary", "detail", "raw"]},
        },
        "required": ["id"],
    },
}

# --- expensive ------------------------------------------------------------

OPENALEX_SEARCH = {
    "name": "openalex_search",
    "description": (
        "Search OpenAlex and return the matching records.\n\n"
        "COSTS $0.001, ten times a list call and a hundredth of the entire "
        "anonymous daily budget. Before calling it, ask whether you need the "
        "records themselves. If the question is how many, which years, which "
        "institutions, which journals or which topics, openalex_count answers "
        "it for a tenth of the price.\n\n"
        "IMPORTANT: on works, the plain 'search' parameter searches FULL TEXT, "
        "which is far broader than most people expect and returns roughly "
        "twice as many hits as a title and abstract search. For conventional "
        "bibliographic searching set search_field='title_and_abstract'.\n\n"
        "Filtering by an id resolved through openalex_resolve is both free-er "
        "and more precise than searching by name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "enum": _ENTITY_ENUM, "default": "works"},
            "search": {"type": "string", "description": "Free-text query."},
            "search_field": {
                "type": "string",
                "enum": ["default", "title", "abstract", "title_and_abstract", "fulltext"],
                "description": (
                    "Which field to search. 'default' means full text on "
                    "works. Use 'title_and_abstract' for normal bibliographic "
                    "search."
                ),
            },
            "filter": {
                "type": "string",
                "description": "Filter expression, combined with the search.",
            },
            "sort": {
                "type": "string",
                "description": (
                    "'field:desc' or 'field:asc'. The '-field' form documented "
                    "by OpenAlex does not work and is rewritten automatically."
                ),
            },
            "per_page": {"type": "integer", "description": "1 to 200. Default 25.", "default": 25},
            "page": {
                "type": "integer",
                "description": "1-indexed. page times per_page must be under 10000.",
                "default": 1,
            },
            "verbosity": {"type": "string", "enum": ["summary", "detail", "raw"]},
        },
        "required": [],
    },
}

# --- full profile ---------------------------------------------------------

OPENALEX_CLASSIFY = {
    "name": "openalex_classify",
    "description": (
        "Classify arbitrary text into OpenAlex's topic hierarchy.\n\n"
        "COSTS $0.01 PER CALL. That is a hundred times a list call, and ten "
        "calls exhaust the entire anonymous daily budget. It is disabled by "
        "default and must be enabled explicitly in config.\n\n"
        "Before using it, check whether the work already exists in OpenAlex. "
        "If it does, openalex_get returns its topics for free AND more "
        "accurately. Measured: classifying the title and abstract of "
        "'Attention Is All You Need' returned 'Cognitive Science and Education "
        "Research' under Neuroscience, apparently misled by the word "
        "attention, while the indexed record for the same paper says 'Natural "
        "Language Processing Techniques' under Computer Science.\n\n"
        "So this tool is only for text that is genuinely not in OpenAlex, such "
        "as an unpublished abstract or a grant proposal. For anything with a "
        "DOI, resolve and fetch instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Title of the text to classify."},
            "abstract": {"type": "string", "description": "Abstract or body text."},
        },
        "required": [],
    },
}

OPENALEX_HARVEST = {
    "name": "openalex_harvest",
    "description": (
        "Page through a large result set with cursor pagination, up to a "
        "declared maximum number of records.\n\n"
        "Each page is a separate billable call, so the cost is roughly "
        "records divided by per_page, times $0.0001. The tool estimates the "
        "total before starting and refuses if it would exceed the session "
        "budget, rather than stopping halfway through and leaving you with "
        "partial data and a spent wallet.\n\n"
        "For anything above a few hundred thousand records the bulk snapshot "
        "at s3://openalex is free and faster. Use this for the middle ground."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "enum": _ENTITY_ENUM, "default": "works"},
            "filter": {"type": "string", "description": "Filter expression defining the set."},
            "search": {
                "type": "string",
                "description": "Optional search. Makes every page search-priced.",
            },
            "max_records": {
                "type": "integer",
                "description": "Hard ceiling on records retrieved.",
                "default": 200,
            },
            "per_page": {
                "type": "integer",
                "description": "1 to 200. Higher means fewer billed calls.",
                "default": 200,
            },
            "verbosity": {"type": "string", "enum": ["summary", "detail", "raw"]},
        },
        "required": [],
    },
}


ALL_SCHEMAS = {
    "openalex_resolve": OPENALEX_RESOLVE,
    "openalex_get": OPENALEX_GET,
    "openalex_count": OPENALEX_COUNT,
    "openalex_search": OPENALEX_SEARCH,
    "openalex_related": OPENALEX_RELATED,
    "openalex_account": OPENALEX_ACCOUNT,
    "openalex_fields": OPENALEX_FIELDS,
    "openalex_classify": OPENALEX_CLASSIFY,
    "openalex_harvest": OPENALEX_HARVEST,
}

EMOJI = {
    "openalex_resolve": "🔗",
    "openalex_get": "📄",
    "openalex_count": "🧮",
    "openalex_search": "🔎",
    "openalex_related": "🕸️",
    "openalex_account": "💳",
    "openalex_fields": "📖",
    "openalex_classify": "🏷️",
    "openalex_harvest": "🌾",
}
