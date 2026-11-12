"""UI workflows with synthetic credentials and strictly mocked Worker requests."""

from datetime import date
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://shortener.example"
TOKEN = "synthetic.access.token"


@pytest.fixture(autouse=True)
def block_real_http(respx_mock):
    respx_mock.assert_all_mocked = True
    yield


def app_instance(*, signed_in=True):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    app.secrets["AUTH_USERNAME"] = "unit-operator"
    app.secrets["AUTH_PASSWORD"] = "synthetic-console-password"
    app.secrets["SHORTENER_BASE_URL"] = ORIGIN
    app.session_state["basic_auth_ok"] = signed_in
    if signed_in:
        app.session_state["access_token"] = TOKEN
    return run(app)


def run(app):
    app.run()
    assert not app.exception
    return app


def click(app, label):
    next(button for button in app.button if button.label == label).click()
    return run(app)


def fixture(name):
    return json.loads((ROOT / "tests" / "fixtures" / name).read_text())


def seed_private_state(app):
    link = fixture("link.json")
    app.session_state["selected_link"] = link
    app.session_state["selected_etag"] = '"3"'
    app.session_state["idempotency"] = {"old-operation": {"key": "synthetic-old-key"}}
    app.text_input(key="lookup_slug").set_value("old-user-draft")
    run(app)


def assert_no_private_state(app):
    assert not app.session_state["access_token"]
    assert app.session_state["results"] == {}
    assert app.session_state["links"]["items"] == []
    assert app.session_state["links"]["next_cursor"] is None
    assert app.session_state["selected_link"] is None
    assert app.session_state["selected_etag"] is None
    assert app.session_state["idempotency"] == {}
    assert app.text_input(key="lookup_slug").value == ""


@pytest.mark.parametrize("control", ["Clear access JWT", "Clear session"])
def test_clear_credentials_restores_warnings_and_blocks_requests(control, respx_mock):
    route = respx_mock.get(f"{ORIGIN}/api/v1/links").respond(200, json=fixture("list.json"))
    app = app_instance()
    click(app, "List")
    seed_private_state(app)
    click(app, control)
    assert_no_private_state(app)
    warnings = [item.value for item in app.warning]
    for text in (
        "No access JWT yet. API calls stay blocked until you paste one.",
        "Paste the access JWT on the Auth tab before listing links.",
        "Paste the access JWT on the Auth tab before loading stats.",
        "Paste the access JWT on the Auth tab. Opening this tab does not call the API.",
        "Paste the access JWT on the Auth tab before adding a member.",
    ):
        assert text in warnings
    click(app, "List")
    click(app, "Tenant stats")
    click(app, "Country")
    app.text_input(key="member_issuer").set_value("https://identity.example")
    app.text_input(key="member_subject").set_value("synthetic-subject")
    click(app, "Add member")
    assert route.call_count == 1
    assert len(respx_mock.calls) == 1
    assert any("access JWT is missing" in error.value for error in app.error)


def test_sign_out_does_not_leak_cached_data_to_next_login(respx_mock):
    respx_mock.get(f"{ORIGIN}/api/v1/links").respond(200, json=fixture("list.json"))
    app = app_instance()
    click(app, "List")
    seed_private_state(app)
    click(app, "Sign out")
    assert not app.tabs
    app.secrets["AUTH_USERNAME"] = "next-operator"
    app.secrets["AUTH_PASSWORD"] = "synthetic-next-password"
    app.text_input(key="basic_username").set_value("next-operator")
    app.text_input(key="basic_password").set_value("synthetic-next-password")
    click(app, "Sign in")
    assert app.tabs
    assert_no_private_state(app)
    assert not app.dataframe


def test_independent_browser_sessions_do_not_share_loaded_data(respx_mock):
    respx_mock.get(f"{ORIGIN}/api/v1/links").respond(200, json=fixture("list.json"))
    first = app_instance()
    click(first, "List")
    seed_private_state(first)
    second = app_instance()
    assert second.session_state["links"]["items"] == []
    assert second.session_state["selected_link"] is None
    assert second.session_state["idempotency"] == {}
    assert second.text_input(key="lookup_slug").value == ""
    assert first.session_state["selected_link"]["id"] == "link-test"


