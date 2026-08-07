# OpenAlex query cookbook

Worked queries, with the cost of each. Prices assume the default table: free
for id lookups and autocomplete, $0.0001 for a list or grouped count, $0.001
for a search.

## Map a researcher's output

```
openalex_resolve(query="yoshua bengio", entity="authors")          free
openalex_count(filter="authorships.author.id:A5023888391",
               group_by="publication_year")                        $0.0001
openalex_count(filter="authorships.author.id:A5023888391",
               group_by="primary_topic.field.id")                  $0.0001
openalex_count(filter="authorships.author.id:A5023888391",
               group_by="open_access.oa_status")                   $0.0001
openalex_search(filter="authorships.author.id:A5023888391",
                sort="cited_by_count:desc", per_page=10)           $0.0001
```

Total: $0.0004. The last call is a filter without a search term, so it is
billed as a list rather than a search. Filtering on a resolved id instead of
searching a name is both cheaper and exact.

## Size a field before reading it

```
openalex_count(search="graph neural network",
               group_by="publication_year")                        $0.0001
openalex_count(search="graph neural network",
               group_by="primary_location.source.id")              $0.0001
openalex_count(search="graph neural network",
               group_by="authorships.institutions.country_code")   $0.0001
```

Three calls, $0.0003, and you know the size, the trajectory, the leading venues
and the geography. The equivalent in searches would be $0.003 and would return
data you would have to aggregate yourself.

## Walk a citation graph

```
openalex_get(id="doi:10.1038/nature14539")                         free
openalex_related(id="W2158899491", mode="cited_by",
                 sort="cited_by_count:desc", per_page=20)          $0.0001
openalex_related(id="W2158899491", mode="references")              $0.0001
```

Forwards and backwards from a known paper for $0.0002.

## Find an institution's open access position

```
openalex_resolve(query="eth zurich", entity="institutions")        free
openalex_count(filter="authorships.institutions.ror:05a28rw58,"
                      "publication_year:2024",
               group_by="open_access.oa_status")                   $0.0001
openalex_count(filter="authorships.institutions.ror:05a28rw58,"
                      "publication_year:2020-2024,is_oa:true",
               group_by="publication_year")                        $0.0001
```

## Recent highly cited work in a field

```
openalex_search(search="protein structure prediction",
                search_field="title_and_abstract",
                filter="publication_year:>2021,cited_by_count:>100",
                sort="cited_by_count:desc", per_page=15)           $0.001
```

Worth the search price because you want the papers themselves. Note
`search_field`: without it you would be searching full text and getting
roughly twice as many, looser hits.

## Check whether something is retracted

```
openalex_get(id="doi:10.1016/j.example.2020.01.001")               free
openalex_count(filter="authorships.author.id:A123,is_retracted:true")  $0.0001
```

## Journals in a field, by cost to publish

```
openalex_count(filter="primary_topic.field.id:17",
               group_by="primary_location.source.id")              $0.0001
openalex_get(id="S137773608")                                      free
```

Group to find the venues, then fetch the ones that matter by id for free. The
source record carries `apc_usd`, `is_in_doaj` and the h-index.

## Everything a country published on a UN goal

```
openalex_count(filter="sustainable_development_goals.id:https://metadata.un.org/sdg/3,"
                      "authorships.institutions.country_code:KE",
               group_by="publication_year")                        $0.0001
```

## Bulk retrieval, when you really need the records

```
openalex_harvest(filter="authorships.institutions.ror:05a28rw58,"
                        "publication_year:2024",
                 max_records=1000, per_page=200)                   $0.0005
```

Five pages at $0.0001 each. The tool estimates this before starting and refuses
if the session budget will not cover it, rather than stopping halfway.

Above a few hundred thousand records, stop using the API. The full snapshot is
free and unmetered:

```
aws s3 sync "s3://openalex/data/jsonl" ./openalex --no-sign-request
```

## Anti-patterns

**Searching for a name you could resolve.**
`openalex_search(search="Yoshua Bengio")` costs ten times
`openalex_resolve` plus a filter, and returns fuzzy matches on a string that
appears in acknowledgements and reference lists.

**Paging a search to count things.** If the question is "how many", one grouped
count answers it for a tenth of one page.

**Classifying text that is already indexed.** `openalex_classify` costs $0.01.
If the work has a DOI, `openalex_get` returns its topics for free.

**Fetching at raw verbosity by habit.** A single work by a large collaboration
came to 2.88 MB, 93% of it one author list. Summary verbosity is a few hundred
bytes and keeps the fields that matter.
