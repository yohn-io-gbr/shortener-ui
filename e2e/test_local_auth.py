from pathlib import Path
from contextlib import closing
import re
import tomllib

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pycognito.aws_srp import AWSSRP
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
        return settings
    except (OSError, ValueError, KeyError, TypeError):
        raise pytest.fail.Exception(
            "Set tenant_id, user, pass, cognito_region, cognito_user_pool_id, and cognito_client_id under "
            "[local_tests] in .streamlit/secrets.toml", pytrace=False
        ) from None


def _generate_access_jwt(settings: dict[str, str]) -> str:
    try:
        # Cognito user login needs no AWS IAM credentials or metadata lookup.
        with closing(boto3.client(
            "cognito-idp",
            region_name=settings["cognito_region"],
            config=Config(
                signature_version=UNSIGNED, connect_timeout=10, read_timeout=30,
                retries={"max_attempts": 0},
            ),
        )) as client:
            srp = AWSSRP(
                username=settings["user"], password=settings["pass"],
                pool_id=settings["cognito_user_pool_id"],
                client_id=settings["cognito_client_id"], client=client,
                client_secret=settings.get("cognito_client_secret") or None,
            )
            parameters = srp.get_auth_params()
            result = client.initiate_auth(
                AuthFlow="USER_SRP_AUTH", ClientId=settings["cognito_client_id"],
                AuthParameters=parameters,
            )
            username = settings["user"]
            if result.get("ChallengeName") == "PASSWORD_VERIFIER":
                answers = srp.process_challenge(result["ChallengeParameters"], parameters)
                username = answers["USERNAME"]
                result = client.respond_to_auth_challenge(
                    ClientId=settings["cognito_client_id"],
                    ChallengeName="PASSWORD_VERIFIER", ChallengeResponses=answers,
                    **({"Session": result["Session"]} if result.get("Session") else {}),
                )
            challenge = result.get("ChallengeName")
            mfa_keys = {
                "SMS_MFA": "SMS_MFA_CODE",
                "SOFTWARE_TOKEN_MFA": "SOFTWARE_TOKEN_MFA_CODE",
                "EMAIL_OTP": "EMAIL_OTP_CODE",
            }
            if challenge in mfa_keys:
                code = settings.get("mfa_code")
                if not code:
                    raise pytest.fail.Exception(
                        "Cognito requires MFA. Add the current mfa_code under [local_tests] "
                        "in the ignored secrets file, then rerun immediately.", pytrace=False
                    ) from None
                answers = {"USERNAME": username, mfa_keys[challenge]: str(code)}
                if settings.get("cognito_client_secret"):
                    answers["SECRET_HASH"] = srp.get_secret_hash(
                        username, settings["cognito_client_id"], settings["cognito_client_secret"]
                    )
                result = client.respond_to_auth_challenge(
                    ClientId=settings["cognito_client_id"], ChallengeName=challenge,
                    ChallengeResponses=answers, Session=result["Session"],
                )
            if result.get("ChallengeName"):
                raise pytest.fail.Exception(
                    "Cognito requires an additional account challenge that this test "
                    "does not handle. Complete account setup before retrying.", pytrace=False
                ) from None
            token = result["AuthenticationResult"]["AccessToken"]
            if not isinstance(token, str) or token.count(".") != 2:
                raise ValueError("No access JWT returned")
            return token
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        allowed = {
            "NotAuthorizedException", "InvalidParameterException", "UserNotFoundException",
            "ResourceNotFoundException", "PasswordResetRequiredException",
            "UserNotConfirmedException", "TooManyRequestsException", "CodeMismatchException",
            "ExpiredCodeException",
        }
        safe_code = code if code in allowed else "AuthenticationError"
        raise pytest.fail.Exception(
            f"Cognito {safe_code}. Check the local user/password, pool, client, and MFA settings.",
            pytrace=False,
        ) from None
    except (BotoCoreError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise pytest.fail.Exception(
            f"Could not obtain a Cognito access JWT using SRP ({type(exc).__name__}). "
            "Check local configuration and network access.", pytrace=False
        ) from None


def _expect_recorded_stats_controls(page: Page) -> None:
    panel = page.get_by_role("tabpanel")
    dates = panel.locator('input[type="date"]')
    expect(dates).to_have_count(2)
    for index, name in enumerate(("start", "end")):
        expect(panel.get_by_text(name, exact=True)).to_be_visible()
        field = dates.nth(index)
        expect(field).to_be_visible()
        expect(field).to_be_enabled()
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
def test_recorded_stats_controls_and_warning_with_generated_access_jwt(browser: Browser):
    settings = _local_access_credentials()
    token = _generate_access_jwt(settings)
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
    dates = panel.locator('input[type="date"]')
    expect(dates).to_have_count(2)
    for index, name in enumerate(("analytics start", "analytics end")):
        expect(panel.get_by_text(name, exact=True)).to_be_visible()
        expect(dates.nth(index)).to_be_visible()
        expect(dates.nth(index)).to_be_enabled()
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
def test_sampled_analytics_controls_and_warning(browser: Browser, with_jwt: bool):
    settings = _local_access_credentials() if with_jwt else None
    token = _generate_access_jwt(settings) if with_jwt else None
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
def test_members_controls_and_warning(browser: Browser, with_jwt: bool):
    settings = _local_access_credentials() if with_jwt else None
    token = _generate_access_jwt(settings) if with_jwt else None
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
def test_auth_tab_accepts_tenant_and_generated_access_jwt(browser: Browser):
    settings = _local_access_credentials()
    token = _generate_access_jwt(settings)
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
