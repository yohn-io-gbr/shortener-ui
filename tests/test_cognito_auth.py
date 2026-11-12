from unittest.mock import patch

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from botocore.stub import ANY, Stubber
from pycognito.aws_srp import AWSSRP
import pytest

from cognito_auth import CognitoAuthError, MFAChallenge, complete_mfa, start_login

REGION = "us-east-1"
CLIENT_ID = "syntheticclient"
USERNAME = "operator@example.test"
TOKEN = "synthetic.access.token"
SESSION = "synthetic-session-" * 3
CANONICAL = "canonical-user"
CHALLENGE = {
    "USER_ID_FOR_SRP": CANONICAL, "SALT": "123abc", "SRP_B": "2",
    "SECRET_BLOCK": "c3ludGhldGljLWJsb2Nr",
}
FIELDS = dict(
    region=REGION, pool_id="us-east-1_Synthetic", client_id=CLIENT_ID,
    username=USERNAME, password="synthetic-password",
)


@pytest.fixture
def provider():
    client = boto3.client("cognito-idp", region_name=REGION, config=Config(signature_version=UNSIGNED))
    with Stubber(client) as stub, patch("cognito_auth._client", return_value=client):
        yield stub
        stub.assert_no_pending_responses()
    client.close()


@pytest.fixture
def srp():
    with patch("cognito_auth.AWSSRP") as factory:
        factory.return_value.get_auth_params.return_value = {"USERNAME": USERNAME, "SRP_A": "synthetic-A"}
        factory.return_value.process_challenge.return_value = {
            "USERNAME": USERNAME, "PASSWORD_CLAIM_SIGNATURE": "synthetic-signature",
        }
        factory.get_secret_hash.side_effect = AWSSRP.get_secret_hash
        yield factory


def add_srp_challenge(provider, srp, result):
    provider.add_response("initiate_auth", {
        "ChallengeName": "PASSWORD_VERIFIER", "Session": SESSION,
        "ChallengeParameters": CHALLENGE,
    }, {
        "AuthFlow": "USER_SRP_AUTH", "ClientId": CLIENT_ID,
        "AuthParameters": srp.return_value.get_auth_params.return_value,
    })
    provider.add_response("respond_to_auth_challenge", result, {
        "ClientId": CLIENT_ID, "ChallengeName": "PASSWORD_VERIFIER", "Session": SESSION,
        "ChallengeResponses": {**srp.return_value.process_challenge.return_value, "USERNAME": CANONICAL},
    })


def test_srp_retains_only_access_token_and_preserves_challenge_session(provider, srp):
    add_srp_challenge(provider, srp, {"AuthenticationResult": {
        "AccessToken": TOKEN, "IdToken": "synthetic.id.token", "RefreshToken": "synthetic-refresh",
    }})
    result = start_login(**FIELDS)
    assert result.access_token == TOKEN
    assert result.challenge is None
    assert not hasattr(result, "id_token")
    assert not hasattr(result, "refresh_token")
    assert TOKEN not in repr(result)
    assert srp.call_args.kwargs["password"] == FIELDS["password"]
    srp.return_value.process_challenge.assert_called_once_with(
        {**CHALLENGE, "USERNAME": CANONICAL}, srp.return_value.get_auth_params.return_value,
    )


@pytest.mark.parametrize("field,value", [
    ("username", ""), ("password", ""), ("region", "https://invalid.example"),
    ("pool_id", "eu-west-1_WrongRegion"), ("client_id", "invalid/id"),
])
def test_invalid_configuration_never_creates_network_client(field, value):
    with patch("cognito_auth._client") as client, pytest.raises(CognitoAuthError):
        start_login(**{**FIELDS, field: value})
    client.assert_not_called()


@pytest.mark.parametrize("challenge,code_key", [
    ("SMS_MFA", "SMS_MFA_CODE"),
    ("SOFTWARE_TOKEN_MFA", "SOFTWARE_TOKEN_MFA_CODE"),
    ("EMAIL_MFA", "EMAIL_MFA_CODE"),
    ("EMAIL_OTP", "EMAIL_OTP_CODE"),
])
def test_mfa_continues_original_session_without_another_password_login(provider, srp, challenge, code_key):
    add_srp_challenge(provider, srp, {"ChallengeName": challenge, "Session": SESSION})
    pending = start_login(**FIELDS).challenge
    assert pending.name == challenge
    assert pending.username == "canonical-user"
    assert FIELDS["password"] not in repr(pending)
    provider.add_response("respond_to_auth_challenge", {"AuthenticationResult": {"AccessToken": TOKEN}}, {
        "ClientId": CLIENT_ID, "ChallengeName": challenge, "Session": SESSION,
        "ChallengeResponses": {"USERNAME": "canonical-user", code_key: "123456"},
    })
    assert complete_mfa(pending, "123456").access_token == TOKEN


