from __future__ import annotations

import json

from conftest import FakeResponse
from hermes_plugins.openalex import config as config_mod
from hermes_plugins.openalex import handlers_core, handlers_full


def call(handler, args=None, **kwargs):
    return json.loads(handler(args or {}, **kwargs))


WORK = {
    "id": "https://openalex.org/W2741809807",
    "display_name": "The state of OA",
    "publication_year": 2018,
    "cited_by_count": 1543,
    "authorships": [{"author": {"id": "https://openalex.org/A1", "display_name": "Piwowar"}}],
    "primary_location": {"source": {"display_name": "PeerJ"}},
}


class TestResolve:
    def test_returns_ids_and_the_next_filter(self, fake_client, budget_reset):
        fake_client(
            [
                FakeResponse(
                    200,
                    {
                        "results": [
                            {
                                "id": "https://openalex.org/A5023888391",
                                "display_name": "Yoshua Bengio",
                                "entity_type": "author",
                                "hint": "Mila",
                                "works_count": 1200,
                                "filter_key": "authorships.author.id",
                            }
                        ]
                    },
                )
            ]
        )
        result = call(handlers_core.openalex_resolve, {"query": "bengio", "entity": "authors"})
        assert result["ok"] is True
        assert result["matches"][0]["id"] == "A5023888391"
        assert result["matches"][0]["filter_key"] == "authorships.author.id"
        assert result["cost"] == "free"

    def test_uses_the_free_autocomplete_endpoint(self, fake_client, budget_reset):
        _, transport = fake_client([FakeResponse(200, {"results": []})])
        call(handlers_core.openalex_resolve, {"query": "x", "entity": "authors"})
        assert transport.calls[0]["url"].endswith("/autocomplete/authors")

    def test_costs_nothing_against_the_ledger(self, fake_client, budget_reset):
        fake_client([FakeResponse(200, {"results": []})])
        call(handlers_core.openalex_resolve, {"query": "x"})
        assert budget_reset.ledger(None).actual_usd == 0.0


class TestGet:
    def test_single_record(self, fake_client, budget_reset):
        fake_client([FakeResponse(200, WORK)])
        result = call(handlers_core.openalex_get, {"id": "W2741809807"})
        assert result["record"]["id"] == "W2741809807"

    def test_a_bare_doi_is_normalised_to_the_free_prefixed_form(self, fake_client, budget_reset):
        """The URL form is billed and a bare DOI 404s, so both get rewritten."""
        _, transport = fake_client([FakeResponse(200, WORK)])
        call(handlers_core.openalex_get, {"id": "10.7717/peerj.4375"})
        assert transport.calls[0]["url"].endswith("/works/doi:10.7717/peerj.4375")

    def test_a_doi_url_is_normalised_too(self, fake_client, budget_reset):
        _, transport = fake_client([FakeResponse(200, WORK)])
        call(handlers_core.openalex_get, {"id": "https://doi.org/10.7717/peerj.4375"})
        assert transport.calls[0]["url"].endswith("/works/doi:10.7717/peerj.4375")

    def test_entity_is_inferred_from_the_identifier(self, fake_client, budget_reset):
        _, transport = fake_client([FakeResponse(200, {"id": "https://openalex.org/A1"})])
        call(handlers_core.openalex_get, {"id": "orcid:0000-0002-1298-3089"})
        assert "/authors/" in transport.calls[0]["url"]

    def test_missing_records_are_reported_not_raised(self, fake_client, budget_reset):
        fake_client([FakeResponse(404, None, text="<html>Not Found</html>")])
        result = call(handlers_core.openalex_get, {"id": "W999"})
        assert result["ok"] is True
        assert result["not_found"] == ["W999"]

    def test_get_is_free(self, fake_client, budget_reset):
        fake_client([FakeResponse(200, WORK)])
        result = call(handlers_core.openalex_get, {"id": "W2741809807"})
        assert result["cost"]["this_call_usd"] == 0.0


