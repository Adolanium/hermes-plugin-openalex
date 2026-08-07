# OpenAlex is installed

```
hermes openalex doctor      why is this not working
hermes openalex prices      what each call costs, and how many a day buys
hermes openalex budget      session spend and account balance
```

It works right now with no API key. The anonymous tier gets $0.10 a day, which
is 1,000 grouped counts or 100 searches. A free key from
https://openalex.org/settings/api raises that to $1.00:

```
hermes openalex setup
```

If a gateway is running, restart it so the tools appear there too:

```
hermes gateway restart
```

## What to know

**Some calls are free and stay free.** Fetching a record by id or DOI, and
resolving a name to an id, cost nothing and keep working even after the daily
budget is spent. The agent is told this, so it leans on them.

**Counting is ten times cheaper than searching.** A grouped count answers how
many, which years, which institutions and which journals for $0.0001, where
the equivalent search costs $0.001 and returns one page you would have to
aggregate yourself. The tool descriptions push hard in that direction.

**There is a session budget**, $0.05 by default, which is 500 counts. Raise it
with `plugins.entries.openalex.budget.usd_per_session` if you are doing
something large.

**Text classification is off.** It costs $0.01 a call, so ten calls would
exhaust an entire anonymous day. It needs both `profile: full` and
`budget.allow_text_classification: true`.

## Try it

```
/openalex 10.7717/peerj.4375
/openalex bengio
```

Or just ask: "how has research on graph neural networks grown since 2015, and
who publishes most of it".
