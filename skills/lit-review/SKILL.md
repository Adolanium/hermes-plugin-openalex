---
name: lit-review
description: Cheap literature mapping and citation tracing with OpenAlex
version: 0.1.0
author: hermes-plugin-openalex
license: MIT
metadata:
  hermes:
    tags: [openalex, literature-review, research, citations, bibliometrics]
---

# Mapping a literature with OpenAlex

A method for going from a vague question to a defensible reading list, ordered
so the free and cheap calls do the work.

## The shape of it

```
1. Anchor      find one or two papers you are confident about   free
2. Size        how big is this literature, and is it growing    $0.0001 each
3. Shape       who, where, which venues, which subtopics        $0.0001 each
4. Traverse    walk citations out from the anchors              $0.0001 each
5. Retrieve    fetch the specific papers worth reading          free by id
```

Search appears nowhere in that list until you genuinely cannot name what you
want. A full map of a field costs well under a cent.

## 1. Anchor

If the user named a paper, fetch it by DOI. That is free and exact:

```
openalex_get(id="doi:10.1038/nature14539", verbosity="detail")
```

If they named a person, lab or journal, resolve it rather than searching:

```
openalex_resolve(query="demis hassabis", entity="authors")
```

Only if you have nothing concrete should you search, and then search
`title_and_abstract` rather than the default full text, which is about twice as
broad and much noisier.

## 2. Size the literature

Before reading anything, find out how much there is. A question that returns
200 papers is a different job from one that returns 200,000.

```
openalex_count(search="mechanistic interpretability",
               group_by="publication_year")
```

The year distribution tells you whether this is an established field, a recent
surge, or something that peaked and faded. That framing is often more useful to
the user than any individual paper.

## 3. Shape it

Three more grouped counts, still a hundredth of a cent each:

```
group_by="authorships.institutions.id"      who is doing the work
group_by="primary_location.source.id"       where it gets published
group_by="primary_topic.id"                 what the subtopics are
group_by="open_access.oa_status"            how much you can actually read
```

Now you can tell the user what the field looks like before you have read a
single abstract, and you know which venues and groups to focus on.

## 4. Traverse

Citation traversal is cheaper and far more precise than searching, because it
follows a graph the authors built rather than matching strings.

```
openalex_related(id="W2741809807", mode="references")   what it built on
openalex_related(id="W2741809807", mode="cited_by",
                 sort="cited_by_count:desc")            what built on it
```

Backwards from a recent survey gives you the canon. Forwards from an old
seminal paper, sorted by citations, gives you what actually mattered. Papers
that appear in both directions from different anchors are usually the ones
worth reading.

## 5. Retrieve

Fetch the shortlist by id, which is free, at detail verbosity for abstracts:

```
openalex_get(id="W123,W456,W789", verbosity="detail")
```

Up to 20 ids in one call, and it costs nothing.

## Reading the data honestly

- **Citation counts favour old work.** A 2015 paper has had a decade to
  accumulate them. Use `fwci`, the field-weighted citation impact, when
  comparing across years, since it normalizes for field and age.
- **Author disambiguation is automated and imperfect.** Common names merge and
  split. An author id with a wildly implausible work count is usually a merge
  artifact. Check the ORCID when it matters.
- **Coverage is uneven.** Strong on journals indexed by Crossref, weaker on
  books, non-English work, humanities, and grey literature. Absence from
  OpenAlex is not absence from the literature, and saying so matters when the
  user asks whether anyone has studied something.
- **Abstracts are missing more often than you would expect**, particularly for
  older and paywalled work. `has_abstract:true` filters to what is readable.
- **Retractions.** `is_retracted` exists and is worth checking before
  recommending anything. It is not always propagated promptly.
- **`concepts` is deprecated.** If you see it anywhere, use `topics`.
- **group_by truncates at 200 groups**, so a long-tail distribution is showing
  you its head. The plugin flags this.

## Cost discipline

The session budget defaults to $0.05, which is 500 grouped counts or 50
searches. That is a lot of literature mapping. If you hit it you were probably
searching where you should have been counting.

When the budget refuses a call, it says what to do instead. Take that seriously
rather than retrying: the refusal is local and costs nothing, but the retry
would cost money and fail the same way.

If the daily OpenAlex budget is gone rather than the session one, id lookups
and name resolution still work. You can keep reading papers you have already
identified until midnight UTC.

## What to hand back

A useful literature answer names the shape before the specifics: how large the
field is, whether it is growing, who dominates it, and where it gets published.
Then a short list of papers with a reason for each. Bare lists of titles are
what a search engine gives you, and the counting workflow above is what makes
the difference.
