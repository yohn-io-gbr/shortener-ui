from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.proto.TextInput_pb2 import TextInput
from streamlit.testing.v1 import AppTest

from cognito_auth import CognitoAuthError, LoginResult, MFAChallenge

APP = Path(__file__).resolve().parents[1] / "app.py"
TOKEN = "synthetic.access.token"


@pytest.fixture
def backend():
    with patch("cognito_auth.start_login") as start, patch("cognito_auth.complete_mfa") as complete:
        yield start, complete


def app_instance():
    app = AppTest.from_file(str(APP), default_timeout=15)
    app.secrets["AUTH_USERNAME"] = "synthetic-console-user"
    app.secrets["AUTH_PASSWORD"] = "synthetic-console-password"
    app.secrets["SHORTENER_BASE_URL"] = "https://shortener.example"
    app.session_state["basic_auth_ok"] = True
    app.run()
    assert not app.exception
    return app


def button(app, label="generate JWT"):
    return next(item for item in app.button if item.label == label)


def fill(app):
    for key, value in {
        "cognito_region": "us-east-1", "cognito_user_pool_id": "us-east-1_Synthetic",
        "cognito_client_id": "syntheticclient", "cognito_username": "operator@example.test",
        "cognito_password": "synthetic-password", "cognito_client_secret": "synthetic-secret",
    }.items():
        app.text_input(key=key).set_value(value)
    app.run()
    assert not app.exception


def click(app, label="generate JWT"):
    button(app, label).click().run()
    assert not app.exception


def test_required_fields_gate_generation_and_typing_never_authenticates(backend):
    start, complete = backend
    app = app_instance()
    assert button(app).disabled
    fill(app)
    assert not button(app).disabled
    assert app.text_input(key="cognito_password").proto.type == TextInput.PASSWORD
    assert app.text_input(key="cognito_client_secret").proto.type == TextInput.PASSWORD
    app.text_input(key="cognito_client_secret").set_value("").run()
    assert not button(app).disabled
    for key in (
        "cognito_region", "cognito_user_pool_id", "cognito_client_id",
        "cognito_username", "cognito_password",
    ):
        value = app.text_input(key=key).value
        app.text_input(key=key).set_value("").run()
        assert button(app).disabled
        app.text_input(key=key).set_value(value).run()
    start.assert_not_called()
    complete.assert_not_called()


def test_success_fills_existing_jwt_field_and_clears_old_identity_data(backend):
    start, _ = backend
    start.return_value = LoginResult(access_token=TOKEN)
    app = app_instance()
    fill(app)
    app.text_input(key="tenant_id").set_value("synthetic-tenant")
    app.session_state["idempotency"] = {"old": {"key": "synthetic-old-key"}}
    app.session_state["results"] = {"old": "synthetic-old-response"}
    click(app)
    assert start.call_count == 1
    assert app.text_input(key="access_token").value == TOKEN
    assert app.text_input(key="tenant_id").value == "synthetic-tenant"
    assert app.text_input(key="cognito_username").value == "operator@example.test"
    assert app.text_input(key="cognito_password").value == ""
    assert app.text_input(key="cognito_client_secret").value == ""
    assert app.session_state["idempotency"] == {}
    assert app.session_state["results"] == {}
    assert not any("No access JWT yet" in warning.value for warning in app.warning)
    assert not any("before listing links" in warning.value for warning in app.warning)
    assert any("generated and filled" in item.value for item in app.info)


def test_failed_login_displays_safe_error_and_preserves_existing_token(backend):
    start, _ = backend
    start.side_effect = CognitoAuthError("Cognito rejected the sign-in.")
    app = app_instance()
    app.text_input(key="access_token").set_value(TOKEN)
    fill(app)
    click(app)
    assert any(error.value == "Cognito rejected the sign-in." for error in app.error)
    assert app.text_input(key="access_token").value == TOKEN
    assert app.text_input(key="cognito_password").value == ""
    assert app.text_input(key="cognito_client_secret").value == ""


def test_mfa_prompts_for_code_and_completes_without_starting_another_login(backend):
    start, complete = backend
    pending = MFAChallenge("us-east-1", "syntheticclient", "canonical-user", "SOFTWARE_TOKEN_MFA", "synthetic-session")
    start.return_value = LoginResult(challenge=pending)
    complete.return_value = LoginResult(access_token=TOKEN)
    app = app_instance()
    fill(app)
    click(app)
    assert button(app).disabled
    assert app.text_input(key="cognito_username").disabled
    assert app.text_input(key="cognito_password").value == ""
    assert app.text_input(key="cognito_mfa_code").proto.type == TextInput.PASSWORD
    app.text_input(key="cognito_mfa_code").set_value("123456").run()
    click(app)
    assert start.call_count == 1
    complete.assert_called_once_with(pending, "123456")
    assert app.text_input(key="access_token").value == TOKEN
    assert "cognito_challenge" not in app.session_state
    assert "cognito_mfa_code" not in app.session_state


def test_wrong_mfa_code_keeps_challenge_available_for_retry(backend):
    start, complete = backend
    pending = MFAChallenge("us-east-1", "syntheticclient", "canonical-user", "SMS_MFA", "synthetic-session")
    start.return_value = LoginResult(challenge=pending)
    complete.side_effect = CognitoAuthError("The MFA code is incorrect.")
    app = app_instance()
    fill(app)
    click(app)
    app.text_input(key="cognito_mfa_code").set_value("123456").run()
    click(app)
    assert app.session_state["cognito_challenge"] is pending
    assert app.text_input(key="cognito_mfa_code").value == ""
    assert any("MFA code is incorrect" in item.value for item in app.error)
    assert start.call_count == 1


@pytest.mark.parametrize("control", ["Cancel Cognito sign-in", "Clear session", "Sign out"])
def test_cancel_clear_and_sign_out_remove_pending_mfa(backend, control):
    start, _ = backend
    start.return_value = LoginResult(challenge=MFAChallenge(
        "us-east-1", "syntheticclient", "canonical-user", "SMS_MFA", "synthetic-session", "synthetic-secret",
    ))
    app = app_instance()
    fill(app)
    click(app)
    click(app, control)
    assert "cognito_challenge" not in app.session_state
    if control != "Sign out":
        assert app.text_input(key="cognito_password").value == ""
        assert app.text_input(key="cognito_client_secret").value == ""
        assert button(app).disabled