def test_secret_hash_is_added_to_mfa_response(provider):
    from pycognito.aws_srp import AWSSRP
    pending = MFAChallenge(REGION, CLIENT_ID, USERNAME, "SMS_MFA", SESSION, "synthetic-secret")
    provider.add_response("respond_to_auth_challenge", {"AuthenticationResult": {"AccessToken": TOKEN}}, {
        "ClientId": CLIENT_ID, "ChallengeName": "SMS_MFA", "Session": SESSION,
        "ChallengeResponses": {
            "USERNAME": USERNAME, "SMS_MFA_CODE": "123456",
            "SECRET_HASH": AWSSRP.get_secret_hash(USERNAME, CLIENT_ID, "synthetic-secret"),
        },
    })
    assert complete_mfa(pending, "123456").access_token == TOKEN
    assert "synthetic-secret" not in repr(pending)


def test_invalid_mfa_code_is_rejected_before_network_access():
    pending = MFAChallenge(REGION, CLIENT_ID, USERNAME, "SMS_MFA", SESSION)
    with patch("cognito_auth._client") as client, pytest.raises(CognitoAuthError, match="six-digit"):
        complete_mfa(pending, "wrong")
    client.assert_not_called()


def test_provider_errors_do_not_expose_raw_diagnostics(provider, srp):
    provider.add_client_error("initiate_auth", "NotAuthorizedException", "private-password-and-provider-details")
    with pytest.raises(CognitoAuthError) as error:
        start_login(**FIELDS)
    assert "private-password" not in str(error.value)
    assert "rejected" in str(error.value)


def test_network_errors_are_safe():
    with patch("cognito_auth._client", side_effect=EndpointConnectionError(endpoint_url="https://private.example")):
        with pytest.raises(CognitoAuthError, match="Cannot reach Cognito") as error:
            start_login(**FIELDS)
    assert "private.example" not in str(error.value)