@pytest.mark.parametrize("button", ["Tenant stats", "Link stats"])
@pytest.mark.parametrize("start,end", [
    (date(2026, 2, 1), date(2026, 1, 1)),
    (date(2026, 1, 1), date(2026, 1, 1)),
    (date(2026, 1, 1), date(2027, 1, 3)),
])
def test_invalid_date_ranges_show_error_without_http(button, start, end, respx_mock):
    app = app_instance()
    app.date_input(key="stats_start").set_value(start)
    app.date_input(key="stats_end").set_value(end)
    app.text_input(key="stats_link_id").set_value("link-test")
    click(app, button)
    assert any("forward range of at most 366 days" in error.value for error in app.error)
    assert not respx_mock.calls


@pytest.mark.parametrize("button", ["Link stats", "Download link CSV"])
def test_missing_link_id_shows_error_without_http(button, respx_mock):
    app = app_instance()
    click(app, button)
    assert any("link id" in error.value.lower() for error in app.error)
    assert not respx_mock.calls


@pytest.mark.parametrize("issuer,subject,expected", [
    ("", "synthetic-subject", "Issuer"),
    ("not-a-url", "synthetic-subject", "Issuer"),
    ("https://identity.example", "contains spaces", "Subject"),
    ("https://identity.example", "x" * 256, "Subject"),
])
def test_invalid_member_input_shows_error_without_http(issuer, subject, expected, respx_mock):
    app = app_instance()
    app.text_input(key="member_issuer").set_value(issuer)
    app.text_input(key="member_subject").set_value(subject)
    click(app, "Add member")
    assert any(expected in error.value for error in app.error)
    assert not respx_mock.calls


def test_list_filters_pagination_and_cursor_reset(respx_mock):
    first_page = fixture("list.json")
    second_link = {**fixture("link.json"), "id": "link-next", "slug": "next-slug"}
    route = respx_mock.get(f"{ORIGIN}/api/v1/links").mock(side_effect=[
        httpx.Response(200, json=first_page),
        httpx.Response(200, json={"items": [second_link], "nextCursor": None}),
    ])
    app = app_instance()
    assert next(item for item in app.button if item.label == "Next page").disabled
    app.text_input(key="list_domain").set_value("domain-test")
    app.selectbox(key="list_status").set_value("active")
    app.text_input(key="list_search").set_value("slug")
    app.number_input(key="list_limit").set_value(25)
    click(app, "List")
    assert dict(route.calls[0].request.url.params) == {
        "domainId": "domain-test", "status": "active", "search": "slug", "limit": "25",
    }
    assert app.dataframe[0].value["id"].tolist() == ["link-test"]
    click(app, "Next page")
    assert route.calls[1].request.url.params["cursor"] == "cursor-test"
    assert app.dataframe[0].value["id"].tolist() == ["link-next"]
    assert next(item for item in app.button if item.label == "Next page").disabled
    # A draft filter change must invalidate an old continuation token before another request.
    app.session_state["links"] = {**app.session_state["links"], "next_cursor": "old-cursor"}
    app.text_input(key="list_search").set_value("changed")
    run(app)
    assert app.session_state["links"]["next_cursor"] is None
    assert next(item for item in app.button if item.label == "Next page").disabled
    assert route.call_count == 2


def test_lookup_loads_exact_identity_and_selected_link_details(respx_mock):
    link = {**fixture("link.json"), "slug": "Slug-Case"}
    route = respx_mock.get(f"{ORIGIN}/api/v1/domains/domain-test/links/Slug-Case").respond(
        200, json=link, headers={"ETag": '"3"'}
    )
    app = app_instance()
    app.text_input(key="lookup_domain").set_value("domain-test")
    app.text_input(key="lookup_slug").set_value("Slug-Case")
    click(app, "Lookup")
    assert route.call_count == 1
    assert app.session_state["selected_link"] == link
    assert app.session_state["selected_etag"] == '"3"'
    assert any(item.value == "ETag matches revision." for item in app.caption)
    assert any(item.value == link["shortUrl"] for item in app.code)
    click(app, "Use selected link")
    assert app.text_input(key="stats_link_id").value == "link-test"


def test_lookup_not_found_displays_error(respx_mock):
    respx_mock.get(f"{ORIGIN}/api/v1/domains/domain-test/links/missing").respond(404)
    app = app_instance()
    app.text_input(key="lookup_domain").set_value("domain-test")
    app.text_input(key="lookup_slug").set_value("missing")
    click(app, "Lookup")
    assert any(error.value == "The link is gone." for error in app.error)
    assert app.session_state["selected_link"] is None


