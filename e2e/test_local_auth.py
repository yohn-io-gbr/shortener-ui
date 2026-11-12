from pathlib import Path
import re
import tomllib

import pytest
from playwright.sync_api import Browser, Error, Page, WebSocket, expect
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg

SECRETS = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml"


def _script_finished(payload: str | bytes) -> bool:
    if not isinstance(payload, bytes):
        return False
    message = ForwardMsg()
    message.ParseFromString(payload)
    return message.WhichOneof("type") == "script_finished"


def _enter_local_credentials(page: Page, socket: WebSocket) -> None:
    try:
        with SECRETS.open("rb") as source:
            secrets = tomllib.load(source)
        username = secrets["AUTH_USERNAME"]
        password = secrets["AUTH_PASSWORD"]
        if not all(isinstance(value, str) and value for value in (username, password)):
            raise ValueError("Missing console credentials")
    except (OSError, ValueError, KeyError):
        raise pytest.fail.Exception(
            "Set AUTH_USERNAME and AUTH_PASSWORD in .streamlit/secrets.toml", pytrace=False
        ) from None

    try:
        page.get_by_label("Username", exact=True).fill(username)
        with socket.expect_event("framereceived", predicate=_script_finished):
            page.get_by_label("Username", exact=True).press("Enter")
        page.get_by_label("Password", exact=True).fill(password)
        with socket.expect_event("framereceived", predicate=_script_finished):
            page.get_by_label("Password", exact=True).press("Enter")
    except Error:
        # Playwright's error logs can contain the values passed to fill().
        raise pytest.fail.Exception("Could not enter local console credentials", pytrace=False) from None


@pytest.mark.e2e
def test_local_credentials_open_console(browser: Browser):
    try:
        _check_local_login(browser)
    except (AssertionError, Error):
        # Never include locator diagnostics: ARIA snapshots can expose form values.
        raise pytest.fail.Exception("Local authentication check failed", pytrace=False) from None


def _check_local_login(browser: Browser) -> None:
    # A separate context avoids plugin traces, videos, and screenshots of real credentials.
    with browser.new_context() as context:
        page = context.new_page()
        _sign_in(page)
        panel = page.get_by_role("tabpanel")
        for name in (
            "AWS region", "User pool ID", "App client ID", "Email / username",
            "Cognito password", "App client secret (optional)",
        ):
            field = panel.get_by_label(name, exact=True).and_(page.locator("input"))
            expect(field).to_be_visible()
            expect(field).to_be_enabled()
        for name in ("Cognito password", "App client secret (optional)"):
            expect(panel.get_by_label(name, exact=True)).to_have_attribute("type", "password")
        generate = panel.get_by_role("button", name="generate JWT", exact=True)
        expect(generate).to_be_visible()
        expect(generate).to_be_disabled()
        expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
        page.get_by_role("button", name="Sign out", exact=True).click()
        expect(page.get_by_label("Password", exact=True)).to_be_visible()
        expect(page.get_by_role("tab", name="Auth", exact=True)).to_have_count(0)


def _sign_in(page: Page) -> WebSocket:
    with page.expect_websocket() as connection:
        response = page.goto("http://localhost:8501/")
    assert response is not None and response.status == 200
    expect(page.get_by_label("Username", exact=True)).to_be_visible(timeout=15000)
    _enter_local_credentials(page, connection.value)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("tab", name="Auth", exact=True)).to_be_visible(timeout=15000)
    return connection.value


@pytest.mark.e2e
def test_all_console_tabs_can_be_clicked_after_login(browser: Browser):
    try:
        # Keep real credentials out of plugin-recorded artifacts.
        with browser.new_context() as context:
            page = context.new_page()
            _sign_in(page)
            names = ["Auth", "Session", "Links", "Recorded stats", "Sampled analytics", "Members"]
            expect(page.get_by_role("tab")).to_have_text(names)
            for name in names:
                tab = page.get_by_role("tab", name=name, exact=True)
                expect(tab).to_be_visible()
                tab.click()
                expect(tab).to_have_attribute("aria-selected", "true")
                expect(page.get_by_role("tabpanel")).to_be_visible()
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
    except (AssertionError, Error):
        raise pytest.fail.Exception("Local console tab check failed", pytrace=False) from None


