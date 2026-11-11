import inspect
import json
import re
from pathlib import Path

import httpx
import pytest

from client import (
    STAGING_BASE_URL,
    STAGING_HOST,
    ClientError,
    ShortenerClient,
    classify_analytics,
    error_object,
    guard_path,
    resolve_idempotency,
    validate_base_url,
    validate_date_range,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client():
    return ShortenerClient(STAGING_BASE_URL, "stage-test-simple", "token-test")


@pytest.fixture(autouse=True)
def _block_unmocked_http(respx_mock):
    respx_mock.assert_all_mocked = True
    yield respx_mock


def test_padded_access_token_is_sent_without_becoming_a_connection_failure(respx_mock):
    route = respx_mock.get(url__regex=r".*/api/v1/links(?:\?.*)?$").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": "unauthorized",
                    "message": "A valid bearer token is required",
                    "requestId": "req-test",
                }
            },
        )
    )
    blank = ShortenerClient(STAGING_BASE_URL, "stage-test-simple", " \n")
    with pytest.raises(ClientError):
        blank.list_links(limit=1)
    assert route.call_count == 0
    padded = ShortenerClient(STAGING_BASE_URL, " stage-test-simple\n", "Bearer token-test \n")
    result = padded.list_links(limit=1)
    assert result.transport_error is None
    assert result.status_code == 401
    sent = route.calls[0].request.headers
    assert sent["authorization"] == "Bearer token-test"
    assert sent["x-tenant-id"] == "stage-test-simple"


def test_health_and_ready_omit_auth_headers(client, respx_mock):
    health = respx_mock.get(f"{STAGING_BASE_URL}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    ready = respx_mock.get(f"{STAGING_BASE_URL}/ready").mock(
        return_value=httpx.Response(200, json={"status": "ready"})
    )
    assert client.health().parsed == {"status": "ok"}
    assert client.ready().parsed == {"status": "ready"}
    for route in (health, ready):
        headers = route.calls[0].request.headers
        assert "authorization" not in headers
        assert "x-tenant-id" not in headers


def test_api_call_sends_bearer_and_tenant_and_does_not_follow_redirect(client, respx_mock):
    route = respx_mock.get(f"{STAGING_BASE_URL}/api/v1/links/link-test").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/landing"})
    )
    result = client.get_link("link-test")
    assert result.status_code == 302
    assert result.path == "/api/v1/links/link-test"
    assert result.headers["location"] == "https://example.com/landing"
    assert route.call_count == 1
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer token-test"
    assert sent.headers["x-tenant-id"] == "stage-test-simple"
    assert sent.headers["accept"] == "application/json"


def test_idempotency_helper_reuses_key_until_the_body_changes():
    store: dict = {}
    body = {
        "domainId": "domain-test",
        "destinationUrl": "https://example.com/a",
        "queryPolicy": {"mode": "allowlist", "allowlist": ["b", "a"]},
    }
    key, canonical = resolve_idempotency(store, "create_link", body)
    again, canonical_again = resolve_idempotency(store, "create_link", body)
    assert key == again
    assert canonical == canonical_again
    assert canonical.index('"b"') < canonical.index('"a"')
    assert re.fullmatch(r"[\x21-\x7e]{1,128}", key)
    changed, _canonical = resolve_idempotency(
        store,
        "create_link",
        {**body, "destinationUrl": "https://example.com/b"},
    )
    assert changed != key


def test_create_reuses_key_and_body_after_timeout(client, respx_mock):
    link = load("link.json")
    route = respx_mock.post(f"{STAGING_BASE_URL}/api/v1/links").mock(
        side_effect=[
            httpx.ReadTimeout("timed out"),
            httpx.Response(
                201,
                json=link,
                headers={"ETag": '"3"', "Location": "/api/v1/links/link-test"},
            ),
            httpx.Response(
                201,
                json=link,
                headers={"ETag": '"3"', "Location": "/api/v1/links/link-test"},
            ),
        ]
    )
    body = {
        "domainId": "domain-test",
        "destinationUrl": "https://example.com/a",
        "caseInsensitive": False,
    }
    first = client.create_link(body)
    assert first.status_code is None
    assert first.transport_error == "timeout"
    second = client.create_link(body)
    assert second.status_code == 201
    first_request = route.calls[0].request
    second_request = route.calls[1].request
    assert first_request.headers["idempotency-key"] == second_request.headers["idempotency-key"]
    assert first_request.content == second_request.content
    third = client.create_link({**body, "destinationUrl": "https://example.com/b"})
    assert third.status_code == 201
    assert route.calls[2].request.headers["idempotency-key"] != first_request.headers["idempotency-key"]
    assert route.calls[2].request.content != first_request.content