def test_load_link_displays_revision_mismatch(respx_mock):
    respx_mock.get(f"{ORIGIN}/api/v1/links").respond(200, json=fixture("list.json"))
    route = respx_mock.get(f"{ORIGIN}/api/v1/links/link-test").respond(
        200, json=fixture("link.json"), headers={"ETag": '"4"'}
    )
    app = app_instance()
    click(app, "List")
    app.selectbox(key="link_choice").set_value("link-test")
    run(app)
    click(app, "Load link")
    assert route.call_count == 1
    assert app.session_state["selected_link"]["revision"] == 3
    assert any("ETag and revision disagree" in error.value for error in app.error)


@pytest.mark.parametrize("scope", ["tenant", "link"])
def test_recorded_stats_shows_total_and_only_returned_daily_rows(scope, respx_mock):
    path = "/api/v1/stats" if scope == "tenant" else "/api/v1/links/link-test/stats"
    respx_mock.get(ORIGIN + path).respond(200, json=fixture("stats.json"))
    app = app_instance()
    app.text_input(key="stats_link_id").set_value("link-test")
    click(app, "Tenant stats" if scope == "tenant" else "Link stats")
    result = app.session_state["results"][f"{scope}_stats"]
    assert result.parsed["total"] == 2
    assert any("total" in item.value and "2" in item.value for item in app.markdown)
    assert app.dataframe[0].value.to_dict("records") == [{"date": "2026-11-11", "recorded": 2}]
    assert any("Missing days mean zero" in item.value for item in app.caption)


@pytest.mark.parametrize("scope", ["tenant", "link"])
def test_empty_recorded_stats_shows_no_daily_rows(scope, respx_mock):
    path = "/api/v1/stats" if scope == "tenant" else "/api/v1/links/link-test/stats"
    respx_mock.get(ORIGIN + path).respond(200, json={"total": 0, "daily": []})
    app = app_instance()
    app.text_input(key="stats_link_id").set_value("link-test")
    click(app, "Tenant stats" if scope == "tenant" else "Link stats")
    assert not app.dataframe
    assert any(item.value == "The API returned no daily rows." for item in app.caption)


def test_tenant_stats_permission_error_suggests_link_stats(respx_mock):
    respx_mock.get(f"{ORIGIN}/api/v1/stats").respond(403, json={"error": {"code": "forbidden"}})
    app = app_instance()
    click(app, "Tenant stats")
    assert any(item.value == "This identity cannot read tenant stats. Use link stats." for item in app.warning)
    assert not app.dataframe


@pytest.mark.parametrize("scope", ["tenant", "link"])
def test_csv_download_contains_exact_response_and_filename(scope, respx_mock):
    path = "/api/v1/stats" if scope == "tenant" else "/api/v1/links/link-test/stats"
    csv = "date,recorded\r\n2026-11-11,2\r\n"
    route = respx_mock.get(ORIGIN + path).respond(200, text=csv, content_type="text/csv")
    app = app_instance()
    app.text_input(key="stats_link_id").set_value("link-test")
    with patch("streamlit.download_button", wraps=st.download_button) as download:
        click(app, f"Download {scope} CSV")
    assert route.calls[0].request.url.params["format"] == "csv"
    assert route.calls[0].request.headers["accept"] == "text/csv"
    assert download.call_count == 1
    assert download.call_args.kwargs["data"] == csv
    assert download.call_args.kwargs["file_name"] == f"{scope}-stats.csv"
    assert download.call_args.kwargs["mime"] == "text/csv"


def test_session_health_and_readiness_render_successful_responses(respx_mock):
    health = respx_mock.get(f"{ORIGIN}/health").respond(200, json={"status": "ok"})
    ready = respx_mock.get(f"{ORIGIN}/ready").respond(200, json={"status": "ready"})
    app = app_instance()
    click(app, "Check health")
    click(app, "Check ready")
    assert {item.value for item in app.success} >= {'{"status":"ok"}', '{"status":"ready"}'}
    for route in (health, ready):
        assert route.call_count == 1
        assert "authorization" not in route.calls[0].request.headers