@pytest.mark.e2e
def test_links_sections_and_lookup_button_are_available(browser: Browser):
    try:
        with browser.new_context() as context:
            page = context.new_page()
            _sign_in(page)
            links = page.get_by_role("tab", name="Links", exact=True)
            links.click()
            expect(links).to_have_attribute("aria-selected", "true")
            panel = page.get_by_role("tabpanel")
            for name in ("Lookup", "Loaded link", "Create"):
                expect(panel.get_by_role("heading", name=name, exact=True)).to_be_visible()
            lookup = panel.get_by_role("button", name="Lookup", exact=True)
            expect(lookup).to_be_visible()
            expect(lookup).to_be_enabled()
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
    except (AssertionError, Error):
        raise pytest.fail.Exception(
            "Links Lookup, Loaded link, Create, or enabled Lookup button was not available", pytrace=False
        ) from None


@pytest.mark.e2e
def test_session_check_health_and_check_ready(browser: Browser):
    stage = "console sign-in"
    try:
        with browser.new_context() as context:
            page = context.new_page()
            _sign_in(page)
            session = page.get_by_role("tab", name="Session", exact=True)
            session.click()
            expect(session).to_have_attribute("aria-selected", "true")
            panel = page.get_by_role("tabpanel")
            failures = []
            checks = [
                ("Check health", '{"status":"ok"}'),
                ("Check ready", '{"status":"ready"}'),
            ]
            for index, (button, body) in enumerate(checks):
                stage = button
                expect(panel.get_by_text(body, exact=True)).to_have_count(0)
                panel.get_by_role("button", name=button, exact=True).click()
                status = panel.get_by_text(re.compile(
                    r"^(HTTP \d{3}|Transport failure \((?:connection|timeout|header)\)\.)$"
                )).nth(index)
                expect(status).to_be_visible(timeout=30000)
                if status.inner_text() != "HTTP 200":
                    failures.append(f"{button}: {status.inner_text()}")
                else:
                    try:
                        expect(panel.get_by_text(body, exact=True)).to_be_visible()
                    except AssertionError:
                        failures.append(f"{button}: expected {body} was not visible")
            stage = "sign-out"
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
            if failures:
                raise pytest.fail.Exception("; ".join(failures), pytrace=False) from None
    except (AssertionError, Error):
        raise pytest.fail.Exception(
            f"Session test failed at {stage}", pytrace=False
        ) from None


def _local_access_credentials() -> dict[str, str]:
    try:
        with SECRETS.open("rb") as source:
            settings = tomllib.load(source)["local_tests"]
        required = ("tenant_id", "user", "pass", "cognito_region", "cognito_user_pool_id", "cognito_client_id")
        if not all(isinstance(settings.get(key), str) and settings[key].strip() for key in required):
            raise ValueError("Missing local test settings")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", settings["cognito_region"]):
            raise ValueError("Invalid Cognito region")
        if not isinstance(settings.get("cognito_client_secret", ""), str):
            raise ValueError("Invalid Cognito client secret")
        return settings
    except (OSError, ValueError, KeyError, TypeError):
        raise pytest.fail.Exception(
            "Set tenant_id, user, pass, cognito_region, cognito_user_pool_id, and cognito_client_id under "
            "[local_tests] in .streamlit/secrets.toml", pytrace=False
        ) from None


def _fill_and_commit(page: Page, socket: WebSocket, label: str, value: str) -> None:
    field = page.get_by_label(label, exact=True).and_(page.locator("input"))
    if field.input_value() == value:
        return  # Streamlit does not rerun when an unchanged input is committed.
    field.fill(value)
    with socket.expect_event("framereceived", predicate=_script_finished):
        field.press("Enter")


def _submit_cognito(page: Page, socket: WebSocket) -> None:
    with socket.expect_event("framereceived", predicate=_script_finished, timeout=60000):
        page.get_by_role("button", name="generate JWT", exact=True).click()
    if page.get_by_text("Cannot reach Cognito. Check your network and AWS region.", exact=True).is_visible():
        raise pytest.fail.Exception(
            "Cognito is unreachable from the local application. Live authentication remains unverified.",
            pytrace=False,
        ) from None


