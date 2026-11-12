"""Private Streamlit console for the staging shortener.

Every Worker call is behind a button. The browser never calls the Worker.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import streamlit as st

from client import (
    CallResult,
    ClientError,
    ShortenerClient,
    classify_analytics,
    credentials_match,
    default_stats_range,
    error_object,
    lookup_idempotency_key,
    normalize_access_token,
    require_id,
    revisions_agree,
    validate_base_url,
)

_SUBJECT = re.compile(r"^[\x21-\x7e]{1,255}$")


def init_state() -> None:
    if "base_url" not in st.session_state:
        st.session_state.base_url = configured_origin() or ""
    st.session_state.setdefault("allow_other_origin", False)
    st.session_state.setdefault("tenant_id", "stage-test-simple")
    st.session_state.setdefault("access_token", "")
    st.session_state.setdefault("idempotency", {})
    st.session_state.setdefault(
        "links",
        {
            "items": [],
            "next_cursor": None,
            "filters": {"domain_id": "", "status": "", "search": "", "limit": 50},
        },
    )
    st.session_state.setdefault("selected_link", None)
    st.session_state.setdefault("selected_etag", None)
    st.session_state.setdefault("results", {})


def basic_credentials() -> tuple[str, str] | None:
    try:
        username = st.secrets["AUTH_USERNAME"]
        password = st.secrets["AUTH_PASSWORD"]
    except Exception:
        return None
    if isinstance(username, str) and isinstance(password, str) and username and password:
        return username, password
    return None


def require_basic_auth() -> bool:
    if st.session_state.get("basic_auth_ok") is True:
        st.session_state.basic_password = ""
        return True
    st.title("Staging shortener console")
    expected = basic_credentials()
    if expected is None:
        st.error("Set AUTH_USERNAME and AUTH_PASSWORD in Streamlit secrets before using this app.")
        return False
    st.subheader("Sign in")
    st.caption("This password only opens the console. The access JWT is still entered on the Auth tab.")
    username = st.text_input("Username", key="basic_username")
    password = st.text_input("Password", type="password", key="basic_password")
    if st.button("Sign in"):
        if credentials_match(username, password, expected[0], expected[1]):
            st.session_state.basic_auth_ok = True
            st.rerun()
        st.error("Username or password is wrong.")
    return False


def sign_out() -> None:
    st.session_state.clear()


def configured_origin() -> str | None:
    raw = secret_base_url()
    if not raw:
        return None
    try:
        return validate_base_url(raw, allow_other_origin=True)
    except ClientError:
        return None


def allowed_host() -> str | None:
    origin = configured_origin()
    if not origin:
        return None
    return urlsplit(origin).hostname


def secret_base_url() -> str | None:
    try:
        value = st.secrets["SHORTENER_BASE_URL"]
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def clear_session() -> None:
    authenticated = st.session_state.get("basic_auth_ok") is True
    st.session_state.clear()
    st.session_state.basic_auth_ok = authenticated


def make_client() -> ShortenerClient:
    return ShortenerClient(
        base_url=st.session_state.base_url,
        tenant_id=st.session_state.tenant_id,
        access_token=st.session_state.access_token,
        allow_other_origin=bool(st.session_state.allow_other_origin),
        idempotency=st.session_state.idempotency,
        allowed_host=allowed_host(),
    )


def run(name: str, action) -> CallResult | None:
    try:
        client = make_client()
    except ClientError as exc:
        st.error(str(exc))
        return None
    try:
        result = action(client)
    except ClientError as exc:
        st.session_state.idempotency = dict(client.idempotency)
        st.error(str(exc))
        return None
    st.session_state.idempotency = dict(client.idempotency)
    st.session_state.results = {**st.session_state.results, name: result}
    return result


def explain_error(status: int | None, error: dict[str, Any]) -> str:
    code = error.get("code") if isinstance(error.get("code"), str) else ""
    message = error.get("message") if isinstance(error.get("message"), str) else None
    details = error.get("details")
    if status == 400:
        parts = [part for part in (code, message) if part]
        if isinstance(details, dict) and details:
            parts.append(json.dumps(details, ensure_ascii=True, sort_keys=True))
        return "\n".join(parts) if parts else "Invalid request."
    sentences = {
        401: "The access JWT is missing, expired, or not the access token.",
        403: "This identity is not a member, or the role or website binding cannot do this.",
        413: "Request exceeded 16 KiB.",
        415: "JSON content type was not accepted.",
        503: "Outcome of a write may be unknown. Retry with the same idempotency key.",
    }
    if status == 404:
        text = "Resource missing."
    elif status == 409:
        text = {
            "revision_conflict": "Reload the link before editing again.",
            "slug_conflict": "Slug is reserved, including after deletion.",
            "idempotency_conflict": "Same key, different body, still inside 24 hours.",
            "membership_conflict": "That identity is already a member.",
        }.get(code, message or "Conflict.")
    else:
        text = sentences.get(status or 0, message or "")
    if code and text:
        return f"{code}: {text}"
    return code or text or f"HTTP {status}"


def show_status(result: CallResult) -> bool:
    st.caption(f"{result.method} {result.path}")
    if result.transport_error == "header":
        st.error("The access JWT could not be sent. Paste it again as a single line.")
        return True
    if result.transport_error:
        st.error(f"Transport failure ({result.transport_error}).")
        if result.idempotency_key:
            st.warning("The idempotency key was kept. Press the button again.")
            st.code(result.idempotency_key)
        return True
    st.write(f"HTTP {result.status_code}")
    request_id = result.headers.get("x-request-id")
    if request_id:
        st.caption(f"x-request-id: {request_id}")
    return False


def show_body(result: CallResult) -> None:
    error = error_object(result)
    if error:
        st.error(explain_error(result.status_code, error))
        return
    if result.parsed is not None:
        st.json(result.parsed)
        return
    if result.text:
        st.code(result.text)


def show_kept_write(result: CallResult) -> None:
    if result.idempotency_key:
        st.caption("Idempotency-Key")
        st.code(result.idempotency_key)
    if result.status_code == 503 and result.idempotency_key:
        st.warning("The idempotency key was kept. Press the button again.")


def format_epoch_ms(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return ""
    try:
        stamp = datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def allowlist_entries(text: str) -> list[str]:
    values = [line for line in text.splitlines() if line != ""]
    if not 1 <= len(values) <= 100:
        raise ClientError("Allowlist needs 1 to 100 entries.")
    for value in values:
        if not 1 <= len(value) <= 128:
            raise ClientError("Each allowlist entry must be 1 to 128 characters.")
    return values


def build_create_body(
    domain_id: str,
    slug: str,
    destination: str,
    case_insensitive: bool,
    policy: str,
    allow_text: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {"domainId": require_id(domain_id, "domainId")}
    if slug:
        body["slug"] = require_id(slug, "slug")
    if not isinstance(destination, str) or not 1 <= len(destination) <= 8192:
        raise ClientError("destinationUrl must be 1 to 8192 characters.")
    body["caseInsensitive"] = bool(case_insensitive)
    body["destinationUrl"] = destination
    if policy == "allowlist":
        body["queryPolicy"] = {"mode": "allowlist", "allowlist": allowlist_entries(allow_text)}
    elif policy != "none":
        raise ClientError("Query policy must be none or allowlist.")
    return body


def build_changes(
    link: dict[str, Any], destination: str, status: str, policy: str, allow_text: str
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if destination:
        if not 1 <= len(destination) <= 8192:
            raise ClientError("destinationUrl must be 1 to 8192 characters.")
        if destination != link.get("destinationUrl"):
            changes["destinationUrl"] = destination
    if status:
        if status not in {"active", "disabled"}:
            raise ClientError("status must be active or disabled.")
        if status != link.get("status"):
            changes["status"] = status
    current = link.get("queryPolicy") if isinstance(link.get("queryPolicy"), dict) else {}
    if policy == "none" and current.get("mode") != "none":
        changes["queryPolicy"] = {"mode": "none", "allowlist": []}
    elif policy == "allowlist":
        entries = allowlist_entries(allow_text)
        current_entries = [item for item in current.get("allowlist") or [] if isinstance(item, str)]
        if current.get("mode") != "allowlist" or current_entries != entries:
            changes["queryPolicy"] = {"mode": "allowlist", "allowlist": entries}
    if not changes:
        raise ClientError("Provide at least one update.")
    return changes


def build_member(issuer: str, subject: str, role: str) -> dict[str, str]:
    if not isinstance(issuer, str) or not issuer or len(issuer) > 2048:
        raise ClientError("Issuer must be a URL of at most 2048 characters.")
    parsed = urlsplit(issuer)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ClientError("Issuer must be an http or https URL. Type the configured issuer.")
    if not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
        raise ClientError("Subject must be 1 to 255 non-whitespace printable ASCII characters.")
    if role not in {"editor", "viewer"}:
        raise ClientError("Role must be editor or viewer.")
    return {"issuer": issuer, "subject": subject, "role": role}


def as_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ClientError("Use a real UTC calendar date.")


def date_text(value: Any) -> str | None:
    try:
        return as_iso(value)
    except ClientError:
        return None


def ensure_dates(start_key: str, end_key: str) -> None:
    start, end = default_stats_range()
    st.session_state.setdefault(start_key, date.fromisoformat(start))
    st.session_state.setdefault(end_key, date.fromisoformat(end))


def apply_list_page(result: CallResult, filters: dict[str, Any]) -> None:
    if result.status_code != 200 or not isinstance(result.parsed, dict):
        return
    items = result.parsed.get("items")
    cursor = result.parsed.get("nextCursor")
    st.session_state.links = {
        "items": items if isinstance(items, list) else [],
        "next_cursor": cursor if isinstance(cursor, str) and cursor else None,
        "filters": dict(filters),
    }


def refresh_current_list(client: ShortenerClient) -> None:
    filters = dict(st.session_state.links["filters"])
    result = client.list_links(
        domain_id=filters.get("domain_id") or None,
        status=filters.get("status") or None,
        search=filters.get("search") or None,
        limit=int(filters.get("limit") or 50),
    )
    st.session_state.results = {**st.session_state.results, "list_links": result}
    apply_list_page(result, filters)


def remember_loaded_link(result: CallResult) -> None:
    if result.status_code == 200 and isinstance(result.parsed, dict):
        st.session_state.selected_link = result.parsed
        st.session_state.selected_etag = result.headers.get("etag")


def use_selected_link() -> None:
    link = st.session_state.selected_link
    if isinstance(link, dict) and isinstance(link.get("id"), str):
        st.session_state.stats_link_id = link["id"]


def token_is_present() -> bool:
    try:
        return bool(normalize_access_token(st.session_state.access_token))
    except ClientError:
        return False


def render_sidebar() -> None:
    st.sidebar.text_input("Base URL", key="base_url")
    st.sidebar.checkbox("Allow another https origin", key="allow_other_origin")
    st.sidebar.caption("http is accepted only for localhost or 127.0.0.1, and only while this is on.")
    if token_is_present():
        st.sidebar.success("Access JWT is set. Change it on the Auth tab.")
    else:
        st.sidebar.warning("Paste the access JWT on the Auth tab.")
    st.sidebar.button("Sign out", on_click=sign_out)
    st.sidebar.button("Clear session", on_click=clear_session)
    st.sidebar.caption("Clear session deletes the access JWT, cached links, drafts, and API responses.")
    try:
        validate_base_url(
            st.session_state.base_url,
            bool(st.session_state.allow_other_origin),
            allowed_host(),
        )
    except ClientError as exc:
        st.sidebar.warning(str(exc))


def render_auth() -> None:
    st.subheader("Access JWT")
    st.markdown(
        "Paste the staging Cognito **access** JWT for a member of this tenant. "
        "An ID token is the wrong token. The value stays in this browser session and is not saved."
    )
    st.text_input("Tenant id", key="tenant_id")
    st.text_input(
        "Access JWT",
        type="password",
        key="access_token",
        placeholder="eyJ... Paste one line. Do not include the word Bearer.",
    )
    cleaned = ""
    try:
        cleaned = normalize_access_token(st.session_state.access_token)
    except ClientError as exc:
        st.error(str(exc))
    if not cleaned:
        st.warning("No access JWT yet. API calls stay blocked until you paste one.")
    elif cleaned.count(".") == 2:
        st.success("Access JWT is stored for this browser session.")
    else:
        st.warning("A value is stored, but it does not look like a JWT. Use the access token, not the ID token.")
    st.button("Clear access JWT", on_click=clear_session)


def render_session() -> None:
    if not token_is_present():
        st.warning("Paste the access JWT on the Auth tab before calling the API.")
    st.caption("The base URL is in the sidebar. The access JWT is on the Auth tab and stays in this browser session only.")
    if st.button("Check health"):
        run("health", lambda client: client.health())
    health = st.session_state.results.get("health")
    if isinstance(health, CallResult):
        if show_status(health):
            pass
        elif health.status_code == 200 and health.parsed == {"status": "ok"}:
            st.success('{"status":"ok"}')
        else:
            show_body(health)
    if st.button("Check ready"):
        run("ready", lambda client: client.ready())
    ready = st.session_state.results.get("ready")
    if isinstance(ready, CallResult):
        if show_status(ready):
            pass
        elif ready.status_code == 503:
            st.warning("not ready")
            show_body(ready)
        elif ready.status_code == 200 and ready.parsed == {"status": "ready"}:
            st.success('{"status":"ready"}')
        else:
            show_body(ready)


def render_links() -> None:
    if not token_is_present():
        st.warning("Paste the access JWT on the Auth tab before listing links.")
    st.subheader("List")
    domain_id = st.text_input("domainId", key="list_domain")
    status = st.selectbox(
        "status",
        ["", "active", "disabled"],
        key="list_status",
        format_func=lambda value: "all non-deleted" if value == "" else value,
    )
    search = st.text_input("search", key="list_search")
    st.session_state.setdefault("list_limit", 50)
    limit = int(st.number_input("limit", min_value=1, max_value=200, step=1, key="list_limit"))
    draft = {"domain_id": domain_id, "status": status, "search": search, "limit": limit}
    if draft != st.session_state.links["filters"]:
        st.session_state.links = {**st.session_state.links, "next_cursor": None}
    list_clicked = st.button("List")
    next_cursor = st.session_state.links.get("next_cursor")
    next_clicked = st.button("Next page", disabled=not next_cursor)
    if list_clicked:
        filters = dict(draft)

        def action(client: ShortenerClient, filters: dict[str, Any] = filters) -> CallResult:
            result = client.list_links(
                domain_id=filters["domain_id"] or None,
                status=filters["status"] or None,
                search=filters["search"] or None,
                limit=filters["limit"],
            )
            apply_list_page(result, filters)
            return result

        listed_now = run("list_links", action)
        if isinstance(listed_now, CallResult) and listed_now.status_code == 200:
            st.rerun()
    elif next_clicked and draft == st.session_state.links["filters"] and next_cursor:
        filters = dict(st.session_state.links["filters"])

        def action(
            client: ShortenerClient, filters: dict[str, Any] = filters, cursor: str = next_cursor
        ) -> CallResult:
            result = client.list_links(
                domain_id=filters["domain_id"] or None,
                status=filters["status"] or None,
                search=filters["search"] or None,
                limit=int(filters["limit"]),
                cursor=cursor,
            )
            apply_list_page(result, filters)
            return result

        listed_now = run("list_links", action)
        if isinstance(listed_now, CallResult) and listed_now.status_code == 200:
            st.rerun()
    listed = st.session_state.results.get("list_links")
    if isinstance(listed, CallResult):
        if not show_status(listed):
            if listed.status_code != 200:
                show_body(listed)
    items = [item for item in st.session_state.links["items"] if isinstance(item, dict)]
    if items:
        st.dataframe(
            [
                {
                    "slug": item.get("slug"),
                    "status": item.get("status"),
                    "domainId": item.get("domainId"),
                    "destinationUrl": item.get("destinationUrl"),
                    "revision": item.get("revision"),
                    "shortUrl": item.get("shortUrl"),
                    "id": item.get("id"),
                    "createdAt": format_epoch_ms(item.get("createdAt")),
                    "updatedAt": format_epoch_ms(item.get("updatedAt")),
                }
                for item in items
            ],
            hide_index=True,
        )
    elif isinstance(listed, CallResult) and listed.status_code == 200:
        st.caption("No links on this page.")
    ids = [item.get("id") for item in items if isinstance(item.get("id"), str)]
    choice = None
    if ids:
        if "link_choice" in st.session_state and st.session_state.link_choice not in ids:
            del st.session_state.link_choice
        select_kwargs: dict[str, Any] = {
            "options": ids,
            "placeholder": "Choose a link",
            "key": "link_choice",
        }
        if "link_choice" not in st.session_state:
            select_kwargs["index"] = None
        choice = st.selectbox("Selected link", **select_kwargs)
    chosen = next((item for item in items if item.get("id") == choice), None)
    if isinstance(chosen, dict) and isinstance(chosen.get("shortUrl"), str):
        show_short_url(chosen["shortUrl"])
    if st.button("Load link", disabled=not choice):

        def action(client: ShortenerClient, link_id: str = choice) -> CallResult:
            result = client.get_link(link_id)
            remember_loaded_link(result)
            return result

        run("get_link", action)
    loaded = st.session_state.results.get("get_link")
    if isinstance(loaded, CallResult):
        render_loaded(loaded)

    st.subheader("Lookup")
    st.caption("Exact canonical identity. The slug is not trimmed or case-folded.")
    lookup_domain = st.text_input("Lookup domainId", key="lookup_domain")
    lookup_slug = st.text_input("Lookup slug", key="lookup_slug")
    if st.button("Lookup"):

        def action(
            client: ShortenerClient, domain: str = lookup_domain, slug: str = lookup_slug
        ) -> CallResult:
            result = client.lookup_link(domain, slug)
            remember_loaded_link(result)
            return result

        run("lookup_link", action)
    looked_up = st.session_state.results.get("lookup_link")
    if isinstance(looked_up, CallResult):
        render_loaded(looked_up)

    render_selected_link()
    render_create()
    render_update()


def show_short_url(short_url: str) -> None:
    st.caption("shortUrl")
    st.code(short_url)
    if short_url.startswith("https://"):
        st.link_button("Open short URL in the browser", short_url)
    st.caption("Opening the short URL is a real redirect in your browser. This app does not request the slug.")


def render_loaded(result: CallResult) -> None:
    if show_status(result):
        return
    if result.status_code == 404:
        st.error("The link is gone.")
        return
    if result.headers.get("etag"):
        st.caption("ETag")
        st.code(result.headers["etag"])
    show_body(result)


def render_selected_link() -> None:
    link = st.session_state.selected_link
    etag = st.session_state.selected_etag
    st.subheader("Loaded link")
    if not isinstance(link, dict):
        st.caption("No link loaded.")
        return
    st.write({"id": link.get("id"), "revision": link.get("revision"), "ETag": etag or ""})
    short_url = link.get("shortUrl")
    if isinstance(short_url, str) and short_url:
        show_short_url(short_url)
    if revisions_agree(link, etag):
        st.caption("ETag matches revision.")
    else:
        st.error("The ETag and revision disagree. Reload the link before PATCH or DELETE.")


def render_create() -> None:
    st.subheader("Create")
    domain_id = st.text_input("Create domainId", key="create_domain")
    slug = st.text_input("slug", key="create_slug")
    destination = st.text_area("destinationUrl", key="create_destination")
    case_insensitive = st.checkbox("caseInsensitive", key="create_case")
    policy = st.radio("Query policy", ["none", "allowlist"], key="create_policy", horizontal=True)
    allow_text = st.text_area("Allowlist, one entry per line", key="create_allowlist")
    if st.button("Create link"):
        try:
            body = build_create_body(domain_id, slug, destination, case_insensitive, policy, allow_text)
        except ClientError as exc:
            st.error(str(exc))
        else:

            def action(client: ShortenerClient, payload: dict[str, Any] = body) -> CallResult:
                result = client.create_link(payload)
                if result.status_code == 201:
                    refresh_current_list(client)
                return result

            created_now = run("create_link", action)
            if isinstance(created_now, CallResult) and created_now.status_code == 201:
                st.rerun()
    try:
        draft = build_create_body(domain_id, slug, destination, case_insensitive, policy, allow_text)
    except ClientError:
        draft = None
    if draft is not None:
        key = lookup_idempotency_key(st.session_state.idempotency, "create_link", draft)
        if key:
            st.caption("Idempotency-Key")
            st.code(key)
    created = st.session_state.results.get("create_link")
    if isinstance(created, CallResult):
        render_write(created)


def render_write(result: CallResult) -> None:
    stopped = show_status(result)
    show_kept_write(result)
    if stopped:
        return
    if result.status_code == 201:
        if result.headers.get("location"):
            st.caption("Location")
            st.code(result.headers["location"])
        if result.headers.get("etag"):
            st.caption("ETag")
            st.code(result.headers["etag"])
    if result.status_code == 404:
        st.error("The link is gone.")
        return
    show_body(result)


def render_update() -> None:
    link = st.session_state.selected_link
    etag = st.session_state.selected_etag
    if not isinstance(link, dict):
        return
    agreed = revisions_agree(link, etag)
    st.subheader("Update")
    destination = st.text_input("New destinationUrl", key="edit_destination")
    status = st.selectbox(
        "New status",
        ["", "active", "disabled"],
        key="edit_status",
        format_func=lambda value: "unchanged" if value == "" else value,
    )
    policy = st.selectbox(
        "New query policy",
        ["", "none", "allowlist"],
        key="edit_policy",
        format_func=lambda value: "unchanged" if value == "" else value,
    )
    allow_text = st.text_area("New allowlist, one entry per line", key="edit_allowlist")
    if st.button("Update link", disabled=not agreed):
        try:
            changes = build_changes(link, destination, status, policy, allow_text)
        except ClientError as exc:
            st.error(str(exc))
        else:

            def action(client: ShortenerClient, payload: dict[str, Any] = changes) -> CallResult:
                result = client.patch_link(str(link["id"]), str(etag), payload)
                if result.status_code == 200 and isinstance(result.parsed, dict):
                    st.session_state.selected_link = result.parsed
                    st.session_state.selected_etag = result.headers.get("etag")
                elif result.status_code == 409 and (error_object(result) or {}).get("code") == "revision_conflict":
                    reloaded = client.get_link(str(link["id"]))
                    st.session_state.results = {**st.session_state.results, "get_link": reloaded}
                    remember_loaded_link(reloaded)
                return result

            patched_now = run("patch_link", action)
            if isinstance(patched_now, CallResult) and patched_now.status_code in {200, 409}:
                st.rerun()
    patched = st.session_state.results.get("patch_link")
    if isinstance(patched, CallResult):
        render_write(patched)
    st.subheader("Delete")
    st.caption("There is no undelete. A repeated delete is 404, not a second success.")
    confirm = st.checkbox("Confirm permanent delete", key="confirm_delete")
    if st.button("Delete link", disabled=not (agreed and confirm)):

        def action(client: ShortenerClient) -> CallResult:
            result = client.delete_link(str(link["id"]), str(etag))
            if result.status_code == 204:
                st.session_state.selected_link = None
                st.session_state.selected_etag = None
                refresh_current_list(client)
            return result

        deleted_now = run("delete_link", action)
        if isinstance(deleted_now, CallResult) and deleted_now.status_code == 204:
            st.rerun()
    deleted = st.session_state.results.get("delete_link")
    if isinstance(deleted, CallResult):
        if deleted.status_code == 204:
            show_status(deleted)
            st.success("Deleted.")
        else:
            render_write(deleted)


def render_stats() -> None:
    if not token_is_present():
        st.warning("Paste the access JWT on the Auth tab before loading stats.")
    st.caption("Recorded D1 counts, including bots and repeat GETs. These are not Analytics Engine estimates.")
    ensure_dates("stats_start", "stats_end")
    start = date_text(st.date_input("start", key="stats_start"))
    end = date_text(st.date_input("end", key="stats_end"))
    if st.button("Tenant stats"):
        if start and end:
            run("tenant_stats", lambda client: client.tenant_stats(start, end))
        else:
            st.error("Use a real UTC calendar date.")
    tenant = st.session_state.results.get("tenant_stats")
    if isinstance(tenant, CallResult):
        render_recorded(tenant, tenant_scope=True)
    if st.button("Download tenant CSV"):
        if start and end:
            run("tenant_stats_csv", lambda client: client.tenant_stats(start, end, response_format="csv"))
        else:
            st.error("Use a real UTC calendar date.")
    tenant_csv = st.session_state.results.get("tenant_stats_csv")
    if isinstance(tenant_csv, CallResult):
        render_csv(tenant_csv, "save_tenant_csv", "tenant-stats.csv")
    st.session_state.setdefault("stats_link_id", "")
    selected = st.session_state.selected_link
    selected_id = selected.get("id") if isinstance(selected, dict) else None
    st.caption(f"Selected link: {selected_id or 'none'}")
    st.button("Use selected link", on_click=use_selected_link)
    link_id = st.text_input("Link id", key="stats_link_id")
    if st.button("Link stats"):
        if start and end:
            run("link_stats", lambda client: client.link_stats(link_id, start, end))
        else:
            st.error("Use a real UTC calendar date.")
    link_stats = st.session_state.results.get("link_stats")
    if isinstance(link_stats, CallResult):
        render_recorded(link_stats, tenant_scope=False)
    if st.button("Download link CSV"):
        if start and end:
            run("link_stats_csv", lambda client: client.link_stats(link_id, start, end, response_format="csv"))
        else:
            st.error("Use a real UTC calendar date.")
    link_csv = st.session_state.results.get("link_stats_csv")
    if isinstance(link_csv, CallResult):
        render_csv(link_csv, "save_link_csv", "link-stats.csv")


def render_recorded(result: CallResult, *, tenant_scope: bool) -> None:
    if show_status(result):
        return
    if result.status_code == 403 and tenant_scope:
        show_body(result)
        st.warning("This identity cannot read tenant stats. Use link stats.")
        return
    if result.status_code == 200 and isinstance(result.parsed, dict):
        st.write("total", result.parsed.get("total"))
        daily = result.parsed.get("daily")
        rows = [row for row in daily if isinstance(row, dict)] if isinstance(daily, list) else []
        if rows:
            st.dataframe(
                [{"date": row.get("date"), "recorded": row.get("recorded")} for row in rows],
                hide_index=True,
            )
        else:
            st.caption("The API returned no daily rows.")
        st.caption("Missing days mean zero. This table shows only the rows the API returned.")
        return
    show_body(result)


def render_csv(result: CallResult, download_key: str, file_name: str) -> None:
    if show_status(result):
        return
    st.caption("CSV columns are date,recorded. The total is only in the JSON response.")
    if result.status_code == 200 and result.text:
        st.download_button("Save CSV file", data=result.text, file_name=file_name, mime="text/csv", key=download_key)
        return
    show_body(result)


def render_analytics() -> None:
    if not token_is_present():
        st.warning("Paste the access JWT on the Auth tab. Opening this tab does not call the API.")
    st.caption("Each admitted read spends staging budget, including a failed read. Opening this tab does not call the API.")
    with st.expander("Sampled analytics reads"):
        ensure_dates("analytics_start", "analytics_end")
        start = date_text(st.date_input("analytics start", key="analytics_start"))
        end = date_text(st.date_input("analytics end", key="analytics_end"))
        scope = st.radio("Scope", ["tenant", "link"], horizontal=True, key="analytics_scope")
        selected = st.session_state.selected_link
        link_id = selected.get("id") if isinstance(selected, dict) else None
        if scope == "link":
            st.caption(f"Selected link: {link_id or 'none'}")
        if st.button("Country"):
            run_analytics("country", scope, link_id, start, end)
        country = st.session_state.results.get(f"analytics_{scope}_country")
        if isinstance(country, CallResult):
            render_sampled(country, tenant_scope=scope == "tenant")
        if st.button("Referrer"):
            run_analytics("referrer", scope, link_id, start, end)
        referrer = st.session_state.results.get(f"analytics_{scope}_referrer")
        if isinstance(referrer, CallResult):
            render_sampled(referrer, tenant_scope=scope == "tenant")
    with st.expander("Emergency stop"):
        st.caption("This is an emergency stop. It does not recall writes already accepted.")
        confirm = st.checkbox("Confirm analytics disable", key="confirm_disable")
        if st.button("Disable analytics", disabled=not confirm):
            run("disable_analytics", lambda client: client.disable_analytics())
        disabled = st.session_state.results.get("disable_analytics")
        if isinstance(disabled, CallResult):
            render_disable(disabled)


def run_analytics(group_by: str, scope: str, link_id: str | None, start: str | None, end: str | None) -> None:
    if not start or not end:
        st.error("Use a real UTC calendar date.")
        return
    if scope == "link" and not link_id:
        st.error("Select a link on the Links tab first.")
        return

    def action(client: ShortenerClient) -> CallResult:
        if scope == "tenant":
            return client.tenant_analytics(start, end, group_by)
        return client.link_analytics(str(link_id), start, end, group_by)

    run(f"analytics_{scope}_{group_by}", action)


def render_sampled(result: CallResult, *, tenant_scope: bool) -> None:
    if show_status(result):
        return
    if classify_analytics(result) == "not_configured":
        st.warning("Analytics is not configured for that tenant.")
        return
    if result.status_code == 403 and tenant_scope:
        show_body(result)
        st.warning("A website-bound identity must use link scope.")
        return
    if result.status_code == 200 and isinstance(result.parsed, dict):
        body = result.parsed
        coverage = body.get("coverage") if isinstance(body.get("coverage"), dict) else {}
        st.write(
            {
                "resultStatus": body.get("resultStatus"),
                "coverage.status": coverage.get("status"),
                "countKind": body.get("countKind"),
                "source": body.get("source"),
            }
        )
        if body.get("resultStatus") == "no_observations" or coverage.get("status") in {
            "partial",
            "unknown",
            "unavailable",
        }:
            st.caption("not a zero count")
        data = body.get("data") if isinstance(body.get("data"), list) else []
        rows = [row for row in data if isinstance(row, dict)]
        if rows:
            st.dataframe(
                [
                    {
                        "value": row.get("value"),
                        "estimatedCount": row.get("estimatedCount"),
                        "observedRows": row.get("observedRows"),
                    }
                    for row in rows
                ],
                hide_index=True,
            )
        return
    show_body(result)


def render_disable(result: CallResult) -> None:
    if show_status(result):
        return
    if result.status_code == 200 and isinstance(result.parsed, dict):
        body = result.parsed
        st.json(
            {
                "tenantId": body.get("tenantId"),
                "status": body.get("status"),
                "revision": body.get("revision"),
                "submittedWritesRecallable": body.get("submittedWritesRecallable"),
                "propagation": body.get("propagation"),
            }
        )
        return
    show_body(result)


def render_members() -> None:
    if not token_is_present():
        st.warning("Paste the access JWT on the Auth tab before adding a member.")
    st.caption("Tenant admin only. This does not create a Cognito user. Type the existing subject and the exact issuer.")
    issuer = st.text_input("issuer", key="member_issuer")
    subject = st.text_input("subject", key="member_subject")
    role = st.radio("role", ["editor", "viewer"], horizontal=True, key="member_role")
    if st.button("Add member"):
        try:
            body = build_member(issuer, subject, role)
        except ClientError as exc:
            st.error(str(exc))
        else:

            def action(client: ShortenerClient, payload: dict[str, str] = body) -> CallResult:
                return client.add_user(payload)

            run("add_user", action)
    try:
        draft = build_member(issuer, subject, role)
    except ClientError:
        draft = None
    if draft is not None:
        key = lookup_idempotency_key(st.session_state.idempotency, "add_user", draft)
        if key:
            st.caption("Idempotency-Key")
            st.code(key)
    added = st.session_state.results.get("add_user")
    if isinstance(added, CallResult):
        render_member_result(added)


def render_member_result(result: CallResult) -> None:
    stopped = show_status(result)
    show_kept_write(result)
    if stopped:
        return
    if result.status_code == 201 and isinstance(result.parsed, dict):
        st.json(
            {
                "principalId": result.parsed.get("principalId"),
                "tenantId": result.parsed.get("tenantId"),
                "role": result.parsed.get("role"),
            }
        )
        return
    show_body(result)


def main() -> None:
    st.set_page_config(page_title="Staging shortener console", layout="wide")
    if not require_basic_auth():
        return
    init_state()
    render_sidebar()
    st.title("Staging shortener console")
    st.caption("Calls the staging Worker from Python. The browser does not call the Worker.")
    auth, session, links, stats, analytics, members = st.tabs(
        ["Auth", "Session", "Links", "Recorded stats", "Sampled analytics", "Members"]
    )
    with auth:
        render_auth()
    with session:
        render_session()
    with links:
        render_links()
    with stats:
        render_stats()
    with analytics:
        render_analytics()
    with members:
        render_members()


if __name__ == "__main__":
    main()
