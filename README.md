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

## Tests

The suite currently collects **32 tests: 19 unit tests and 13 Chromium browser
cases**. Plain `pytest` runs only the unit tests; browser tests are opt-in.

### Coverage

| File / area | Cases | Checks | External requests |
| --- | ---: | --- | --- |
| `tests/test_auth.py` | 2 | Missing console credentials, wrong password, successful sign-in, six tabs, sign-out | None |
| `tests/test_client.py` | 17 | Token cleanup, auth/tenant headers, health/readiness headers, redirects, idempotency and retries, revisions, pagination, date validation, recorded stats and analytics responses, URL/path validation, credential comparison, unpublished Worker origin | None; HTTP is mocked |
| `e2e/test_availability.py` | 1 | `http://localhost:8501/` returns HTTP 200 and shows the console heading | Local app only |
| `e2e/test_browser_auth.py` | 1 | Rejects a wrong password, accepts temporary test credentials, shows all six tabs, signs out | Isolated local app only |
| `e2e/test_local_auth.py`: console and navigation | 2 | Local secrets sign-in/sign-out; clicks Auth, Session, Links, Recorded stats, Sampled analytics, Members and verifies selected tabs and visible panels | Local app only |
| `e2e/test_local_auth.py`: Links controls | 1 | Lookup, Loaded link, Create headings; visible, enabled Lookup button | Local app only |
| `e2e/test_local_auth.py`: Session | 1 | Clicks Check health, then Check ready; expects HTTP 200 with `{"status":"ok"}` and `{"status":"ready"}` respectively | Worker `/health` and `/ready` |
| `e2e/test_local_auth.py`: Recorded stats | 2 | All eight controls visible/enabled; warning visible without a JWT and hidden after Auth is filled | Local app; JWT case also calls Cognito |
| `e2e/test_local_auth.py`: Sampled analytics | 2 | Both expanders open; date fields, scope options, Country/Referrer buttons and confirmation checkbox enabled; checkbox unchecked and Disable analytics disabled; warning hidden after Auth is filled | Local app; JWT case also calls Cognito |
| `e2e/test_local_auth.py`: Members | 2 | Tenant-admin guidance; issuer/subject inputs, editor/viewer role options and Add member button visible/enabled; editor selected by default; warning hidden after Auth is filled | Local app; JWT case also calls Cognito |
| `e2e/test_local_auth.py`: Auth and List | 1 | Generates an access JWT, fills Tenant id and Access JWT, hides Auth/Links warnings, verifies List controls, clicks List and expects HTTP 200 | Cognito and Worker `GET /api/v1/links` |

Recorded stats controls: **start**, **end**, **Link id**, **Tenant stats**,
**Download tenant CSV**, **Use selected link**, **Link stats**, **Download link CSV**.
The tests do not click the statistics buttons or fetch statistics/CSV data.

Sampled analytics checks **analytics start**, **analytics end**, **Scope**
(**tenant** / **link**), **Country**, **Referrer**, **Confirm analytics disable**,
and **Disable analytics**. The tests never click Country, Referrer, the confirmation
checkbox, or Disable analytics. They send no analytics reads or disable requests.
The warning checked here is `Paste the access JWT on the Auth tab. Opening this tab does not call the API.`
Recorded stats has its separate `Paste the access JWT on the Auth tab before loading stats.` warning.

Members checks **issuer**, **subject**, **role** (**editor** / **viewer**), and
**Add member**, both without a token and after filling Auth with a Cognito access
JWT. The warning `Paste the access JWT on the Auth tab before adding a member.`
must appear initially and disappear after a JWT is set. Both cases check the
tenant-admin guidance and default editor role. They never click Add member or
send a member-creation request; these UI checks do not verify admin permissions.

### Setup and local secrets