@pytest.fixture(scope="session")
def cognito_ui(browser: Browser, pytestconfig):
    """One real UI login shared by the UI regression and authenticated checks."""
    if not pytestconfig.getoption("--live-cognito"):
        pytest.skip("Live Cognito UI login requires --live-cognito and ignored [local_tests] credentials")
    settings = _local_access_credentials()
    stage = "console sign-in"
    try:
        # A manual context has no pytest-plugin traces, screenshots, or videos.
        with browser.new_context() as context:
            page = context.new_page()
            socket = _sign_in(page)
            stage = "Cognito form input"
            page.get_by_role("tab", name="Auth", exact=True).click()
            for label, key in (
                ("Tenant id", "tenant_id"), ("AWS region", "cognito_region"),
                ("User pool ID", "cognito_user_pool_id"), ("App client ID", "cognito_client_id"),
                ("Email / username", "user"), ("Cognito password", "pass"),
                ("App client secret (optional)", "cognito_client_secret"),
            ):
                stage = f"Cognito form input: {label}"
                _fill_and_commit(page, socket, label, settings.get(key, ""))
            stage = "generate JWT through Cognito sign-in"
            _submit_cognito(page, socket)
            mfa = page.get_by_label("MFA code", exact=True).and_(page.locator("input"))
            if mfa.is_visible():
                stage = "MFA continuation through the UI"
                expect(mfa).to_have_attribute("type", "password")
                code = str(settings.get("mfa_code", ""))
                if not re.fullmatch(r"\d{6}", code):
                    raise pytest.fail.Exception(
                        "Cognito requires MFA. Set a valid current mfa_code in ignored [local_tests] "
                        "and rerun immediately. Live UI authentication remains unverified.", pytrace=False,
                    ) from None
                _fill_and_commit(page, socket, "MFA code", code)
                _submit_cognito(page, socket)
            stage = "generated JWT confirmation"
            expect(page.get_by_text(
                "Access JWT generated and filled for this browser session.", exact=True,
            )).to_be_visible()
            jwt = page.get_by_label("Access JWT", exact=True).and_(page.locator("input"))
            expect(jwt).to_have_value(re.compile(r"^[^.]+\.[^.]+\.[^.]+$"))
            # Keep the token in memory for the other authenticated checks.
            token = jwt.input_value()
            yield settings, token, page, socket
    except (AssertionError, Error):
        # Locator diagnostics may include passwords, codes, secrets, or tokens.
        raise pytest.fail.Exception(f"Live Cognito UI check failed at {stage}", pytrace=False) from None


@pytest.fixture(scope="session")
def cognito_auth(cognito_ui):
    """Reuse the JWT issued through the production form; never log in separately."""
    settings, token, _, _ = cognito_ui
    return settings, token


