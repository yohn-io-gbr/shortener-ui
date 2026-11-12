from unittest.mock import patch

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
import pytest

from cognito_auth import CognitoAuthError, MFAChallenge, complete_mfa, start_login

REGION = "us-east-1"
CLIENT_ID = "syntheticclient"
USERNAME = "operator@example.test"
TOKEN = "synthetic.access.token"
SESSION = "synthetic-session-" * 3
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
            "USERNAME": "canonical-user", "PASSWORD_CLAIM_SIGNATURE": "synthetic-signature",
        }
        yield factory


def add_srp_challenge(provider, srp, result):
    provider.add_response("initiate_auth", {
        "ChallengeName": "PASSWORD_VERIFIER", "Session": SESSION,
        "ChallengeParameters": {"USER_ID_FOR_SRP": "canonical-user"},
    }, {
        "AuthFlow": "USER_SRP_AUTH", "ClientId": CLIENT_ID,
        "AuthParameters": srp.return_value.get_auth_params.return_value,
    })
    provider.add_response("respond_to_auth_challenge", result, {
        "ClientId": CLIENT_ID, "ChallengeName": "PASSWORD_VERIFIER", "Session": SESSION,
        "ChallengeResponses": srp.return_value.process_challenge.return_value,
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
