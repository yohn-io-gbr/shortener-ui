# Staging shortener console

Streamlit console for the staging shortener. The app calls the Worker from Python. The browser does not call the Worker. The Worker origin is `SHORTENER_BASE_URL` in Streamlit secrets and is not stored in this repository.

This directory is a client. It does not belong in `yohn-aws`, and it must not be pushed to that repository. The shortener CI fingerprints its own workflow and source files.

Python 3.12.

The access JWT stays in the browser session. Do not put a JWT, Cognito password, Analytics Engine token, or smoke token in this tree, in `secrets.toml.example`, or in Streamlit secrets. The console password belongs only in Streamlit secrets as `AUTH_PASSWORD`.

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

Cognito login, Worker or CORS changes, production deploys, polling, charts, domain onboarding, and a public demo with a built-in token.
