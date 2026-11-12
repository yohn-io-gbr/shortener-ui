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

The suite currently collects **60 tests: 47 unit/UI workflow tests and 13 Chromium browser
cases**. Plain `pytest` runs only the unit tests; browser tests are opt-in.

### Coverage

| File / area | Cases | Checks | External requests |
| --- | ---: | --- | --- |
| `tests/test_auth.py` | 2 | Missing console credentials, wrong password, successful sign-in, six tabs, sign-out | None |
| `tests/test_client.py` | 17 | Token cleanup, auth/tenant headers, health/readiness headers, redirects, idempotency and retries, revisions, pagination, date validation, recorded stats and analytics responses, URL/path validation, credential comparison, unpublished Worker origin | None; HTTP is mocked |
| `tests/test_console_flows.py` | 28 | Credential clearing and blocked requests; session isolation; input errors; filters/pagination; Lookup and loaded details; totals, daily/empty stats, permissions and CSV contents; health/readiness success UI | None; synthetic credentials and strictly mocked HTTP |
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
The browser tests do not click the statistics buttons or fetch statistics/CSV data.
The mocked UI workflow tests exercise those buttons and verify returned data and
the exact CSV payload, filename, and MIME type passed to the download control.

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
send a member-creation request; these browser UI checks do not verify admin permissions.
Mocked validation tests click Add member with invalid input and verify that no
HTTP request is sent. Credential-clearing tests also verify that valid-looking
member input cannot send a request after the JWT is removed.

### Additional workflow checks

`tests/test_console_flows.py` covers six areas without a running server, real
credentials, or external requests. Streamlit AppTest drives the actual UI; respx
rejects any HTTP request not explicitly mocked.

| Area | Checks |
| --- | --- |
| API response handling | Mocked health/readiness render their successful JSON bodies; filtered List returns and displays the mocked page. Live health/readiness/List checks remain in the browser suite. |
| Clear JWT/session | Both Clear access JWT and Clear session restore all missing-JWT warnings, clear cached data/drafts/idempotency keys, and prevent List, stats, analytics, and member requests. Console sign-in remains active. |
| Session isolation | Sign-out removes cached links, selection, responses, tokens, and drafts before another operator signs in. Independent app sessions do not share loaded data. |
| Input validation | Reversed, zero-length, and over-366-day stats ranges; missing Link id; missing/invalid issuer and invalid subject. Each displays an error and sends no request. |
| Links | Filter parameters, next-page cursor, disabled pagination at the end, cursor reset after filter changes, exact case-sensitive Lookup, not-found response, loaded link/ETag, stats link selection, and revision-mismatch warning. |
| Recorded results and CSV | Tenant/link totals and exact daily rows, empty results, tenant permission errors, CSV format/Accept headers, exact download contents and filenames. |

```sh
pytest tests/test_console_flows.py
```

Clearing credentials now resets cached links, selected records, drafts, filters,
and idempotency keys as well as tokens and responses. Signing out resets all
session state. Successful List/Next page requests rerender immediately so the
pagination buttons reflect the newly returned cursor.

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

Cognito login uses `USER_SRP_AUTH`. A lazily requested, session-scoped `cognito_auth`
fixture loads local credentials and generates one access JWT shared by the four
authenticated cases in a pytest process. Local-only cases never request the fixture
or contact Cognito. Each test still uses a separate browser context, and the shared
credentials/token remain in memory without recording authenticated browser artifacts.
SMS, authenticator, and email MFA challenges use the optional current `mfa_code`;
one MFA challenge may still be required per pytest session. Remove the code after
use. Starting a new pytest process (or another parallel worker) performs a new login;
an unusually long run may outlive the shared token. Account-setup challenges such
as a forced password change are not handled.

Date-control checks target Streamlit's `stDateInput` widgets and their visible,
enabled `spinbutton` segments. Both date-widget counts and start/end labels are
checked without depending on locale-specific date formatting.

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

# Mocked UI workflows for the six additional areas.
pytest tests/test_console_flows.py

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

Latest PR #1 P1/P2 validation (2026-11-12): **47 unit/mocked UI tests passed**,
the independent synthetic browser test passed, and **8 local-only browser cases
passed** (including the synthetic case). A temporary count-only audit confirmed
zero credential loads and zero Cognito login attempts in the local-only run.
The **4 authenticated cases failed before their JWT-dependent assertions** because
the Cognito endpoint was unreachable; the audit confirmed one credential load and
one shared login attempt across all four. Their live JWT-dependent behavior,
including the two authenticated date-control cases, remains unverified in this run.
The remaining health/readiness case failed with Worker connection errors. All 60
cases still collect successfully; no test cases were removed.

A successful browser UI check confirms visibility, enabled state, or warning
behavior; it does not establish that a statistics or analytics API request succeeds.
The mocked UI workflows verify response rendering, Lookup, and CSV download data
without contacting the Worker. Saving CSV files through an actual browser is not
covered. No tests create/update/delete real links, add real members, or disable analytics.

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
