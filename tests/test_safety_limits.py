"""Exercise spending, credential, and payload boundaries without network access."""

import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from hermes_plugins.openalex import client as client_mod
from hermes_plugins.openalex import handlers_core, runtime, shaping
from hermes_plugins.openalex.budget import tracker
from hermes_plugins.openalex.client import OpenAlexClient
from hermes_plugins.openalex.config import CacheConfig, OpenAlexConfig
from hermes_plugins.openalex.errors import LocalBudgetError, OpenAlexError, TransportError


def make_client(reply, retries=0, **kwargs):
    client = OpenAlexClient(
        OpenAlexConfig(
            cache=CacheConfig(enabled=False),
            rate_limit_per_second=0,
            retries=retries,
            **kwargs,
        )
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(reply),
        follow_redirects=False,
        headers={"Authorization": f"Bearer {client.cfg.api_key}"} if client.cfg.api_key else {},
    )
    return client


def test_concurrent_requests_reserve_budget_before_sending(budget_reset):
    entered, release = threading.Event(), threading.Event()
    sent = []

    def reply(request):
        sent.append(request)
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={"results": []}, headers={"x-ratelimit-cost-usd": "0.001"})

    client = make_client(reply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.get, "works", params={"search": "biology"}, session_id="one", budget_usd=0.001
        )
        try:
            assert entered.wait(5)
            with pytest.raises(LocalBudgetError):
                client.get(
                    "works", params={"search": "chemistry"}, session_id="one", budget_usd=0.001
                )
        finally:
            release.set()
        first.result()
    assert len(sent) == 1
    assert tracker.ledger("one").actual_usd == pytest.approx(0.001)
    assert tracker.ledger("one").reserved_usd == 0
    client.close()


@pytest.mark.parametrize(
    "status,cost,expected_attempts", [(400, "0.001", 1), (500, "0.001", 1), (429, "0", 2)]
)
def test_failed_requests_and_retries_are_budgeted(
    budget_reset, monkeypatch, status, cost, expected_attempts
):
    sent = []

    def reply(request):
        sent.append(request)
        if len(sent) > 1:
            return httpx.Response(
                200, json={"results": []}, headers={"x-ratelimit-cost-usd": "0.001"}
            )
        return httpx.Response(
            status,
            json={"message": "failed"},
            headers={
                "x-ratelimit-cost-usd": cost,
                "retry-after": "1",
            },
        )

    client = make_client(reply, retries=1)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _: None)
    try:
        client.get("works", params={"search": "biology"}, budget_usd=0.001)
    except OpenAlexError:
        assert status != 429
    assert len(sent) == expected_attempts
    assert tracker.ledger().actual_usd == pytest.approx(0.001)
    assert tracker.ledger().reserved_usd == 0
    client.close()


def test_unknown_transport_cost_retains_estimate(budget_reset):
    def reply(request):
        raise httpx.ReadTimeout("timeout", request=request)

    client = make_client(reply)
    with pytest.raises(TransportError):
        client.get("works", params={"search": "biology"}, budget_usd=0.001)
    with pytest.raises(LocalBudgetError):
        client.get("works", params={"search": "biology"}, budget_usd=0.001)
    assert tracker.ledger().reserved_usd == 0
    client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://content.openalex.org/paper",
        "https://unexpected.example/paper",
        "https://content.openalex.org.unexpected.example/paper",
        "https://user@content.openalex.org/paper",
        "https://content.openalex.org:444/paper",
    ],
)
def test_untrusted_content_urls_never_receive_credentials(url, budget_reset):
    sent = []
    client = make_client(lambda request: sent.append(request), api_key="TEST-KEY")
    with pytest.raises(TransportError):
        client.get_content(url)
    assert not sent
    assert tracker.ledger().calls == 0
    client.close()


