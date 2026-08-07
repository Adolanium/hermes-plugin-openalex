from __future__ import annotations

import json

import pytest
from hermes_plugins.openalex import shaping
from hermes_plugins.openalex.errors import (
    AuthError,
    BadRequestError,
    BudgetExhaustedError,
    NotFoundError,
    ThrottledError,
    UpstreamError,
    classify_http,
    redact,
)


def _work(authors: int = 9, refs: int = 54) -> dict:
    """A work shaped like a real one, including the parts that are enormous."""
    return {
        "id": "https://openalex.org/W2741809807",
        "doi": "https://doi.org/10.7717/peerj.4375",
        "display_name": "The state of OA: a large-scale analysis",
        "publication_year": 2018,
        "publication_date": "2018-02-13",
        "type": "article",
        "cited_by_count": 1543,
        "fwci": 12.4,
        "is_retracted": False,
        "authorships": [
            {
                "author": {
                    "id": f"https://openalex.org/A{i}",
                    "display_name": f"Author {i}",
                    "orcid": f"https://orcid.org/0000-0002-0000-{i:04d}",
                },
                "institutions": [
                    {"display_name": f"Institution {i}"},
                    {"display_name": "Second place"},
                ],
                "is_corresponding": i == 0,
            }
            for i in range(authors)
        ],
        "primary_location": {
            "source": {"id": "https://openalex.org/S1983995261", "display_name": "PeerJ"}
        },
        "open_access": {"oa_status": "gold", "oa_url": "https://peerj.com/articles/4375.pdf"},
        "primary_topic": {
            "id": "https://openalex.org/T10102",
            "display_name": "Scholarly Communication",
            "field": {"display_name": "Social Sciences"},
        },
        # The heavy fields, sized the way real ones are.
        "abstract_inverted_index": {
            "Despite": [0],
            "growing": [1],
            "interest": [2],
            "in": [3, 6],
            "Open": [4],
            "Access": [5],
            "publishing": [7],
        },
        "concepts": [{"display_name": f"Concept {i}", "score": 0.5} for i in range(20)],
        "referenced_works": [f"https://openalex.org/W{i}" for i in range(refs)],
        "related_works": [f"https://openalex.org/W{i}" for i in range(20)],
        "counts_by_year": [{"year": y, "cited_by_count": 100} for y in range(2018, 2027)],
        "locations": [{"source": {"display_name": "x" * 400}} for _ in range(6)],
        "mesh": [{"descriptor_name": "y" * 200} for _ in range(10)],
    }


class TestAbstractReconstruction:
    def test_inverted_index_becomes_prose(self):
        text = shaping.reconstruct_abstract(_work()["abstract_inverted_index"])
        assert text == "Despite growing interest in Open Access in publishing"

    def test_gaps_in_positions_do_not_break_it(self):
        """Positions can be sparse, so this walks sorted keys not a range."""
        assert shaping.reconstruct_abstract({"alpha": [0], "omega": [99]}) == "alpha omega"

    def test_reconstruction_is_smaller_than_the_index(self):
        """The index encodes each term plus its positions, so prose is smaller."""
        index = _work()["abstract_inverted_index"]
        assert len(shaping.reconstruct_abstract(index)) < len(json.dumps(index))

    def test_empty_and_junk_are_none(self):
        assert shaping.reconstruct_abstract(None) is None
        assert shaping.reconstruct_abstract({}) is None
        assert shaping.reconstruct_abstract("not an index") is None


class TestWorkShaping:
    def test_summary_is_dramatically_smaller(self):
        raw = _work()
        raw_size = len(json.dumps(raw))
        shaped_size = len(json.dumps(shaping.shape_work(raw, "summary")))
        assert shaped_size < raw_size / 5

    def test_deprecated_concepts_are_dropped_everywhere(self):
        for verbosity in ("summary", "detail", "raw"):
            serialized = json.dumps(shaping.shape_work(_work(), verbosity))
            assert "Concept 1" not in serialized, verbosity

    def test_ids_are_shortened(self):
        shaped = shaping.shape_work(_work())
        assert shaped["id"] == "W2741809807"
        assert shaped["doi"] == "10.7717/peerj.4375"
        assert shaped["venue_id"] == "S1983995261"

    def test_giant_author_lists_are_capped_but_counted(self):
        """One collaboration paper measured 2.88 MB, 93% of it authorships."""
        shaped = shaping.shape_work(_work(authors=2467), "summary")
        assert len(shaped["authors"]) == 10
        assert shaped["author_count"] == 2467
        assert shaped["more_authors"] is True
        assert len(json.dumps(shaped)) < 4000

    def test_detail_adds_the_abstract_but_summary_does_not(self):
        assert "abstract" not in shaping.shape_work(_work(), "summary")
        assert "abstract" in shaping.shape_work(_work(), "detail")

    def test_raw_still_drops_the_unbounded_fields(self):
        shaped = shaping.shape_work(_work(), "raw")
        assert "referenced_works" not in shaped
        assert shaped["referenced_works_count"] == 54
        assert "_omitted" in shaped


