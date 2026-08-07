# Changelog

## 0.1.0

First release.

Nine tools across a `core` and `full` profile, two bundled skills, a
`hermes openalex` CLI and an `/openalex` slash command.

- Cost prediction from call shape, so the budget guard refuses before the
  request goes out rather than after. This matters because OpenAlex bills
  $0.0001 even for a rejected 400, making a refused call genuinely free and a
  failed one not.
- Per-session USD ledger that records both the predicted price and the actual
  cost reported by the response headers, and says so when they diverge.
- The two flavours of 429 are told apart by `Retry-After` rather than by the
  remaining-balance headers, which report zero during throttling even when
  budget remains. Throttles are retried, exhausted budgets are not.
- Free calls stay free and keep working at zero balance, matching what the API
  actually does: identifier lookups, autocomplete and the local field
  reference.
- `group_by` routing, which drops a query from search price to list price even
  with a search term present, a factor of ten for the same question.
- Response shaping at three verbosity levels. A live work record drops from
  33,475 characters to 1,921. Deprecated `concepts` is stripped
  unconditionally, and abstracts are reconstructed from their inverted index,
  which halves their size.
- Author lists are capped and counted rather than dropped, because one
  collaboration paper measured 2.88 MB with 93% in a single field that
  `select` cannot reach inside.
- Identifier normalization into the free prefixed form. A bare DOI 404s and
  the URL form is billed, so both are rewritten.
- Text classification behind two independent opt-ins, since it costs $0.01 and
  ten calls exhaust the anonymous daily budget.
- Harvest costs the whole cursor run up front and refuses if the session
  budget will not cover it, rather than stopping halfway with partial data and
  a spent wallet.
- Works around three places the published docs are wrong: the anonymous daily
  budget is $0.10 rather than $0.01, `per_page` allows 200 rather than 100, and
  the documented `-field` descending sort returns 400.
- API key sent as a Bearer header rather than the documented query parameter,
  keeping it out of proxy logs and tracebacks.
- 104 tests, none of which call the API, so the suite runs without a key and
  spends nothing.