class TestCount:
    def test_group_by_is_billed_at_list_price_even_with_a_search(self, fake_client, budget_reset):
        """The 10x saving the whole tool is built around."""
        fake_client(
            [
                FakeResponse(
                    200,
                    {
                        "meta": {"count": 5797071, "groups_count": 25},
                        "group_by": [{"key": "2024", "key_display_name": "2024", "count": 900000}],
                    },
                )
            ]
        )
        result = call(
            handlers_core.openalex_count,
            {"search": "neural", "group_by": "publication_year"},
        )
        assert result["total"] == 5797071
        assert result["cost"]["this_call_usd"] == 0.0001
        assert result["cost"]["price_class"] == "list"

    def test_a_bare_search_without_grouping_costs_ten_times_more(self, fake_client, budget_reset):
        fake_client([FakeResponse(200, {"meta": {"count": 10}})])
        result = call(handlers_core.openalex_search, {"search": "neural"})
        assert result["cost"]["this_call_usd"] == 0.001

    def test_the_group_cap_is_flagged(self, fake_client, budget_reset):
        """OpenAlex silently truncates at 200 groups, which would otherwise
        read as a complete distribution."""
        fake_client(
            [FakeResponse(200, {"meta": {"count": 1, "groups_count": 200}, "group_by": []})]
        )
        result = call(handlers_core.openalex_count, {"group_by": "publication_year"})
        assert "200 groups" in result["groups_truncated"]

    def test_ungrouped_count_asks_for_the_smallest_possible_page(self, fake_client, budget_reset):
        _, transport = fake_client([FakeResponse(200, {"meta": {"count": 1}})])
        call(handlers_core.openalex_count, {"filter": "is_oa:true"})
        assert transport.calls[0]["params"]["per_page"] == 1
        assert transport.calls[0]["params"]["select"] == "id"

    def test_grouped_count_sends_no_select(self, fake_client, budget_reset):
        """OpenAlex 400s with "select does not work with group_by".

        A grouped response carries no records, so neither select nor per_page
        has anything to do. Sending select made every grouped count fail.
        """
        _, transport = fake_client([FakeResponse(200, {"meta": {"count": 1}, "group_by": []})])
        call(handlers_core.openalex_count, {"filter": "is_oa:true", "group_by": "publication_year"})
        params = transport.calls[0]["params"]
        assert "select" not in params
        assert "per_page" not in params
        assert params["group_by"] == "publication_year"


class TestSearch:
    def test_title_and_abstract_becomes_a_filter_not_the_search_param(
        self, fake_client, budget_reset
    ):
        """The bare search param means full text on works, which is 2x broader."""
        _, transport = fake_client([FakeResponse(200, {"meta": {"count": 0}, "results": []})])
        call(
            handlers_core.openalex_search,
            {"search": "neural", "search_field": "title_and_abstract"},
        )
        params = transport.calls[0]["params"]
        assert "search" not in params
        assert params["filter"] == "title_and_abstract.search:neural"

    def test_descending_sort_shorthand_is_rewritten(self, fake_client, budget_reset):
        """OpenAlex documents -field for descending, and then 400s on it."""
        _, transport = fake_client([FakeResponse(200, {"meta": {"count": 0}, "results": []})])
        call(handlers_core.openalex_search, {"search": "x", "sort": "-cited_by_count"})
        assert transport.calls[0]["params"]["sort"] == "cited_by_count:desc"

    def test_the_ten_thousand_ceiling_is_refused_locally(self, fake_client, budget_reset):
        _, transport = fake_client([])
        result = call(handlers_core.openalex_search, {"search": "x", "page": 100, "per_page": 200})
        assert result["ok"] is False
        assert "10,000" in result["error"]
        assert transport.calls == [], "a doomed call must not reach the network"

    def test_per_page_is_clamped_to_two_hundred(self, fake_client, budget_reset):
        _, transport = fake_client([FakeResponse(200, {"meta": {"count": 0}, "results": []})])
        call(handlers_core.openalex_search, {"search": "x", "per_page": 500})
        assert transport.calls[0]["params"]["per_page"] == 200

    def test_an_unbounded_search_is_refused(self, fake_client, budget_reset):
        fake_client([])
        assert call(handlers_core.openalex_search, {})["ok"] is False


class TestBudgetGuard:
    def test_a_search_is_refused_before_the_request_goes_out(
        self, fake_client, isolated_config, budget_reset
    ):
        isolated_config["budget"] = {"usd_per_session": 0.0}
        config_mod.reset()
        _, transport = fake_client([])
        result = call(handlers_core.openalex_search, {"search": "neural"})
        assert result["error_kind"] == "budget"
        assert transport.calls == [], "a refused call must not be billed"

    def test_free_tools_still_work_with_no_budget(self, fake_client, isolated_config, budget_reset):
        """Mirrors the API: id lookups keep working at zero balance."""
        isolated_config["budget"] = {"usd_per_session": 0.0}
        config_mod.reset()
        fake_client([FakeResponse(200, WORK)])
        assert call(handlers_core.openalex_get, {"id": "W2741809807"})["ok"] is True

    def test_the_refusal_points_at_the_cheap_alternative(
        self, fake_client, isolated_config, budget_reset
    ):
        isolated_config["budget"] = {"usd_per_session": 0.0}
        config_mod.reset()
        fake_client([])
        result = call(handlers_core.openalex_search, {"search": "x"})
        assert "openalex_count" in result["next_step"]