class TestSelect:
    def test_summary_select_is_lean(self):
        select = shaping.select_for("works", "summary")
        assert "abstract_inverted_index" not in select
        assert "concepts" not in select
        assert "id" in select

    def test_detail_select_adds_the_abstract(self):
        assert "abstract_inverted_index" in shaping.select_for("works", "detail")

    def test_raw_sends_no_select(self):
        """Raw means whatever OpenAlex holds, so we must not define it for them."""
        assert shaping.select_for("works", "raw") is None

    def test_entity_selects_avoid_the_three_heavy_fields(self):
        """topics, topic_share and counts_by_year are about 95% of these records."""
        for entity in ("authors", "sources", "institutions"):
            select = shaping.select_for(entity, "summary")
            assert "topic_share" not in select, entity
            assert "counts_by_year" not in select, entity


class TestGroups:
    def test_group_keys_are_split_into_label_and_id(self):
        rows = shaping.shape_groups(
            [
                {
                    "key": "https://openalex.org/T10102",
                    "key_display_name": "Scholarly Communication",
                    "count": 42,
                }
            ]
        )
        assert rows == [{"value": "Scholarly Communication", "id": "T10102", "count": 42}]

    def test_non_url_keys_keep_no_id(self):
        rows = shaping.shape_groups([{"key": "article", "key_display_name": "article", "count": 7}])
        assert rows == [{"value": "article", "count": 7}]


class TestFit:
    def test_output_is_always_valid_json(self):
        payload = {
            "ok": True,
            "results": [shaping.shape_work(_work(), "detail") for _ in range(60)],
        }
        json.loads(json.dumps(shaping.fit(payload, 3000)))

    def test_it_gets_under_the_budget(self):
        payload = {
            "ok": True,
            "results": [shaping.shape_work(_work(), "detail") for _ in range(60)],
        }
        fitted = shaping.fit(payload, 4000)
        assert len(json.dumps(fitted, default=str)) <= 4600

    def test_prose_goes_before_records(self):
        payload = {
            "ok": True,
            "results": [{"id": f"W{i}", "abstract": "x" * 300} for i in range(20)],
        }
        fitted = shaping.fit(payload, 2000)
        assert all("abstract" not in row for row in fitted["results"])
        assert len(fitted["results"]) == 20, "ids should survive when dropping prose sufficed"

    def test_small_payloads_pass_through_untouched(self):
        payload = {"ok": True, "total": 5}
        assert shaping.fit(payload, 24_000) == payload

    def test_trimming_is_announced(self):
        payload = {
            "ok": True,
            "results": [shaping.shape_work(_work(), "detail") for _ in range(40)],
        }
        assert shaping.fit(payload, 2000)["_truncation"]


class TestErrorClassification:
    def test_throttle_and_exhaustion_are_told_apart_by_retry_after(self):
        """The remaining-balance headers report zero during throttling, so
        branching on them would misclassify every throttle as an empty wallet."""
        throttle = classify_http(
            429,
            "",
            {"message": "Rate limit exceeded: 10 requests per second."},
            retry_after=1.0,
            cost_required=0.0,
        )
        exhausted = classify_http(
            429,
            "",
            {"message": "Insufficient budget."},
            retry_after=43924.0,
            cost_required=0.001,
        )
        assert isinstance(throttle, ThrottledError)
        assert isinstance(exhausted, BudgetExhaustedError)

    def test_exhaustion_tells_the_model_to_use_the_free_tools(self):
        error = classify_http(429, "", {"message": "Insufficient budget."}, retry_after=40000.0)
        assert "openalex_get" in error.next_step
        assert "midnight UTC" in error.next_step

    def test_missing_retry_after_assumes_the_expensive_case(self):
        """Better to stop than to hammer an exhausted budget for a day."""
        assert isinstance(classify_http(429, "", {"message": "?"}), BudgetExhaustedError)

    def test_html_bodies_do_not_crash_the_parser(self):
        """404 and 500 come back as HTML rather than JSON."""
        assert isinstance(classify_http(404, "<html>Not Found</html>", None), NotFoundError)
        assert isinstance(classify_http(500, "<html>Error</html>", None), UpstreamError)

    @pytest.mark.parametrize(
        "status,expected", [(401, AuthError), (400, BadRequestError), (403, BadRequestError)]
    )
    def test_status_mapping(self, status, expected):
        assert isinstance(classify_http(status, "", {"message": "x"}), expected)

    def test_bad_request_keeps_the_field_list_openalex_returned(self):
        """The 400 body enumerates every valid field, which is worth surfacing."""
        error = classify_http(
            400, "", {"message": "xyz is not a valid field. Valid fields: a, b, c"}
        )
        assert "Valid fields" in error.details["openalex_message"]


class TestRedaction:
    def test_key_is_stripped_from_a_url(self):
        url = "https://api.openalex.org/works?api_key=SECRET123&filter=is_oa:true"
        assert "SECRET123" not in redact(url, "SECRET123")

    def test_generic_param_is_stripped_without_knowing_the_value(self):
        assert "somethingelse" not in redact("https://api.openalex.org/works?api_key=somethingelse")
