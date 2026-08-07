"""The cost model is the reason this plugin exists, so it gets the most tests."""

from __future__ import annotations

import pytest
from hermes_plugins.openalex import pricing
from hermes_plugins.openalex.budget import BudgetTracker
from hermes_plugins.openalex.errors import LocalBudgetError


class TestClassification:
    """Which price applies, worked out before the request goes out."""

    def test_prefixed_singleton_is_free(self):
        assert pricing.classify_request("works/W2741809807", {}) == pricing.SINGLETON
        assert pricing.classify_request("works/doi:10.7717/peerj.4375", {}) == pricing.SINGLETON
        assert (
            pricing.classify_request("authors/orcid:0000-0002-1298-3089", {}) == pricing.SINGLETON
        )
        assert pricing.cost_of(pricing.SINGLETON) == 0.0

    def test_url_form_singleton_is_billed(self):
        """OpenAlex charges list price for the URL form of an id, free for the prefix."""
        assert (
            pricing.classify_request("works/https://doi.org/10.7717/peerj.4375", {}) == pricing.LIST
        )

    def test_autocomplete_is_free(self):
        assert (
            pricing.classify_request("autocomplete/authors", {"q": "bengio"}) == pricing.SINGLETON
        )

    def test_plain_list_is_list_priced(self):
        assert pricing.classify_request("works", {"filter": "is_oa:true"}) == pricing.LIST

    def test_search_is_ten_times_a_list(self):
        assert pricing.classify_request("works", {"search": "neural"}) == pricing.SEARCH
        assert pricing.cost_of(pricing.SEARCH) == pytest.approx(10 * pricing.cost_of(pricing.LIST))

    def test_search_filter_is_also_search_priced(self):
        """A *.search: filter is not a cheap back door around search pricing."""
        assert (
            pricing.classify_request("works", {"filter": "title_and_abstract.search:neural"})
            == pricing.SEARCH
        )

    def test_group_by_beats_search_pricing(self):
        """The saving the count tool is built around.

        OpenAlex bills a grouped query at list price even when a search term
        is present, so adding group_by cuts the cost by a factor of ten.
        """
        searching = pricing.classify_request("works", {"search": "neural"})
        grouped = pricing.classify_request(
            "works", {"search": "neural", "group_by": "publication_year"}
        )
        assert searching == pricing.SEARCH
        assert grouped == pricing.LIST
        assert pricing.cost_of(grouped) * 10 == pytest.approx(pricing.cost_of(searching))

    def test_text_classification_is_the_most_expensive(self):
        assert pricing.classify_request("text/topics", {"title": "x"}) == pricing.TEXT
        assert pricing.cost_of(pricing.TEXT) == pytest.approx(100 * pricing.cost_of(pricing.LIST))

    def test_random_is_billed_despite_looking_like_a_singleton(self):
        assert pricing.classify_request("works/random", {}) == pricing.LIST


class TestDailyBudgets:
    def test_anonymous_budget_is_ten_cents(self):
        """llms.txt claims $0.01. The live header says $0.10."""
        assert pricing.ANON_DAILY_USD == 0.10

    def test_a_free_key_is_worth_ten_times_the_budget(self):
        assert pricing.KEYED_DAILY_USD == pytest.approx(10 * pricing.ANON_DAILY_USD)

    def test_anonymous_day_buys_a_hundred_searches(self):
        assert int(pricing.ANON_DAILY_USD / pricing.cost_of(pricing.SEARCH)) == 100

    def test_anonymous_day_buys_a_thousand_counts(self):
        assert int(pricing.ANON_DAILY_USD / pricing.cost_of(pricing.LIST)) == 1000


class TestLivePriceRefresh:
    def test_account_prices_override_the_defaults(self):
        assert pricing.refresh_from_account({"search": 0.002}) is True
        assert pricing.cost_of(pricing.SEARCH) == 0.002

    def test_unchanged_prices_report_no_change(self):
        assert pricing.refresh_from_account({"search": pricing.cost_of(pricing.SEARCH)}) is False

    def test_junk_is_ignored_rather_than_crashing(self):
        assert pricing.refresh_from_account({"search": "free"}) is False
        assert pricing.refresh_from_account(None) is False
        assert pricing.cost_of(pricing.SEARCH) == 0.001


class TestBudget:
    def test_refuses_before_crossing_the_line(self):
        tracker = BudgetTracker()
        tracker.record(predicted=0.04, actual=0.04)
        with pytest.raises(LocalBudgetError) as exc:
            tracker.check(0.02, limit=0.05)
        assert exc.value.details["remaining_usd"] == pytest.approx(0.01)
        assert "openalex_count" in exc.value.next_step

    def test_free_calls_never_trip_the_guard(self):
        tracker = BudgetTracker()
        tracker.record(predicted=0.05, actual=0.05)
        tracker.check(0.0, limit=0.05)  # must not raise

    def test_a_call_that_exactly_fits_is_allowed(self):
        tracker = BudgetTracker()
        tracker.record(predicted=0.04, actual=0.04)
        tracker.check(0.01, limit=0.05)  # must not raise

    def test_sessions_have_separate_ledgers(self):
        tracker = BudgetTracker()
        tracker.record(predicted=0.05, actual=0.05, session_id="alpha")
        assert tracker.ledger("beta").actual_usd == 0.0
        tracker.check(0.05, limit=0.05, session_id="beta")

    def test_actual_cost_wins_over_prediction(self):
        """The headers are OpenAlex's own number, so they are the truth."""
        tracker = BudgetTracker()
        tracker.record(predicted=0.001, actual=0.0001)
        snap = tracker.ledger().snapshot(0.05)
        assert snap["spent_usd"] == pytest.approx(0.0001)
        assert snap["predicted_usd"] == pytest.approx(0.001)

    def test_free_calls_are_counted_separately(self):
        tracker = BudgetTracker()
        tracker.record(predicted=0.0, actual=0.0, call_class="singleton")
        tracker.record(predicted=0.001, actual=0.001, call_class="search")
        snap = tracker.ledger().snapshot(0.05)
        assert snap["calls"] == 2
        assert snap["free_calls"] == 1
        assert snap["calls_by_price_class"] == {"singleton": 1, "search": 1}

    def test_cache_hits_are_recorded_as_money_saved(self):
        tracker = BudgetTracker()
        tracker.record_cache_hit(0.001)
        tracker.record_cache_hit(0.001)
        snap = tracker.ledger().snapshot(0.05)
        assert snap["cache_hits"] == 2
        assert snap["saved_by_cache_usd"] == pytest.approx(0.002)

    def test_reset_clears_one_session(self):
        tracker = BudgetTracker()
        tracker.record(predicted=0.01, session_id="alpha")
        tracker.reset("alpha")
        assert tracker.ledger("alpha").actual_usd == 0.0