@pytest.mark.e2e
def test_cognito_ui_generates_access_jwt(cognito_ui):
    _, _, page, socket = cognito_ui
    stage = "masked JWT and cleared sensitive inputs"
    try:
        jwt = page.get_by_label("Access JWT", exact=True).and_(page.locator("input"))
        stage = "Access JWT masking"
        expect(jwt).to_have_attribute("type", "password")
        expect(jwt).not_to_have_value("")
        for name in ("Cognito password", "App client secret (optional)"):
            stage = f"cleared {name}"
            field = page.get_by_label(name, exact=True).and_(page.locator("input"))
            expect(field).to_have_attribute("type", "password")
            expect(field).to_have_value("")
        stage = "cleared MFA input and challenge"
        expect(page.get_by_label("MFA code", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Cancel Cognito sign-in", exact=True)).to_have_count(0)
        stage = "missing-token warnings after generation"
        expect(page.get_by_text(
            "No access JWT yet. API calls stay blocked until you paste one.", exact=True,
        )).to_be_hidden()
        expect(page.get_by_text("Paste the access JWT on the Auth tab.", exact=True)).to_be_hidden()
        for tab, warning in (
            ("Session", "Paste the access JWT on the Auth tab before calling the API."),
            ("Links", "Paste the access JWT on the Auth tab before listing links."),
            ("Recorded stats", "Paste the access JWT on the Auth tab before loading stats."),
            ("Sampled analytics", "Paste the access JWT on the Auth tab. Opening this tab does not call the API."),
            ("Members", "Paste the access JWT on the Auth tab before adding a member."),
        ):
            page.get_by_role("tab", name=tab, exact=True).click()
            expect(page.get_by_text(warning, exact=True)).to_be_hidden()
        stage = "sign-out and cleared session after console re-entry"
        page.get_by_role("button", name="Sign out", exact=True).click()
        expect(page.get_by_label("Password", exact=True)).to_be_visible()
        expect(page.get_by_role("tab", name="Auth", exact=True)).to_have_count(0)
        # Re-enter the console in this context to prove the JWT did not survive.
        _enter_local_credentials(page, socket)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("tab", name="Auth", exact=True)).to_be_visible()
        expect(jwt).to_have_value("")
        for name in ("Email / username", "Cognito password", "App client secret (optional)"):
            expect(page.get_by_label(name, exact=True).and_(page.locator("input"))).to_have_value("")
        expect(page.get_by_label("MFA code", exact=True)).to_have_count(0)
        expect(page.get_by_text(
            "No access JWT yet. API calls stay blocked until you paste one.", exact=True,
        )).to_be_visible()
        page.get_by_role("button", name="Sign out", exact=True).click()
    except (AssertionError, Error):
        raise pytest.fail.Exception(f"Live Cognito UI check failed at {stage}", pytrace=False) from None


def _expect_recorded_stats_controls(page: Page) -> None:
    panel = page.get_by_role("tabpanel")
    dates = panel.get_by_test_id("stDateInput")
    expect(dates).to_have_count(2)
    for index, name in enumerate(("start", "end")):
        expect(panel.get_by_text(name, exact=True)).to_be_visible()
        widget = dates.nth(index)
        expect(widget).to_be_visible()
        segments = widget.get_by_role("spinbutton")
        expect(segments).not_to_have_count(0)
        for segment in segments.all():
            expect(segment).to_be_visible()
            expect(segment).to_be_enabled()
    link_id = panel.get_by_label("Link id", exact=True).and_(page.locator("input"))
    expect(link_id).to_be_visible()
    expect(link_id).to_be_enabled()
    for name in (
        "Tenant stats", "Download tenant CSV", "Use selected link",
        "Link stats", "Download link CSV",
    ):
        button = panel.get_by_role("button", name=name, exact=True)
        expect(button).to_be_visible()
        expect(button).to_be_enabled()


@pytest.mark.e2e
def test_recorded_stats_controls_are_available(browser: Browser):
    try:
        with browser.new_context() as context:
            page = context.new_page()
            _sign_in(page)
            stats = page.get_by_role("tab", name="Recorded stats", exact=True)
            stats.click()
            expect(stats).to_have_attribute("aria-selected", "true")
            _expect_recorded_stats_controls(page)
            expect(page.get_by_text(
                "Paste the access JWT on the Auth tab before loading stats.", exact=True
            )).to_be_visible()
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
    except (AssertionError, Error):
        raise pytest.fail.Exception(
            "Recorded stats controls or initial missing-JWT warning were not available", pytrace=False
        ) from None


@pytest.mark.e2e
def test_recorded_stats_controls_and_warning_with_generated_access_jwt(
    browser: Browser, request: pytest.FixtureRequest
):
    settings, token = request.getfixturevalue("cognito_auth")
    stage = "console sign-in"
    try:
        with browser.new_context() as context:
            page = context.new_page()
            socket = _sign_in(page)
            stage = "Recorded stats warning before access JWT"
            stats = page.get_by_role("tab", name="Recorded stats", exact=True)
            stats.click()
            warning = page.get_by_text(
                "Paste the access JWT on the Auth tab before loading stats.", exact=True
            )
            expect(warning).to_be_visible()

            stage = "Auth fields"
            page.get_by_role("tab", name="Auth", exact=True).click()
            tenant = page.get_by_label("Tenant id", exact=True)
            tenant.fill(settings["tenant_id"])
            with socket.expect_event("framereceived", predicate=_script_finished):
                tenant.press("Enter")
            jwt = page.get_by_label("Access JWT", exact=True).and_(page.locator("input"))
            jwt.fill(token)
            with socket.expect_event("framereceived", predicate=_script_finished):
                jwt.press("Enter")
            expect(jwt).to_have_value(token)

            stage = "Recorded stats warning after access JWT"
            stats.click()
            expect(stats).to_have_attribute("aria-selected", "true")
            expect(warning).to_be_hidden()
            stage = "Recorded stats controls after access JWT"
            _expect_recorded_stats_controls(page)
            stage = "sign-out"
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
    except (AssertionError, Error):
        raise pytest.fail.Exception(
            f"Recorded stats test failed at {stage}", pytrace=False
        ) from None


def _expect_sampled_analytics_controls(page: Page) -> None:
    panel = page.get_by_role("tabpanel")
    reads = panel.get_by_text("Sampled analytics reads", exact=True)
    expect(reads).to_be_visible()
    country = panel.get_by_role("button", name="Country", exact=True)
    if not country.is_visible():
        reads.click()
    dates = panel.get_by_test_id("stDateInput")
    expect(dates).to_have_count(2)
    for index, name in enumerate(("analytics start", "analytics end")):
        expect(panel.get_by_text(name, exact=True)).to_be_visible()
        widget = dates.nth(index)
        expect(widget).to_be_visible()
        segments = widget.get_by_role("spinbutton")
        expect(segments).not_to_have_count(0)
        for segment in segments.all():
            expect(segment).to_be_visible()
            expect(segment).to_be_enabled()
    expect(panel.get_by_text("Scope", exact=True)).to_be_visible()
    for name in ("tenant", "link"):
        radio = panel.get_by_role("radio", name=name, exact=True)
        expect(radio).to_be_visible()
        expect(radio).to_be_enabled()
    for name in ("Country", "Referrer"):
        button = panel.get_by_role("button", name=name, exact=True)
        expect(button).to_be_visible()
        expect(button).to_be_enabled()

    emergency = panel.get_by_text("Emergency stop", exact=True)
    expect(emergency).to_be_visible()
    disable = panel.get_by_role("button", name="Disable analytics", exact=True)
    if not disable.is_visible():
        emergency.click()
    confirm = panel.get_by_role("checkbox", name="Confirm analytics disable", exact=True)
    expect(confirm).to_be_visible()
    expect(confirm).to_be_enabled()
    expect(confirm).not_to_be_checked()
    expect(disable).to_be_visible()
    expect(disable).to_be_disabled()


@pytest.mark.e2e
@pytest.mark.parametrize("with_jwt", [False, True], ids=["no_token", "generated_access_jwt"])
def test_sampled_analytics_controls_and_warning(
    browser: Browser, with_jwt: bool, request: pytest.FixtureRequest
):
    settings, token = request.getfixturevalue("cognito_auth") if with_jwt else (None, None)
    stage = "console sign-in"
    try:
        with browser.new_context() as context:
            page = context.new_page()
            socket = _sign_in(page)
            tab = page.get_by_role("tab", name="Sampled analytics", exact=True)
            tab.click()
            expect(tab).to_have_attribute("aria-selected", "true")
            warning = page.get_by_text(
                "Paste the access JWT on the Auth tab. Opening this tab does not call the API.",
                exact=True,
            )
            stage = "initial warning and controls"
            expect(warning).to_be_visible()
            _expect_sampled_analytics_controls(page)
            if with_jwt:
                stage = "Auth fields"
                page.get_by_role("tab", name="Auth", exact=True).click()
                tenant = page.get_by_label("Tenant id", exact=True)
                tenant.fill(settings["tenant_id"])
                with socket.expect_event("framereceived", predicate=_script_finished):
                    tenant.press("Enter")
                jwt = page.get_by_label("Access JWT", exact=True).and_(page.locator("input"))
                jwt.fill(token)
                with socket.expect_event("framereceived", predicate=_script_finished):
                    jwt.press("Enter")
                expect(jwt).to_have_value(token)
                stage = "warning and controls after access JWT"
                tab.click()
                expect(tab).to_have_attribute("aria-selected", "true")
                expect(warning).to_be_hidden()
                _expect_sampled_analytics_controls(page)
            stage = "sign-out"
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
    except (AssertionError, Error):
        raise pytest.fail.Exception(
            f"Sampled analytics test failed at {stage}", pytrace=False
        ) from None


@pytest.mark.e2e
@pytest.mark.parametrize("with_jwt", [False, True], ids=["no_token", "generated_access_jwt"])
def test_members_controls_and_warning(
    browser: Browser, with_jwt: bool, request: pytest.FixtureRequest
):
    settings, token = request.getfixturevalue("cognito_auth") if with_jwt else (None, None)
    stage = "console sign-in"
    try:
        with browser.new_context() as context:
            page = context.new_page()
            socket = _sign_in(page)
            tab = page.get_by_role("tab", name="Members", exact=True)
            tab.click()
            expect(tab).to_have_attribute("aria-selected", "true")
            warning = page.get_by_text(
                "Paste the access JWT on the Auth tab before adding a member.", exact=True
            )
            stage = "initial warning and controls"
            expect(warning).to_be_visible()
            _expect_members_controls(page)
            if with_jwt:
                stage = "Auth fields"
                page.get_by_role("tab", name="Auth", exact=True).click()
                tenant = page.get_by_label("Tenant id", exact=True)
                tenant.fill(settings["tenant_id"])
                with socket.expect_event("framereceived", predicate=_script_finished):
                    tenant.press("Enter")
                jwt = page.get_by_label("Access JWT", exact=True).and_(page.locator("input"))
                jwt.fill(token)
                with socket.expect_event("framereceived", predicate=_script_finished):
                    jwt.press("Enter")
                expect(jwt).to_have_value(token)
                stage = "warning and controls after access JWT"
                tab.click()
                expect(tab).to_have_attribute("aria-selected", "true")
                expect(warning).to_be_hidden()
                _expect_members_controls(page)
            stage = "sign-out"
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
    except (AssertionError, Error):
        raise pytest.fail.Exception(
            f"Members test failed at {stage}", pytrace=False
        ) from None


def _expect_members_controls(page: Page) -> None:
    panel = page.get_by_role("tabpanel")
    expect(panel.get_by_text(
        "Tenant admin only. This does not create a Cognito user. "
        "Type the existing subject and the exact issuer.", exact=True
    )).to_be_visible()
    for name in ("issuer", "subject"):
        field = panel.get_by_label(name, exact=True).and_(page.locator("input"))
        expect(field).to_be_visible()
        expect(field).to_be_enabled()
    expect(panel.get_by_text("role", exact=True)).to_be_visible()
    for name in ("editor", "viewer"):
        radio = panel.get_by_role("radio", name=name, exact=True)
        expect(radio).to_be_visible()
        expect(radio).to_be_enabled()
    expect(panel.get_by_role("radio", name="editor", exact=True)).to_be_checked()
    button = panel.get_by_role("button", name="Add member", exact=True)
    expect(button).to_be_visible()
    expect(button).to_be_enabled()


@pytest.mark.e2e
def test_auth_tab_accepts_tenant_and_generated_access_jwt(
    browser: Browser, request: pytest.FixtureRequest
):
    settings, token = request.getfixturevalue("cognito_auth")
    stage = "console sign-in"
    try:
        # Never record real credentials or issued tokens in plugin artifacts.
        with browser.new_context() as context:
            page = context.new_page()
            socket = _sign_in(page)
            stage = "Links warning before access JWT"
            page.get_by_role("tab", name="Links", exact=True).click()
            links_warning = page.get_by_text(
                "Paste the access JWT on the Auth tab before listing links.", exact=True
            )
            expect(links_warning).to_be_visible()
            page.get_by_role("tab", name="Auth", exact=True).click()
            stage = "initial missing-token warning"
            warning = page.get_by_text(
                "No access JWT yet. API calls stay blocked until you paste one.", exact=True
            )
            expect(warning).to_be_visible()
            stage = "tenant input"
            tenant = page.get_by_label("Tenant id", exact=True)
            tenant.fill(settings["tenant_id"])
            with socket.expect_event("framereceived", predicate=_script_finished):
                tenant.press("Enter")
            stage = "access JWT input"
            jwt = page.get_by_label("Access JWT", exact=True).and_(page.locator("input"))
            jwt.fill(token)
            with socket.expect_event("framereceived", predicate=_script_finished):
                jwt.press("Enter")
            stage = "filled field values"
            expect(tenant).to_have_value(settings["tenant_id"])
            expect(jwt).to_have_value(token)
            stage = "missing-token warning hidden"
            expect(warning).to_be_hidden()
            expect(page.get_by_text(
                "Access JWT is stored for this browser session.", exact=True
            )).to_be_visible()
            stage = "Links warning and List controls after access JWT"
            links = page.get_by_role("tab", name="Links", exact=True)
            links.click()
            expect(links).to_have_attribute("aria-selected", "true")
            panel = page.get_by_role("tabpanel")
            expect(links_warning).to_be_hidden()
            expect(panel.get_by_role("heading", name="List", exact=True)).to_be_visible()
            list_button = panel.get_by_role("button", name="List", exact=True)
            expect(list_button).to_be_visible()
            expect(list_button).to_be_enabled()
            stage = "List response"
            list_button.click()
            expect(panel.get_by_text("GET /api/v1/links", exact=True)).to_be_visible(timeout=30000)
            status = panel.get_by_text(re.compile(
                r"^(HTTP \d{3}|Transport failure \((?:connection|timeout|header)\)\.)$"
            ))
            expect(status).to_be_visible(timeout=30000)
            if status.inner_text() != "HTTP 200":
                raise pytest.fail.Exception(
                    f"List failed: {status.inner_text()}", pytrace=False
                ) from None
            expect(panel.get_by_text("HTTP 200", exact=True)).to_be_visible()
            stage = "sign-out"
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_label("Password", exact=True)).to_be_visible()
    except (AssertionError, Error) as exc:
        raise pytest.fail.Exception(
            f"Local Auth tab JWT check failed at {stage} ({type(exc).__name__})", pytrace=False
        ) from None
