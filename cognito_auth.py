"""Cognito SRP sign-in without persisting credentials or authentication results."""

from contextlib import closing
from dataclasses import dataclass
import re
from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pycognito.aws_srp import AWSSRP


class CognitoAuthError(ValueError):
    """A safe, user-facing error that never includes raw provider diagnostics."""


@dataclass(frozen=True, repr=False)
class MFAChallenge:
    region: str
    client_id: str
    username: str
    name: str
    session: str
    client_secret: str = ""


@dataclass(frozen=True, repr=False)
class LoginResult:
    access_token: str = ""
    challenge: MFAChallenge | None = None


MFA_CODES = {
    "SMS_MFA": "SMS_MFA_CODE",
    "SOFTWARE_TOKEN_MFA": "SOFTWARE_TOKEN_MFA_CODE",
    "EMAIL_OTP": "EMAIL_OTP_CODE",
}


def _client(region: str):
    return boto3.client(
        "cognito-idp", region_name=region,
        config=Config(
            signature_version=UNSIGNED, connect_timeout=10, read_timeout=30,
            retries={"max_attempts": 0},
        ),
    )


def _provider_error(error: ClientError) -> CognitoAuthError:
    code = error.response.get("Error", {}).get("Code")
    messages = {
        "NotAuthorizedException": "Cognito rejected the sign-in. Check your credentials and app client settings.",
        "UserNotFoundException": "Cognito rejected the sign-in. Check your credentials and app client settings.",
        "UserNotConfirmedException": "Confirm this Cognito account before signing in.",
        "PasswordResetRequiredException": "Reset this account's password before signing in.",
        "CodeMismatchException": "The MFA code is incorrect. Enter the current code and try again.",
        "ExpiredCodeException": "The MFA code has expired. Cancel sign-in and start again.",
        "TooManyRequestsException": "Too many sign-in attempts. Wait before trying again.",
        "InvalidParameterException": "Check the Cognito settings and that the app client permits SRP sign-in.",
        "ResourceNotFoundException": "Check the Cognito region, user pool ID, and app client ID.",
    }
    return CognitoAuthError(messages.get(code, "Cognito sign-in failed. Check the account and app client settings."))


def _result(body: dict[str, Any], region: str, client_id: str, username: str, secret: str) -> LoginResult:
    challenge = body.get("ChallengeName")
    if challenge in MFA_CODES:
        session = body.get("Session")
        if not isinstance(session, str) or not session:
            raise CognitoAuthError("Cognito did not return an MFA session. Start sign-in again.")
        canonical = body.get("ChallengeParameters", {}).get("USERNAME")
        if isinstance(canonical, str) and canonical:
            username = canonical
        return LoginResult(challenge=MFAChallenge(region, client_id, username, challenge, session, secret))
    if challenge:
        raise CognitoAuthError("Complete the account's password change or additional setup before signing in here.")
    authentication = body.get("AuthenticationResult")
    token = authentication.get("AccessToken") if isinstance(authentication, dict) else None
    if not isinstance(token, str) or token.count(".") != 2:
        raise CognitoAuthError("Cognito did not return an access JWT. Start sign-in again.")
    # ID and refresh tokens are deliberately not retained.
    return LoginResult(access_token=token)


def start_login(
    *, region: str, pool_id: str, client_id: str, username: str, password: str,
    client_secret: str = "",
) -> LoginResult:
    region, pool_id, client_id, username = (value.strip() for value in (region, pool_id, client_id, username))
    if not all((region, pool_id, client_id, username, password)):
        raise CognitoAuthError("Fill the region, user pool ID, app client ID, username, and password.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
        raise CognitoAuthError("Enter a valid AWS region.")
    if not re.fullmatch(re.escape(region) + r"_[A-Za-z0-9]+", pool_id):
        raise CognitoAuthError("The user pool ID must belong to the selected AWS region.")
    if not re.fullmatch(r"[A-Za-z0-9_+]{1,128}", client_id):
        raise CognitoAuthError("Enter a valid Cognito app client ID.")
    try:
        with closing(_client(region)) as client:
            srp = AWSSRP(
                username=username, password=password, pool_id=pool_id,
                client_id=client_id, client=client, client_secret=client_secret or None,
            )
            parameters = srp.get_auth_params()
            result = client.initiate_auth(
                AuthFlow="USER_SRP_AUTH", ClientId=client_id, AuthParameters=parameters,
            )
            if result.get("ChallengeName") == "PASSWORD_VERIFIER":
                answers = srp.process_challenge(result["ChallengeParameters"], parameters)
                username = answers["USERNAME"]
                result = client.respond_to_auth_challenge(
                    ClientId=client_id, ChallengeName="PASSWORD_VERIFIER", ChallengeResponses=answers,
                    **({"Session": result["Session"]} if result.get("Session") else {}),
                )
            return _result(result, region, client_id, username, client_secret)
    except ClientError as error:
        raise _provider_error(error) from None
    except BotoCoreError:
        raise CognitoAuthError("Cannot reach Cognito. Check your network and AWS region.") from None
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, CognitoAuthError):
            raise
        raise CognitoAuthError("Cognito returned an unexpected sign-in response. Start again.") from None


def complete_mfa(challenge: MFAChallenge, code: str) -> LoginResult:
    code = code.strip()
    if not re.fullmatch(r"\d{6}", code):
        raise CognitoAuthError("Enter the six-digit MFA code.")
    try:
        answers = {"USERNAME": challenge.username, MFA_CODES[challenge.name]: code}
        if challenge.client_secret:
            answers["SECRET_HASH"] = AWSSRP.get_secret_hash(
                challenge.username, challenge.client_id, challenge.client_secret,
            )
        with closing(_client(challenge.region)) as client:
            result = client.respond_to_auth_challenge(
                ClientId=challenge.client_id, ChallengeName=challenge.name,
                ChallengeResponses=answers, Session=challenge.session,
            )
        return _result(result, challenge.region, challenge.client_id, challenge.username, challenge.client_secret)
    except ClientError as error:
        raise _provider_error(error) from None
    except BotoCoreError:
        raise CognitoAuthError("Cannot reach Cognito. Check your network and AWS region.") from None
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, CognitoAuthError):
            raise
        raise CognitoAuthError("Cognito returned an unexpected MFA response. Start again.") from None