@pytest.mark.parametrize("result", [
    {"AuthenticationResult": {"IdToken": "synthetic.id.token"}},
    {"AuthenticationResult": {"AccessToken": "not-a-jwt"}},
    {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": SESSION},
])
def test_incomplete_or_unsupported_login_never_returns_token(provider, srp, result):
    add_srp_challenge(provider, srp, result)
    with pytest.raises(CognitoAuthError):
        start_login(**FIELDS)


@pytest.mark.parametrize("secret", ["", "synthetic-client-secret"])
@pytest.mark.parametrize("verifier_username", [None, USERNAME])
def test_real_srp_uses_canonical_username_and_hash_through_mfa(provider, secret, verifier_username):
    """Exercise pycognito itself: its alias fallback must not reach Cognito."""
    parameters = dict(CHALLENGE)
    if verifier_username is not None:
        parameters["USERNAME"] = verifier_username
    initial = {"USERNAME": USERNAME, "SRP_A": ANY}
    verifier = {
        "USERNAME": CANONICAL, "TIMESTAMP": ANY,
        "PASSWORD_CLAIM_SECRET_BLOCK": CHALLENGE["SECRET_BLOCK"],
        "PASSWORD_CLAIM_SIGNATURE": ANY,
    }
    mfa = {"USERNAME": CANONICAL, "EMAIL_MFA_CODE": "123456"}
    if secret:
        initial["SECRET_HASH"] = AWSSRP.get_secret_hash(USERNAME, CLIENT_ID, secret)
        canonical_hash = AWSSRP.get_secret_hash(CANONICAL, CLIENT_ID, secret)
        assert canonical_hash != initial["SECRET_HASH"]
        verifier["SECRET_HASH"] = canonical_hash
        mfa["SECRET_HASH"] = canonical_hash
    provider.add_response("initiate_auth", {
        "ChallengeName": "PASSWORD_VERIFIER", "Session": SESSION,
        "ChallengeParameters": parameters,
    }, {"AuthFlow": "USER_SRP_AUTH", "ClientId": CLIENT_ID, "AuthParameters": initial})
    provider.add_response("respond_to_auth_challenge", {
        "ChallengeName": "EMAIL_MFA", "Session": SESSION,
    }, {
        "ClientId": CLIENT_ID, "ChallengeName": "PASSWORD_VERIFIER",
        "Session": SESSION, "ChallengeResponses": verifier,
    })
    pending = start_login(**FIELDS, client_secret=secret).challenge
    assert pending.username == CANONICAL
    provider.add_response("respond_to_auth_challenge", {
        "AuthenticationResult": {"AccessToken": TOKEN},
    }, {
        "ClientId": CLIENT_ID, "ChallengeName": "EMAIL_MFA",
        "Session": SESSION, "ChallengeResponses": mfa,
    })
    assert complete_mfa(pending, "123456").access_token == TOKEN


@pytest.mark.parametrize("parameters", [
    {}, {**CHALLENGE, "USER_ID_FOR_SRP": ""},
    {**CHALLENGE, "USER_ID_FOR_SRP": "   "},
    {key: value for key, value in CHALLENGE.items() if key != "SALT"},
    {**CHALLENGE, "SALT": "invalid-hex"},
    {**CHALLENGE, "SRP_B": "0"},
    {**CHALLENGE, "SECRET_BLOCK": "x"},
])
def test_malformed_password_challenge_is_safe(provider, parameters):
    provider.add_response("initiate_auth", {
        "ChallengeName": "PASSWORD_VERIFIER", "ChallengeParameters": parameters,
    })
    with pytest.raises(CognitoAuthError) as error:
        start_login(**FIELDS, client_secret="synthetic-client-secret")
    assert FIELDS["password"] not in str(error.value)
    assert "synthetic-client-secret" not in str(error.value)
    assert SESSION not in str(error.value)


@pytest.mark.parametrize("body", [
    None, [], {"ChallengeName": "PASSWORD_VERIFIER", "ChallengeParameters": None},
    {"ChallengeName": "PASSWORD_VERIFIER", "ChallengeParameters": []},
    {"ChallengeName": "PASSWORD_VERIFIER", "ChallengeParameters": {**CHALLENGE, "USER_ID_FOR_SRP": 42}},
])
def test_unexpected_sdk_response_types_are_safe(body, srp):
    with patch("cognito_auth._client") as client:
        client.return_value.initiate_auth.return_value = body
        with pytest.raises(CognitoAuthError):
            start_login(**FIELDS)
        client.return_value.respond_to_auth_challenge.assert_not_called()


@pytest.mark.parametrize("body", [
    None, {"ChallengeName": "SMS_MFA"},
    {"ChallengeName": "EMAIL_MFA", "Session": SESSION, "ChallengeParameters": None},
    {"ChallengeName": "EMAIL_MFA", "Session": SESSION, "ChallengeParameters": {"USERNAME": 42}},
    {"ChallengeName": "EMAIL_MFA", "Session": SESSION, "ChallengeParameters": {"USERNAME": ""}},
    {"AuthenticationResult": {"AccessToken": "private-token"}},
])
def test_malformed_mfa_response_is_safe(body):
    pending = MFAChallenge(REGION, CLIENT_ID, CANONICAL, "EMAIL_MFA", SESSION, "synthetic-client-secret")
    with patch("cognito_auth._client") as client:
        client.return_value.respond_to_auth_challenge.return_value = body
        with pytest.raises(CognitoAuthError) as error:
            complete_mfa(pending, "123456")
    assert all(value not in str(error.value) for value in (
        "private-token", "synthetic-client-secret", SESSION, "123456",
    ))


@pytest.mark.parametrize("provider_code,message", [
    ("CodeMismatchException", "incorrect"), ("ExpiredCodeException", "expired"),
])
def test_mfa_provider_errors_hide_secrets_and_allow_session_retry(provider, provider_code, message):
    pending = MFAChallenge(REGION, CLIENT_ID, CANONICAL, "EMAIL_MFA", SESSION, "synthetic-client-secret")
    private = " ".join((FIELDS["password"], pending.client_secret, SESSION, TOKEN, "123456"))
    provider.add_client_error("respond_to_auth_challenge", provider_code, private)
    with pytest.raises(CognitoAuthError, match=message) as error:
        complete_mfa(pending, "123456")
    assert all(value not in str(error.value) for value in private.split())
    # A retry uses the same immutable challenge and provider session.
    provider.add_response("respond_to_auth_challenge", {"AuthenticationResult": {"AccessToken": TOKEN}}, {
        "ClientId": CLIENT_ID, "ChallengeName": "EMAIL_MFA", "Session": SESSION,
        "ChallengeResponses": {
            "USERNAME": CANONICAL, "EMAIL_MFA_CODE": "654321",
            "SECRET_HASH": AWSSRP.get_secret_hash(CANONICAL, CLIENT_ID, pending.client_secret),
        },
    })
    assert complete_mfa(pending, "654321").access_token == TOKEN