In the active Python 3.12 virtual environment:

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium
```

Keep console credentials and the Worker origin in the ignored
`.streamlit/secrets.toml` created from `secrets.toml.example`. Tests that generate
a JWT also read this section from that same local file:

```toml
[local_tests]
tenant_id = "your-test-tenant"
user = "your-test-user"
pass = "your-test-password"
cognito_region = "your-aws-region"
cognito_user_pool_id = "your-cognito-user-pool-id"
cognito_client_id = "your-cognito-app-client-id"
# cognito_client_secret = "only-if-this-app-client-has-a-secret"
# mfa_code = "current-code-only-if-Cognito-requests-MFA"
```

Cognito login uses `USER_SRP_AUTH`. SMS, authenticator, and email MFA challenges use
the optional current `mfa_code`; remove it after use. A new login may require a new
code. Account-setup challenges such as a forced password change are not handled.

Except for the isolated authentication test, browser tests expect an app already
running at `http://localhost:8501/`. Start it in one terminal:

```sh
source .venv/bin/activate
streamlit run app.py
```

Run tests in a second terminal with the same virtual environment activated.
`test_browser_auth.py` starts and stops its own app on an available port with
synthetic credentials and no Worker origin; it does not use local secrets.

### Run commands

```sh
# Unit tests only; no browser or external HTTP requests.
pytest

# Isolated browser authentication test; no running app required.
pytest e2e/test_browser_auth.py --browser chromium

# Local UI checks without Cognito or Worker requests; running app required.
pytest e2e --browser chromium -k 'not generated_access_jwt and not check_health'

# Focused local app checks.
pytest e2e/test_availability.py
pytest e2e/test_local_auth.py -k lookup
pytest e2e/test_local_auth.py -k recorded_stats
pytest e2e/test_local_auth.py -k sampled_analytics
pytest e2e/test_local_auth.py -k members
pytest e2e/test_local_auth.py -k check_health
pytest e2e/test_local_auth.py -k generated_access_jwt

# All browser cases, including Cognito and Worker requests.
pytest e2e --browser chromium

# Collect all tests without executing them.
pytest tests e2e --collect-only -q
```

`recorded_stats`, `sampled_analytics`, and `members` each select a local-only case
and a Cognito case. `generated_access_jwt` selects four cases: Recorded stats,
Sampled analytics, Members, and Auth/List. `check_health` selects the combined health/readiness case, which
attempts both requests even if health returns an error.

### Results, limits, and artifacts

A successful UI check confirms visibility, enabled state, or warning behavior;
it does not establish that a statistics or analytics API request succeeds.
Lookup is not submitted, links are not created/updated/deleted, CSV downloads are
not exercised, and members are not created.

Health/readiness and List are live response checks. They fail on DNS/connection
errors, unexpected status codes, or unexpected health/readiness bodies. They do
not substitute mock responses. For `Transport failure (connection)`, check DNS,
network access, and the configured `SHORTENER_BASE_URL`. Cognito DNS failures
prevent JWT generation before the UI warning checks can run. The last observed
health/readiness and List runs failed with connection errors; retry them after
fixing the Worker connection rather than treating UI passes as API passes.

Tests using real secrets create separate browser contexts without recording
traces, screenshots, or video, and suppress sensitive Playwright error diagnostics.
JWTs remain in process/browser memory; they are not saved in the secrets file.
Do not enable SDK debug logging or publish local secrets, credentials, or tokens.

For visible browser runs or failure artifacts from the synthetic/availability tests:

```sh
pytest e2e/test_browser_auth.py --headed
pytest e2e/test_browser_auth.py --tracing retain-on-failure --screenshot only-on-failure
```

`.streamlit/secrets.toml`, `.venv/`, `test-results/`, and `playwright-report/` are
ignored by Git. Do not upload `[local_tests]` to Streamlit Cloud. On Linux CI,
install browser system dependencies with `python -m playwright install --with-deps chromium`.
No CI workflow has been added for these local tests.

The console never requests `GET` or `HEAD /{slug}`. Open `shortUrl` in the browser
when a real redirect test is needed.

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