def test_connection_error_is_a_transport_failure(client, respx_mock):
    respx_mock.get(f"{STAGING_BASE_URL}/health").mock(side_effect=httpx.ConnectError("failed"))
    result = client.health()
    assert result.transport_error == "connection"
    assert result.status_code is None


def test_patch_and_delete_send_quoted_if_match_and_omit_unchanged_fields(client, respx_mock):
    link = load("link.json")
    patch = respx_mock.patch(f"{STAGING_BASE_URL}/api/v1/links/link-test").mock(
        return_value=httpx.Response(200, json=link, headers={"ETag": '"4"'})
    )
    delete = respx_mock.delete(f"{STAGING_BASE_URL}/api/v1/links/link-test").mock(
        return_value=httpx.Response(204)
    )
    patched = client.patch_link("link-test", '"3"', {"destinationUrl": "https://example.com/new"})
    sent = json.loads(patch.calls[0].request.content)
    assert sent == {"destinationUrl": "https://example.com/new"}
    assert "status" not in sent
    assert patch.calls[0].request.headers["if-match"] == '"3"'
    assert patch.calls[0].request.headers["content-type"] == "application/json"
    deleted = client.delete_link("link-test", '"4"')
    assert delete.calls[0].request.headers["if-match"] == '"4"'
    assert deleted.status_code == 204
    assert deleted.text == ""
    assert deleted.parsed is None
    assert patched.parsed["id"] == "link-test"


def test_list_passes_cursor_only_when_supplied_and_omits_empty_filters(client, respx_mock):
    page = load("list.json")
    route = respx_mock.get(f"{STAGING_BASE_URL}/api/v1/links").mock(
        return_value=httpx.Response(200, json=page)
    )
    result = client.list_links(domain_id="", status=None, search="", limit=50)
    assert dict(route.calls[0].request.url.params) == {"limit": "50"}
    assert result.parsed["items"][0]["id"] == "link-test"
    assert result.parsed["nextCursor"] == "cursor-test"
    client.list_links(
        limit=25,
        cursor="cursor-test",
        domain_id="domain-test",
        status="active",
        search="slug",
    )
    assert dict(route.calls[1].request.url.params) == {
        "limit": "25",
        "domainId": "domain-test",
        "status": "active",
        "search": "slug",
        "cursor": "cursor-test",
    }


def test_stats_and_analytics_reject_inverted_or_367_day_ranges_without_http(client, respx_mock):
    route = respx_mock.get(url__regex=r".*").mock(return_value=httpx.Response(500))
    ranges = (("2026-02-01", "2026-01-01"), ("2026-01-01", "2027-01-03"), ("2024-01-01", "2025-01-02"))
    for start, end in ranges:
        with pytest.raises(ValueError):
            client.tenant_stats(start, end)
        with pytest.raises(ValueError):
            client.link_stats("link-test", start, end)
        with pytest.raises(ValueError):
            client.tenant_analytics(start, end, "country")
        with pytest.raises(ValueError):
            client.link_analytics("link-test", start, end, "referrer")
    assert route.call_count == 0
    validate_date_range("2026-01-01", "2027-01-02")
    validate_date_range("2024-01-01", "2025-01-01")


def test_analytics_rejects_deferred_groupings_without_http(client, respx_mock):
    route = respx_mock.get(url__regex=r".*").mock(return_value=httpx.Response(500))
    for group in ("device", "browser", "os", "hour"):
        with pytest.raises(ValueError):
            client.tenant_analytics("2026-11-04", "2026-11-12", group)
    assert route.call_count == 0