@pytest.mark.parametrize("kind", ["plain", "gzip", "wire_limit", "expanded_limit", "redirect"])
def test_streamed_content_limits_and_redirects(kind, monkeypatch, budget_reset):
    monkeypatch.setattr(client_mod, "MAX_CONTENT_BYTES", 1024, raising=False)
    monkeypatch.setattr(client_mod, "MAX_EXPANDED_BYTES", 2048, raising=False)
    text = b"<TEI>scientific text</TEI>"
    payload = {
        "plain": text,
        "gzip": gzip.compress(text),
        "wire_limit": b"x" * 1025,
        "expanded_limit": gzip.compress(b"x" * 2049),
        "redirect": b"",
    }[kind]
    sent = []

    def reply(request):
        sent.append(request)
        return httpx.Response(
            302 if kind == "redirect" else 200,
            stream=httpx.ByteStream(payload),
            headers={
                "location": "https://unexpected.example/paper",
                "x-ratelimit-cost-usd": "0.01",
            },
        )

    client = make_client(reply, api_key="TEST-KEY")
    if kind in ("plain", "gzip"):
        assert client.get_content("https://content.openalex.org/paper") == text.decode()
    else:
        with pytest.raises(OpenAlexError):
            client.get_content("https://content.openalex.org/paper")
    assert len(sent) == 1
    assert sent[0].headers["authorization"] == "Bearer TEST-KEY"
    assert sent[0].headers["accept-encoding"] == "identity"
    assert tracker.ledger().actual_usd == pytest.approx(0.01)
    client.close()


def test_nested_error_payload_and_logs_redact_key(isolated_config, caplog):
    key = 'TEST-KEY-"quoted"'
    isolated_config["api_key"] = key

    @runtime.tool
    def typed(args):
        raise OpenAlexError(key, details={"nested": [key, {"url": f"https://x?api_key={key}"}]})

    @runtime.tool
    def unexpected(args):
        raise ValueError(key)

    for handler in (typed, unexpected):
        result = json.loads(handler({}))
        assert key not in str(result)
        assert not result["ok"]
    assert key not in caplog.text


def test_upstream_error_is_redacted_before_reaching_cli(budget_reset):
    key = "TEST-KEY"
    client = make_client(lambda request: httpx.Response(401, json={"message": key}), api_key=key)
    with pytest.raises(OpenAlexError) as exc:
        client.get("works/W123")
    assert key not in str(exc.value.to_payload())
    client.close()


@pytest.mark.parametrize(
    "data",
    [
        {"full_text": '雪\n"' * 40000},
        {"record": {"id": "W123", "title": "x" * 100000}},
        {"records": [{"id": str(i), "title": "x" * 2000} for i in range(20)]},
        {"results": [{"id": str(i), "abstract": "x" * 5000} for i in range(20)]},
        {str(i): "x" * 50 for i in range(10000)},
    ],
)
@pytest.mark.parametrize("limit", [2000, 24000])
def test_size_limit_includes_metadata_and_truncation(data, limit):
    original = json.dumps(data)
    result = shaping.fit({"ok": True, **data, "cost": {"spent_usd": 0.01}}, limit)
    assert len(json.dumps(result)) <= limit
    assert result["_truncation"]
    assert json.dumps(data) == original


@pytest.mark.parametrize("count", [20, 75, 200])
def test_group_counts_disclose_local_and_upstream_truncation(fake_client, budget_reset, count):
    from conftest import FakeResponse

    fake_client(
        [
            FakeResponse(
                200,
                {
                    "meta": {"count": 1000, "groups_count": count},
                    "group_by": [
                        {"key": str(i), "key_display_name": str(i), "count": 1}
                        for i in range(count)
                    ],
                },
            )
        ]
    )
    result = json.loads(handlers_core.openalex_count({"group_by": "publication_year"}))
    assert result["groups_returned"] == len(result["groups"]) == min(count, 50)
    assert result["groups_available"] == count
    assert bool(result.get("groups_truncated")) == (count > 50)


@pytest.mark.parametrize("count", [20, 75])
def test_group_disclosure_survives_response_fitting(
    fake_client, budget_reset, isolated_config, count
):
    from conftest import FakeResponse

    isolated_config["max_result_chars"] = 2000
    fake_client(
        [
            FakeResponse(
                200,
                {
                    "meta": {"count": 1000, "groups_count": count},
                    "group_by": [
                        {"key": str(i), "key_display_name": "x" * 150, "count": 1}
                        for i in range(count)
                    ],
                },
            )
        ]
    )
    raw = handlers_core.openalex_count({"group_by": "institutions.id"})
    result = json.loads(raw)
    assert len(raw) <= 2000
    assert result["groups_returned"] == len(result["groups"]) < min(count, 50)
    assert result["groups_available"] == count
    assert result["groups_truncated"]
    assert "Showing 50" not in result["groups_truncated"]
