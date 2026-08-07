"""Valid field names, shipped rather than fetched.

OpenAlex will tell you the authoritative list: send a filter it does not
recognise and the 400 response enumerates every valid field for that entity.
That is how this table was built. But doing it at runtime costs $0.0001 per
lookup, and a tool whose whole job is to save the model from a wasted call
should not itself cost money. So the lists live here and the tool is free.

They change rarely. ``hermes openalex fields --refresh`` re-harvests them live
when you want to be certain.

Counts as measured: works 206 filter fields, sources 48, authors 43,
institutions 33, funders 26, publishers 24, topics 16, keywords 9.
"""

from __future__ import annotations

WORK_FILTERS = {
    "identity": ["doi", "ids.pmid", "ids.pmcid", "has_doi", "has_pmid", "openalex_id"],
    "authorship": [
        "authorships.author.id",
        "authorships.author.orcid",
        "authorships.institutions.id",
        "authorships.institutions.ror",
        "authorships.institutions.country_code",
        "authorships.institutions.lineage",
        "authorships.is_corresponding",
        "authors_count",
        "has_orcid",
    ],
    "venue": [
        "primary_location.source.id",
        "primary_location.source.issn",
        "primary_location.source.is_in_doaj",
        "primary_location.source.host_organization",
        "locations.source.id",
        "best_oa_location.license",
    ],
    "time": [
        "publication_year",
        "publication_date",
        "from_publication_date",
        "to_publication_date",
    ],
    "type_and_access": [
        "type",
        "type_crossref",
        "is_oa",
        "open_access.oa_status",
        "has_fulltext",
        "has_abstract",
        "language",
        "version",
    ],
    "impact": [
        "cited_by_count",
        "fwci",
        "citation_normalized_percentile.is_in_top_1_percent",
        "citation_normalized_percentile.is_in_top_10_percent",
        "referenced_works_count",
    ],
    "subject": [
        "primary_topic.id",
        "primary_topic.field.id",
        "primary_topic.subfield.id",
        "primary_topic.domain.id",
        "topics.id",
        "keywords.id",
        "sustainable_development_goals.id",
        "concepts.id",
    ],
    "funding": ["awards.funder_id", "funders.id", "grants.funder"],
    "graph": ["cites", "cited_by", "related_to", "referenced_works"],
    "quality": ["is_retracted", "is_paratext", "has_references"],
    "search": [
        "default.search",
        "title.search",
        "abstract.search",
        "title_and_abstract.search",
        "fulltext.search",
        "keyword.search",
        "raw_affiliation_strings.search",
    ],
}

AUTHOR_FILTERS = {
    "identity": ["orcid", "has_orcid", "openalex_id"],
    "affiliation": [
        "affiliations.institution.id",
        "affiliations.institution.ror",
        "affiliations.institution.country_code",
        "last_known_institutions.id",
        "last_known_institutions.country_code",
    ],
    "impact": ["works_count", "cited_by_count", "summary_stats.h_index", "summary_stats.i10_index"],
    "subject": ["topics.id", "topic_share.id", "x_concepts.id"],
    "search": ["default.search", "display_name.search"],
}

SOURCE_FILTERS = {
    "identity": ["issn", "issn_l", "has_issn", "openalex_id"],
    "nature": ["type", "is_oa", "is_in_doaj", "is_core", "is_preprint_repository", "oa_flip_year"],
    "publisher": ["host_organization", "host_organization_lineage", "country_code"],
    "impact": ["works_count", "cited_by_count", "summary_stats.h_index", "apc_usd"],
    "search": ["default.search", "display_name.search"],
}

INSTITUTION_FILTERS = {
    "identity": ["ror", "has_ror", "openalex_id"],
    "place": ["country_code", "continent", "is_global_south"],
    "nature": ["type", "lineage", "status", "repositories.id", "roles.id"],
    "impact": ["works_count", "cited_by_count", "summary_stats.h_index"],
    "search": ["default.search", "display_name.search"],
}

FILTERS_BY_ENTITY = {
    "works": WORK_FILTERS,
    "authors": AUTHOR_FILTERS,
    "sources": SOURCE_FILTERS,
    "institutions": INSTITUTION_FILTERS,
}

# The select set is NOT the filter set. authors_count is a valid filter and an
# invalid select, which is a 400 waiting to happen if you share one list.
WORK_SELECT = [
    "id",
    "doi",
    "title",
    "display_name",
    "relevance_score",
    "publication_year",
    "publication_date",
    "ids",
    "language",
    "primary_location",
    "sources",
    "type",
    "type_crossref",
    "indexed_in",
    "open_access",
    "authorships",
    "institution_assertions",
    "institutions",
    "countries_distinct_count",
    "institutions_distinct_count",
    "corresponding_author_ids",
    "corresponding_institution_ids",
    "apc_list",
    "apc_paid",
    "fwci",
    "is_authors_truncated",
    "has_fulltext",
    "fulltext_origin",
    "cited_by_count",
    "citation_normalized_percentile",
    "cited_by_percentile_year",
    "biblio",
    "is_retracted",
    "is_paratext",
    "primary_topic",
    "topics",
    "keywords",
    "concepts",
    "mesh",
    "locations_count",
    "locations",
    "best_oa_location",
    "sustainable_development_goals",
    "awards",
    "funders",
    "datasets",
    "versions",
    "has_content",
    "content_urls",
    "referenced_works_count",
    "referenced_works",
    "related_works",
    "abstract_inverted_index",
    "cited_by_api_url",
    "counts_by_year",
    "updated_date",
    "created_date",
]

VOCABULARIES = {
    "work_types": [
        "article",
        "book",
        "book-chapter",
        "dataset",
        "dissertation",
        "editorial",
        "erratum",
        "grant",
        "letter",
        "libguides",
        "other",
        "paratext",
        "peer-review",
        "preprint",
        "reference-entry",
        "report",
        "retraction",
        "review",
        "standard",
        "supplementary-materials",
    ],
    "oa_status": ["diamond", "gold", "green", "hybrid", "bronze", "closed"],
    "source_types": [
        "journal",
        "repository",
        "conference",
        "ebook platform",
        "book series",
        "metadata",
        "other",
    ],
    "institution_types": [
        "education",
        "healthcare",
        "company",
        "archive",
        "nonprofit",
        "government",
        "facility",
        "funder",
        "other",
    ],
    "domains": ["Health Sciences", "Life Sciences", "Physical Sciences", "Social Sciences"],
}

GROUPABLE_HINTS = [
    "publication_year",
    "type",
    "open_access.oa_status",
    "is_oa",
    "language",
    "authorships.institutions.country_code",
    "authorships.institutions.id",
    "authorships.author.id",
    "primary_location.source.id",
    "primary_topic.id",
    "primary_topic.field.id",
    "primary_topic.domain.id",
    "sustainable_development_goals.id",
    "is_retracted",
    "cited_by_count",
]