def test_analytics_empty_404_is_not_configured_and_json_404_is_not_found(client, respx_mock):
    error = load("error.json")
    route = respx_mock.get(url__regex=r".*/api/v1/analytics(?:\?.*)?$").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(404, json=error),
        ]
    )
    empty = client.tenant_analytics("2026-11-04", "2026-11-12", "country")
    assert classify_analytics(empty) == "not_configured"
    assert empty.parsed is None
    assert empty.text == ""
    found = client.tenant_analytics("2026-11-04", "2026-11-12", "referrer")
    assert classify_analytics(found) == "not_found"
    assert error_object(found)["code"] == "not_found"
    assert route.call_count == 2


def test_no_observations_is_returned_as_a_report(client, respx_mock):
    body = load("analytics_no_observations.json")
    respx_mock.get(url__regex=r".*/api/v1/links/link-test/analytics(?:\?.*)?$").mock(
        return_value=httpx.Response(200, json=body)
    )
    result = client.link_analytics("link-test", "2026-11-04", "2026-11-12", "country")
    assert classify_analytics(result) == "report"
    assert result.parsed == body
    assert result.parsed["resultStatus"] == "no_observations"
    assert result.parsed["data"] == []
    assert "total" not in result.parsed


def test_recorded_stats_returns_the_fixture_rows_only(client, respx_mock):
    body = load("stats.json")
    route = respx_mock.get(f"{STAGING_BASE_URL}/api/v1/stats").mock(
        return_value=httpx.Response(200, json=body)
    )
    result = client.tenant_stats("2026-11-04", "2026-11-12")
    assert result.parsed == body
    assert [row["date"] for row in result.parsed["daily"]] == ["2026-11-11"]
    assert dict(route.calls[0].request.url.params) == {
        "start": "2026-11-04",
        "end": "2026-11-12",
        "format": "json",
    }
    csv_route = respx_mock.get(f"{STAGING_BASE_URL}/api/v1/links/link-test/stats").mock(
        return_value=httpx.Response(
            200,
            text="date,recorded\r\n2026-11-11,2\r\n",
            headers={"Content-Type": "text/csv"},
        )
    )
    csv_result = client.link_stats("link-test", "2026-11-04", "2026-11-12", response_format="csv")
    assert csv_route.calls[0].request.headers["accept"] == "text/csv"
    assert csv_result.parsed is None
    assert csv_result.text == "date,recorded\r\n2026-11-11,2\r\n"


def test_base_url_validation():
    assert validate_base_url(STAGING_BASE_URL, False) == STAGING_BASE_URL
    assert validate_base_url("http://127.0.0.1:8787", True) == "http://127.0.0.1:8787"
    assert validate_base_url("http://localhost:8787", True) == "http://localhost:8787"
    with pytest.raises(ValueError):
        validate_base_url("http://127.0.0.1:8787", False)
    with pytest.raises(ValueError):
        validate_base_url("http://example.com", True)
    with pytest.raises(ValueError):
        validate_base_url("https://example.com", False)
    with pytest.raises(ValueError):
        validate_base_url(f"https://user:pass@{STAGING_HOST}", False)
    with pytest.raises(ValueError):
        validate_base_url(f"https://user@{STAGING_HOST}/", True)
    with pytest.raises(ValueError):
        validate_base_url(f"{STAGING_BASE_URL}?token=nope", True)


def test_client_does_not_request_a_public_slug():
    source = (ROOT / "client.py").read_text(encoding="utf-8")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "import streamlit" not in source
    assert "/{slug}" not in source
    assert "st.cache_data" not in app_source
    assert "st.cache_resource" not in app_source
    public = [
        name
        for name in dir(ShortenerClient)
        if callable(getattr(ShortenerClient, name)) and not name.startswith("_")
    ]
    assert {"redirect", "head", "get_slug", "follow"}.isdisjoint(public)
    joined = "\n".join(inspect.getsource(getattr(ShortenerClient, name)) for name in public)
    assert re.search(r"""["']/(?!api/v1|health|ready)""", joined) is None
    with pytest.raises(ValueError):
        guard_path("/slug-test")
    with pytest.raises(ValueError):
        guard_path("/")
    guard_path("/health")
    guard_path("/ready")
    guard_path("/api/v1/links/link-test")
