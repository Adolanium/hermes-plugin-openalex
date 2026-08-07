---
name: query-syntax
description: OpenAlex filters, grouping and cost-aware querying
version: 0.1.0
author: hermes-plugin-openalex
license: MIT
metadata:
  hermes:
    tags: [openalex, research, literature, bibliometrics, search, filters]
---

# OpenAlex query syntax

Load this before writing any non-obvious OpenAlex query. Two reasons: the
filter vocabulary is large (206 fields on works alone), and a rejected query
still costs money.

## The cost model, first

OpenAlex prices by the shape of the call, not the size of the response. The
spread is a factor of a hundred, so picking the right call type matters more
than anything else in this document.

| Call | Cost | Notes |
|---|---|---|
| Fetch by id, prefixed form | **free** | Works even at zero budget |
| Autocomplete | **free** | Works even at zero budget |
| List or filter | $0.0001 | |
| **Count with group_by** | **$0.0001** | Cheap even with a search term |
| Search | $0.001 | Ten times a list call |
| Text classification | $0.01 | A hundred times a list call |

The daily budget is $0.10 anonymous, $1.00 with a free key, and it resets at
midnight UTC. So the anonymous tier is 100 searches a day, or 1,000 counts, or
unlimited id lookups.

**The single most important habit:** adding `group_by` to a query drops it from
search price to list price, even when a search term is present. If you want
counts rather than records, that is a 10x saving and roughly 500x less data.

## The order to work in

1. **Resolve names to ids.** `openalex_resolve` is free. Never filter on
   `display_name` when you can filter on an id.
2. **Count before you search.** `openalex_count` answers how many, which years,
   which institutions, which journals, which topics, for a tenth of the price.
3. **Search only for the records themselves**, and only once the count told you
   the set is the right size.
4. **Fetch specific papers by id**, which is free, rather than searching for
   ones you can already name.

## Filter syntax

- `,` between filters is AND: `publication_year:2024,is_oa:true`
- `|` within one field is OR, maximum 100 values: `type:article|book`
- `!` negates: `type:!article`
- `>` and `<` compare: `cited_by_count:>100`
- Ranges use a hyphen: `publication_year:2020-2024`
- Dates use prefixes: `from_publication_date:2024-01-01,to_publication_date:2024-06-30`
- Nested attributes use dots: `authorships.institutions.ror:04dkp9463`

Sorting is `field:desc` or `field:asc`. **The `-field` form that OpenAlex's own
API description advertises does not work** and returns 400. The plugin rewrites
it for you, but do not rely on that elsewhere.

## The search trap

On works, the plain `search` parameter searches **full text**, not title and
abstract. That is roughly twice as broad as most people expect:

| Search field | Hits for "neural" |
|---|---|
| `title` | 871,839 |
| `abstract` | 2,526,344 |
| `title_and_abstract` | 2,731,646 |
| `fulltext` (the default) | 5,797,071 |

For conventional bibliographic searching, pass
`search_field="title_and_abstract"`. All variants cost the same $0.001, so
there is no cheap back door through a filter.

## Filters worth knowing

**Works, identity and provenance**
`doi`, `ids.pmid`, `has_doi`, `is_retracted`, `is_paratext`, `language`,
`type` (article, preprint, book-chapter, dataset, review, ...)

**Works, people and places**
`authorships.author.id`, `authorships.author.orcid`,
`authorships.institutions.id`, `authorships.institutions.ror`,
`authorships.institutions.country_code`, `authorships.is_corresponding`,
`authors_count`

**Works, time**
`publication_year`, `from_publication_date`, `to_publication_date`

**Works, access**
`is_oa`, `open_access.oa_status` (diamond, gold, green, hybrid, bronze,
closed), `has_fulltext`, `best_oa_location.license`

**Works, impact**
`cited_by_count`, `fwci`, `citation_normalized_percentile.is_in_top_1_percent`

**Works, subject**
`primary_topic.id`, `primary_topic.field.id`, `topics.id`, `keywords.id`,
`sustainable_development_goals.id`

**Works, citation graph**
`cites:W...` (works this one cites), `cited_by:W...` (works citing it),
`related_to:W...`. The `openalex_related` tool wraps these.

**Authors** `orcid`, `affiliations.institution.ror`, `works_count`,
`summary_stats.h_index`

**Sources** `issn`, `type`, `is_oa`, `is_in_doaj`, `apc_usd`,
`host_organization`

**Institutions** `ror`, `country_code`, `type`, `is_global_south`

`openalex_fields` returns the full list for any entity, free and with no API
call. `references/cookbook.md` has worked queries.

## Grouping

`group_by` returns counts per value instead of records. Useful fields:
`publication_year`, `type`, `open_access.oa_status`,
`authorships.institutions.country_code`, `authorships.institutions.id`,
`primary_topic.field.id`, `primary_location.source.id`,
`sustainable_development_goals.id`.

**The API caps group_by at 200 groups.** For a high-cardinality field like
`publication_year` across all of history, or `topics.id`, you get the head of
the distribution rather than the whole thing. The plugin says so when it
happens. Narrow the filter if you need the tail.

## Identifiers

Always use the prefixed form. It is free, and the URL form is billed:

| Form | Cost |
|---|---|
| `W2741809807` | free |
| `doi:10.7717/peerj.4375` | free |
| `orcid:0000-0002-1298-3089` | free |
| `https://doi.org/10.7717/peerj.4375` | $0.0001 |
| `10.7717/peerj.4375` (no prefix) | 404 |

The plugin normalizes whatever you pass into the free form, so this mostly
matters when reading OpenAlex data directly.

Prefixes: `openalex:` `doi:` `pmid:` `pmcid:` `mag:` `orcid:` `ror:` `issn:`.
Id letters: `W` work, `A` author, `S` source, `I` institution, `T` topic,
`P` publisher, `F` funder.

## Things that will bite you

- **`concepts` is deprecated.** Use `topics` and `primary_topic`. The plugin
  strips concepts from every response because it is a tenth of a work's payload
  and superseded.
- **Abstracts arrive inverted**, as a term-to-positions map rather than prose.
  The plugin reconstructs them, which also halves their size. Never try to read
  the raw index.
- **Basic paging stops at 10,000 records** (`page` times `per_page`). Past that
  you need cursor paging, which `openalex_harvest` does.
- **`per_page` allows up to 200**, despite the spec saying 100.
- **A 400 costs $0.0001.** Malformed queries are billed, so check the field
  name with `openalex_fields` rather than guessing.
- **Two different 429s.** One means slow down, the other means the budget is
  gone until midnight UTC. The plugin tells them apart and only retries the
  first.
- **Semantic search returns 500** for everyone at the moment. Avoid it.
