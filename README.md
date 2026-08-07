# hermes-plugin-openalex

[OpenAlex](https://openalex.org) for [Hermes Agent](https://github.com/NousResearch/hermes-agent). 250 million scholarly works, 90 million authors, every journal and institution, exposed as nine tools with a spend guard that refuses before it costs you anything.

```bash
hermes plugins install Adolanium/hermes-plugin-openalex --enable
```

```bash
hermes plugins remove openalex
```

Works with **no API key**. The anonymous tier gets $0.10 a day, and a free key raises it to $1.00. Identifier lookups and name resolution cost nothing at all and keep working even when the daily budget is gone.

---

## Why this one is different

OpenAlex used to be free and polite-pooled. Since February 2026 it is **metered in actual dollars**, and the price depends on the shape of the call rather than the size of the answer. That spread is a factor of a hundred:

| Call | Price | Anonymous day buys |
|---|---:|---:|
| Fetch by id, prefixed form | **$0** | unlimited |
| Autocomplete | **$0** | unlimited |
| List or filter | $0.0001 | 1,000 |
| **Count with `group_by`** | **$0.0001** | 1,000 |
| Search | $0.001 | 100 |
| Text classification | $0.01 | **10** |

An agent that reaches for search by habit gets a hundred calls a day. One that counts first gets a thousand, and usually a better answer, because a grouped count returns the shape of a literature rather than an arbitrary page of it.

So this plugin does three things a thin wrapper does not.

**It knows the price before it sends the request.** Cost is derivable from the call shape, so the budget guard refuses in advance rather than discovering the wall afterwards. That matters more than it sounds: OpenAlex bills $0.0001 even for a rejected 400, so a refused call is genuinely free while a failed one is not.

**It routes the model to the cheap call.** Adding `group_by` to a query drops it from search price to list price *even when a search term is present*. That is a tenfold saving for the same question, and the tool descriptions say so in capitals, repeatedly.

**It tells the two rate limits apart.** OpenAlex answers 429 both for "you sent that too fast" and for "your money is gone until midnight UTC". The obvious discriminator does not work, because during throttling the API reports `X-RateLimit-Remaining: 0` even when budget remains. `Retry-After` is the honest signal: about a second for a throttle, tens of thousands for an exhausted wallet. Retrying the first is right. Retrying the second wastes half a day.

Verified live at zero balance:

```
error_kind: budget_exhausted
http      : 429
details   : {"retry_after_seconds": 42549.0, "cost_required_usd": 0.0001}
next_step : The OpenAlex daily budget is spent and refills at midnight UTC.
            Do not retry this call. ID lookups (openalex_get) and name
            resolution (openalex_resolve) are free and still work...
```

In the same state, `openalex_get`, `openalex_resolve` and `openalex_fields` all returned real data. That degraded mode is not a fallback bolted on, it is what OpenAlex actually does, surfaced honestly.

---

## The size problem

A single work runs to 33 KB. One paper by a large collaboration measured **2.88 MB**, and 93% of that was one author list. `select` handles most of it server-side, cutting a 25-result page from 1.5 MB to 6.5 KB, but it only accepts top-level fields so it cannot reach inside `authorships`. This plugin does the rest.

Measured live on a real record:

```
raw JSON      33,475 chars
summary        1,921 chars     17x smaller
detail         3,769 chars      9x smaller
```

What survives: identity, year, venue, citation count, open-access status, topic, and the first ten authors with their institutions and a total count. What goes: the deprecated `concepts` block (a tenth of every work, superseded by `topics`), the reference id list, `counts_by_year`, and the location array.

Abstracts get special handling. OpenAlex ships them as an inverted index, a term-to-positions map that is about twice the size of the prose it encodes. The plugin reconstructs the sentence, which both halves the bytes and spares the model a JSON puzzle it would otherwise try to solve in-context.

---

## The tools

Nine tools in one `openalex` toolset, ordered here the way the descriptions order them: free first.

### core, on by default

| Tool | What it does | Price |
|---|---|---|
| `openalex_resolve` | Name to id, via autocomplete. Returns the filter key to use next | **free** |
| `openalex_get` | Fetch by OpenAlex id, DOI, ORCID, ROR, ISSN or PMID. Up to 20 at once | **free** |
| `openalex_fields` | Valid filters, selects and vocabularies, served locally | **free** |
| `openalex_account` | Budget, prices, session ledger | **free** |
| `openalex_count` | Totals and `group_by` breakdowns | $0.0001 |
| `openalex_related` | Citation graph traversal: cited_by, references, related | $0.0001 |
| `openalex_search` | The records themselves | $0.001 |

### full, opt in with `hermes openalex profile full`

| Tool | What it does |
|---|---|
| `openalex_harvest` | Cursor pagination past the 10,000 ceiling, costed up front |
| `openalex_classify` | Topic classification of arbitrary text. Needs a second opt-in, see below |

`openalex_classify` costs $0.01, a hundred times a list call, and ten calls exhaust the entire anonymous daily budget. It stays hidden until explicitly allowed, because a tool that would always refuse is noise in the schema rather than a feature.

---

## Examples

Real captured output.

### Resolve then filter, instead of searching

```
$ hermes openalex resolve "bengio" --entity authors
'bengio'  (free)
  id            name              type    works   hint
  A5039786469   Bengio            author  1,247   Mila, Université de Montréal
```

Filtering on that id is exact and cheap. Searching the name is fuzzy, ten times the price, and matches every acknowledgement and reference list the string appears in.

### Count before you search

```
$ hermes openalex count --search "graph neural network" --group-by publication_year
publication_year
  value    count
  2024    28,417
  2023    24,902
  2022    18,330
  ...
this call $0.0001 (list)   session $0.0001 of $0.0500
```

Same question via search would cost $0.0010 and return one page you would have to aggregate yourself.

### What the model receives

```json
{
  "ok": true,
  "record": {
    "id": "W2741809807",
    "doi": "10.7717/peerj.4375",
    "title": "The state of OA: a large-scale analysis of the prevalence and impact of Open Access articles",
    "year": 2018,
    "type": "article",
    "cited_by_count": 1543,
    "authors": [
      {"name": "Heather Piwowar", "id": "A5048491430", "institutions": ["Impactstory"]}
    ],
    "author_count": 9,
    "venue": "PeerJ",
    "open_access": "gold",
    "topic": "Scholarly Communication"
  },
  "cost": {"this_call_usd": 0.0, "price_class": "singleton", "spent_usd": 0.0}
}
```

OpenAlex sent 33,475 characters for that record. This is 1,921.

### When the budget is gone

```json
{
  "ok": false,
  "error": "Insufficient budget. This request costs $0.0001 but you only have $0.0000 remaining.",
  "error_kind": "budget_exhausted",
  "http_status": 429,
  "details": {"retry_after_seconds": 42549.0, "cost_required_usd": 0.0001},
  "next_step": "The OpenAlex daily budget is spent and refills at midnight UTC. Do not retry this call. ID lookups (openalex_get) and name resolution (openalex_resolve) are free and still work at zero budget, so use those. A free API key raises the daily budget from $0.10 to $1.00."
}
```

---

## Configuration

Everything lives under `plugins.entries.openalex`. Every key has a working default.

```yaml
plugins:
  enabled: [openalex]
  entries:
    openalex:
      profile: core            # core | full
      verbosity: summary       # summary | detail | raw
      max_result_chars: 24000
      rate_limit_per_second: 8 # the API ceiling is 10/s
      timeout_seconds: 30
      retries: 2
      reconstruct_abstracts: true

      cache:
        enabled: true
        ttl_seconds: 3600
        max_entries: 512

      budget:
        usd_per_session: 0.05          # 500 counts, or 50 searches
        allow_text_classification: false

      tools:
        enabled: []
        disabled: []
```

The key comes from `OPENALEX_API_KEY`. It is sent as a Bearer header rather than the documented query parameter, which keeps it out of proxy logs and tracebacks. The query-param form remains the documented one, so it is the fallback.

---

## Terminal

```bash
hermes openalex doctor       # why is this not working
hermes openalex prices       # what each call costs, and how many a day buys
hermes openalex budget       # session ledger and account balance
hermes openalex resolve "eth zurich" --entity institutions
hermes openalex get 10.7717/peerj.4375
hermes openalex count --filter 'is_oa:true,publication_year:2024' --group-by 'open_access.oa_status'
hermes openalex search "protein folding" --limit 10
hermes openalex profile full
hermes openalex cache stats
```

`doctor` checks the plugin is enabled, proves connectivity with a free call so a missing key and a broken network stay distinguishable, reports the live budget and when it resets, and refreshes the price table from your account rather than trusting a constant.

---

## In conversation

```
/openalex W2741809807         fetch a work by id           free
/openalex 10.7717/peerj.4375  fetch by DOI                 free
/openalex bengio              resolve a name to an id      free
/openalex budget              session spend and balance    free
```

Every path the slash command takes is free. Counts and searches cost money, so ask the agent for those and it will pick the cheapest call that answers the question.

---

## Skills

- `openalex:query-syntax` covers the filter language, the grouping cap, the identifier forms and the cost model. It exists mostly to stop the model inventing filter names, because a rejected query is still billed.
- `openalex:lit-review` is the cheap workflow: anchor, size, shape, traverse, retrieve. It also covers reading the data honestly, including citation counts favouring old work, imperfect author disambiguation, and uneven coverage outside Crossref-indexed journals.

---

## Development

```bash
git clone https://github.com/Adolanium/hermes-plugin-openalex
cd hermes-plugin-openalex
pytest tests
```

104 tests, none of which call the API, so the suite runs without a key and spends nothing. Run them as `pytest tests` rather than bare `pytest`: Hermes requires an `__init__.py` at the plugin root, which makes pytest treat the root as a package and try to import that file standalone. The ini file in `tests/` pins rootdir so collection never walks up into it.

`tests/conftest.py` loads the plugin the way `hermes_cli/plugins.py` does, as `hermes_plugins.openalex` with a synthetic namespace parent, so the import path under test is the real one.

### Dependencies

None. `httpx`, `rich` and `pyyaml` already ship with Hermes.

### Why not pyalex

pyalex is the community standard and it authenticates correctly, contrary to its own open issue on the subject. But it discards the response object after parsing, so the `X-RateLimit-*` headers never reach the caller, and on a metered API those headers carry the price of the call and the balance remaining. It also retries 429 blindly, which is exactly wrong for the half of 429s that mean the budget is gone for the day. Its README still documents the superseded January credit model. The API is a small GET-only REST surface, so a thin httpx layer that reads the headers is less code than working around the gaps.

---

## Known limits

Semantic search returns HTTP 500 for everyone at the moment, so it is not exposed. The n-grams endpoint was removed and now silently returns the plain work object instead of erroring, which is a trap worth knowing about if you call OpenAlex directly. Above a few hundred thousand records the free bulk snapshot at `s3://openalex` beats the API on both cost and speed.

## License

MIT. See [LICENSE](LICENSE).