class TestClassifyGate:
    def test_disabled_by_default(self, fake_client, isolated_config, budget_reset):
        isolated_config["profile"] = "full"
        config_mod.reset()
        _, transport = fake_client([])
        result = call(handlers_full.openalex_classify, {"title": "x"})
        assert result["error_kind"] == "classification_disabled"
        assert "Do not attempt to work around this" in result["next_step"]
        assert transport.calls == []

    def test_runs_when_explicitly_allowed(self, fake_client, isolated_config, budget_reset):
        isolated_config.update(
            {
                "profile": "full",
                "budget": {"allow_text_classification": True, "usd_per_session": 0.05},
            }
        )
        config_mod.reset()
        fake_client(
            [
                FakeResponse(
                    200,
                    {
                        "primary_topic": {
                            "id": "https://openalex.org/T1",
                            "display_name": "ML",
                            "score": 0.32,
                        }
                    },
                )
            ]
        )
        result = call(handlers_full.openalex_classify, {"title": "deep nets"})
        assert result["primary_topic"]["id"] == "T1"
        assert result["cost"]["this_call_usd"] == 0.01


class TestHarvest:
    def test_the_whole_run_is_costed_before_it_starts(
        self, fake_client, isolated_config, budget_reset
    ):
        """Stopping halfway leaves partial data and a spent wallet."""
        isolated_config["budget"] = {"usd_per_session": 0.0002}
        config_mod.reset()
        _, transport = fake_client([])
        result = call(
            handlers_full.openalex_harvest,
            {"filter": "is_oa:true", "max_records": 2000, "per_page": 200},
        )
        assert result["error_kind"] == "budget"
        assert transport.calls == []

    def test_it_follows_the_cursor(self, fake_client, isolated_config, budget_reset):
        isolated_config["budget"] = {"usd_per_session": 0.05}
        config_mod.reset()
        _, transport = fake_client(
            [
                FakeResponse(
                    200, {"meta": {"count": 3, "next_cursor": "abc"}, "results": [WORK, WORK]}
                ),
                FakeResponse(200, {"meta": {"count": 3, "next_cursor": None}, "results": [WORK]}),
            ]
        )
        result = call(
            handlers_full.openalex_harvest,
            {"filter": "is_oa:true", "max_records": 10, "per_page": 2},
        )
        assert result["retrieved"] == 3
        assert result["pages_fetched"] == 2
        assert transport.calls[1]["params"]["cursor"] == "abc"


class TestFieldsIsFree:
    def test_serves_locally_with_no_api_call(self, fake_client, budget_reset):
        """A tool whose job is saving a wasted call must not cost one."""
        _, transport = fake_client([])
        result = call(handlers_core.openalex_fields, {"entity": "works"})
        assert result["ok"] is True
        assert "authorships.author.id" in result["filter_fields"]["authorship"]
        assert transport.calls == []

    def test_it_warns_that_filter_and_select_sets_differ(self, fake_client, budget_reset):
        fake_client([])
        result = call(handlers_core.openalex_fields, {"entity": "works"})
        assert "authors_count" in result["select_note"]


class TestNeverRaises:
    def test_garbage_args_do_not_raise(self, fake_client, budget_reset):
        fake_client([])
        for handler in (
            handlers_core.openalex_resolve,
            handlers_core.openalex_get,
            handlers_core.openalex_count,
            handlers_core.openalex_search,
            handlers_core.openalex_related,
            handlers_core.openalex_fields,
            handlers_full.openalex_classify,
            handlers_full.openalex_harvest,
        ):
            json.loads(handler("not a dict"))  # type: ignore[arg-type]

    def test_the_api_key_never_leaks_into_an_error(self, fake_client, budget_reset):
        fake_client(
            [FakeResponse(401, {"error": "Invalid or missing API key"})],
            api_key="SUPERSECRETKEY",
        )
        raw = handlers_core.openalex_count({"filter": "is_oa:true"})
        assert "SUPERSECRETKEY" not in raw
