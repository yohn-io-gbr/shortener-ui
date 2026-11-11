# Staging shortener console

Streamlit console for the staging shortener. The app calls the Worker from Python. The browser does not call the Worker. The Worker origin is `SHORTENER_BASE_URL` in Streamlit secrets and is not stored in this repository.

This directory is a client. It does not belong in `yohn-aws`, and it must not be pushed to that repository. The shortener CI fingerprints its own workflow and source files.

Python 3.12.

The access JWT stays in the browser session. Never commit JWTs, Cognito passwords,
Analytics Engine tokens, or smoke tokens, or add them to `secrets.toml.example`.
The console password belongs in Streamlit secrets as `AUTH_PASSWORD`. Credentials
for the local Cognito browser test belong only in the ignored `[local_tests]`
section of `.streamlit/secrets.toml`; do not upload that section to Streamlit Cloud.
Generated JWTs stay in memory and must not be saved in the secrets file.

## Local setup

From this directory:

```sh
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt -r requirements-dev.txt
```

Optional base URL, plus the console username and password:

```sh
mkdir -p .streamlit
cp secrets.toml.example .streamlit/secrets.toml
```

## Manual checklist

1. `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt -r requirements-dev.txt`
2. `pytest`
3. `streamlit run app.py`
4. Open the **Auth** tab. Paste a staging Cognito **access** JWT for a member of `stage-test-simple`. An ID token is the wrong token. The token stays in the browser session and is not saved.
5. Health and ready both show 200.
6. List links. Create one link on a domain the tenant already owns, using a disposable slug. Confirm 201, `Location`, and `ETag`.
7. Change the destination with `If-Match`. Confirm the revision increases.
8. Load recorded stats for that link for a range that includes today.
9. Press Country once. An empty 404 is a valid "not configured" result. Do not press it again in that case.
10. Delete the disposable link and confirm it disappears from the list.
11. The published app is [shortener-ui-nmaetmpwczhcpzxghdo2sa.streamlit.app](https://shortener-ui-nmaetmpwczhcpzxghdo2sa.streamlit.app/). Opening it redirects to Streamlit sign-in. Confirm health 200 after that, and confirm the repository contains no JWT.

`pytest` does not open a socket and does not call the live Worker. The checklist above is the live check. Do not call `POST /api/v1/analytics/disable` except from the Emergency stop control, after confirming it.

## Browser tests (Playwright)

Install the development dependencies and Chromium in your active virtual environment:

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium
```

To check that your running app at `http://localhost:8501/` is available, start it
with `streamlit run app.py`, then run this in a second terminal with the virtual
environment activated:

```sh
pytest e2e/test_availability.py --browser chromium
```

The availability test checks HTTP 200 and the visible console heading. It does not
start or stop your app, sign in, or make Worker calls.

To verify sign-in using your local `AUTH_USERNAME` and `AUTH_PASSWORD`:

```sh
pytest e2e/test_local_auth.py --browser chromium
```

This test reads `.streamlit/secrets.toml` at runtime, signs in at
`http://localhost:8501/`, checks that the Auth tab opens, and signs out. Keep that
secrets file local; Git ignores it. The test uses a separate browser context without
trace, video, or screenshot recording so credentials are not saved in test artifacts.
The console login and tab navigation tests do not call the Worker.
The second test signs in and clicks Auth, Session, Links, Recorded stats, Sampled
analytics, and Members, checking that each tab is selected and its panel is visible.

To verify the Session tab's Check health and Check ready buttons:

```sh
pytest e2e/test_local_auth.py -k check_health
```

This test signs in to the local console, opens Session, clicks Check health, then
Check ready. It checks HTTP 200 and `{"status":"ok"}` for health, followed by
HTTP 200 and `{"status":"ready"}` for readiness. Both checks run even if health
returns an error. They call the Worker's read-only endpoints using the locally
configured `SHORTENER_BASE_URL`; neither requires an access JWT.

The Auth tab JWT test needs these additional values under `[local_tests]` in your
ignored `.streamlit/secrets.toml`:

```toml
[local_tests]
tenant_id = "your-test-tenant"
user = "your-test-user"
pass = "your-test-password"
cognito_region = "your-aws-region"
cognito_user_pool_id = "your-cognito-user-pool-id"
cognito_client_id = "your-cognito-app-client-id"
# cognito_client_secret = "only-if-your-app-client-has-a-secret"
# mfa_code = "current-code-only-if-Cognito-requests-MFA"
```

```sh
pytest e2e/test_local_auth.py -k generated_access_jwt
```

This test calls Cognito `InitiateAuth` with `USER_SRP_AUTH`, keeps only the
returned access token in memory, fills Tenant id and Access JWT on the local Auth
tab, and checks that the missing-token warning disappears. The Cognito app client
must allow SRP login. SMS, authenticator, and email MFA challenges use the optional
current `mfa_code` from local secrets. Remove the code after the test; it expires.
It makes an external Cognito login request; it does not call the shortener Worker
or verify tenant membership. Real credentials and tokens are never written to
source files or test artifacts. To run only the tests that use no external login:

```sh
pytest e2e/test_local_auth.py -k 'not generated_access_jwt'
```

Run the isolated authentication test separately:

```sh
pytest e2e/test_browser_auth.py --browser chromium
```

The authentication test starts and stops its own local Streamlit server on an available port,
with temporary test credentials and no Worker origin. They do not use your local
secrets or call the live Worker. The authentication test covers a rejected password,
successful sign-in, and sign-out. Plain `pytest` continues to run only the unit tests.

To see the browser or collect failure artifacts:

```sh
pytest e2e --headed
pytest e2e --tracing retain-on-failure --screenshot only-on-failure
```

Artifacts are written to the ignored `test-results/` directory. On Linux CI,
install browser system dependencies with `python -m playwright install --with-deps chromium`.

The console never requests `GET` or `HEAD /{slug}`. Open `shortUrl` in the browser when a real redirect test is needed.

## Repository

The source is [yohn-io-gbr/shortener-ui](https://github.com/yohn-io-gbr/shortener-ui). Do not add this directory to `yohn-aws` and do not push it there.

## Deployment

The published app is [https://shortener-ui-nmaetmpwczhcpzxghdo2sa.streamlit.app/](https://shortener-ui-nmaetmpwczhcpzxghdo2sa.streamlit.app/). Opening that URL redirects to Streamlit sign-in. The console then asks for `AUTH_USERNAME` and `AUTH_PASSWORD`. API calls still require an access JWT pasted on the Auth tab. Do not store the JWT as a secret.

1. Sign in to [Streamlit Community Cloud](https://share.streamlit.io) with the GitHub account that can see `yohn-io-gbr/shortener-ui`.
2. Deploy branch `main` and main file `app.py`. Python is 3.12.
3. In the app secrets editor, set `AUTH_USERNAME`, `AUTH_PASSWORD`, and `SHORTENER_BASE_URL`. Do not commit the Worker origin.
4. The app sleeps when idle and wakes on the next visit. That is acceptable.

## Out of scope

Cognito login in the app UI, Worker or CORS changes, production deploys, polling, charts, domain onboarding, and a public demo with a built-in token.
