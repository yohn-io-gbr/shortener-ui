"""HTTP client for the staging shortener. No Streamlit imports."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

STAGING_HOST = "yohn-shortener-stage.staging-yohn-aws.workers.dev"
STAGING_BASE_URL = f"https://{STAGING_HOST}"
DEFAULT_TENANT = "stage-test-simple"
MAX_STATS_DAYS = 366

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ETAG = re.compile(r'^"[1-9][0-9]*"$')
_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")
_ALLOWED_PATH = re.compile(r"^/(?:health|ready|api/v1(?:/.*)?)$")
_SELECTED_HEADERS = ("etag", "location", "x-request-id", "content-type")


class ClientError(ValueError):
    """Local rejection. No HTTP call was made."""


@dataclass
class CallResult:
    method: str
    path: str
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    parsed: Any = None
    text: str = ""
    transport_error: str | None = None
    idempotency_key: str | None = None


def guard_path(path: str) -> None:
    if not isinstance(path, str) or not _ALLOWED_PATH.fullmatch(path):
        raise ClientError("This console does not request public short links.")


def require_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ClientError(f"{label} must match ^[A-Za-z0-9_-]{{1,128}}$.")
    return value


def validate_base_url(url: str, allow_other_origin: bool = False) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ClientError(
            "Base URL must be an https URL with no username, password, query, or fragment."
        )
    parsed = urlsplit(url.strip())
    if parsed.username is not None or parsed.password is not None:
        raise ClientError("Base URL must not include a username or password.")
    if parsed.query or parsed.fragment:
        raise ClientError("Base URL must not include a query or fragment.")
    if parsed.path not in ("", "/"):
        raise ClientError("Base URL must not include a path.")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ClientError("Base URL host is not valid.") from exc
    if not host:
        raise ClientError("Base URL must include a host.")
    scheme = parsed.scheme.lower()
    staging = scheme == "https" and host == STAGING_HOST
    local_http = scheme == "http" and host in {"localhost", "127.0.0.1"}
    other_https = scheme == "https"
    if not (staging or (allow_other_origin and (local_http or other_https))):
        if scheme == "http":
            raise ClientError(
                "http is allowed only for localhost or 127.0.0.1 when another origin is allowed."
            )
        if scheme == "https":
            raise ClientError(
                "Base URL host must be the staging Worker unless another origin is allowed."
            )
        raise ClientError("Base URL must use https.")
    netloc = host if port is None else f"{host}:{port}"
    return f"{scheme}://{netloc}"


def canonical_json(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"))


def new_idempotency_key() -> str:
    key = secrets.token_hex(16)
    if not _KEY.fullmatch(key):
        raise ClientError("Generated an invalid idempotency key.")
    return key


def _digest(operation: str, canonical: str) -> str:
    return hashlib.sha256(f"{operation}\n{canonical}".encode("utf-8")).hexdigest()


def lookup_idempotency_key(
    store: Mapping[str, Any], operation: str, body: Mapping[str, Any]
) -> str | None:
    canonical = canonical_json(body)
    current = store.get(_digest(operation, canonical))
    if isinstance(current, dict) and current.get("body") == canonical:
        key = current.get("key")
        if isinstance(key, str):
            return key
    return None


def resolve_idempotency(
    store: dict[str, Any], operation: str, body: Mapping[str, Any]
) -> tuple[str, str]:
    """Return the key and canonical body. The same body reuses the stored key."""
    canonical = canonical_json(body)
    digest = _digest(operation, canonical)
    current = store.get(digest)
    if isinstance(current, dict) and current.get("body") == canonical and isinstance(current.get("key"), str):
        return current["key"], canonical
    key = new_idempotency_key()
    store[digest] = {"key": key, "body": canonical}
    return key, canonical


def validate_date_range(start: str, end: str) -> None:
    start_day = _utc_date(start)
    end_day = _utc_date(end)
    days = (end_day - start_day).days
    if days <= 0 or days > MAX_STATS_DAYS:
        raise ClientError("Use a forward range of at most 366 days.")


def default_stats_range(today: date | None = None) -> tuple[str, str]:
    current = today or datetime.now(timezone.utc).date()
    return (current - timedelta(days=7)).isoformat(), (current + timedelta(days=1)).isoformat()


def etag_revision(etag: str | None) -> int | None:
    if not isinstance(etag, str) or not _ETAG.fullmatch(etag):
        return None
    return int(etag[1:-1])


def revisions_agree(link: Mapping[str, Any] | None, etag: str | None) -> bool:
    if not isinstance(link, Mapping):
        return False
    quoted = etag_revision(etag)
    return quoted is not None and quoted == link.get("revision")


def error_object(result: CallResult) -> dict[str, Any] | None:
    body = result.parsed
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error
    return None


def classify_analytics(result: CallResult) -> str:
    if result.transport_error:
        return "transport"
    if result.status_code == 404 and result.parsed is None and result.text == "":
        return "not_configured"
    if result.status_code == 404:
        return "not_found"
    if result.status_code == 200:
        return "report"
    return "http"


def _utc_date(value: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ClientError("Use a real UTC calendar date.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ClientError("Use a real UTC calendar date.") from exc
    if parsed.isoformat() != value:
        raise ClientError("Use a real UTC calendar date.")
    return parsed


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ClientError("limit must be an integer from 1 to 200.")
    return limit


def normalize_access_token(token: str) -> str:
    """Return the JWT with surrounding whitespace and a pasted Bearer prefix removed."""
    if not isinstance(token, str):
        raise ClientError("Paste the access JWT as a single line.")
    cleaned = token.strip()
    if cleaned[:6].lower() == "bearer" and (len(cleaned) == 6 or cleaned[6].isspace()):
        cleaned = cleaned[6:].strip()
    return "".join(cleaned.split())


def _etag(etag: str) -> str:
    if not isinstance(etag, str) or not _ETAG.fullmatch(etag):
        raise ClientError('If-Match must be a quoted revision such as "3".')
    return etag


def _group_by(group_by: str) -> str:
    if group_by not in {"country", "referrer"}:
        raise ClientError("groupBy must be country or referrer.")
    return group_by


def _result_from_response(method: str, path: str, response: httpx.Response) -> CallResult:
    content_type = response.headers.get("content-type", "")
    text = response.text
    parsed = None
    if text != "" and "application/json" in content_type.lower():
        try:
            parsed = response.json()
        except json.JSONDecodeError:
            parsed = None
    headers = {
        name: response.headers[name] for name in _SELECTED_HEADERS if name in response.headers
    }
    return CallResult(
        method=method,
        path=path,
        status_code=response.status_code,
        headers=headers,
        parsed=parsed,
        text=text,
    )


class ShortenerClient:
    def __init__(
        self,
        base_url: str,
        tenant_id: str,
        access_token: str,
        allow_other_origin: bool = False,
        idempotency: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = validate_base_url(base_url, allow_other_origin)
        self.tenant_id = tenant_id
        self.access_token = access_token
        self.idempotency = idempotency if idempotency is not None else {}

    def health(self) -> CallResult:
        return self._request("GET", "/health", authenticated=False)

    def ready(self) -> CallResult:
        return self._request("GET", "/ready", authenticated=False)

    def list_links(
        self,
        *,
        domain_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CallResult:
        params: dict[str, str] = {"limit": str(_limit(limit))}
        if domain_id:
            params["domainId"] = require_id(domain_id, "domainId")
        if status:
            if status not in {"active", "disabled"}:
                raise ClientError("status must be active or disabled.")
            params["status"] = status
        if search:
            if not isinstance(search, str) or not 1 <= len(search) <= 256:
                raise ClientError("search must be 1–256 characters.")
            params["search"] = search
        if cursor:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ClientError("cursor must be at most 4096 characters.")
            params["cursor"] = cursor
        return self._request("GET", "/api/v1/links", query=params)

    def create_link(self, body: Mapping[str, Any]) -> CallResult:
        key, canonical = resolve_idempotency(self.idempotency, "create_link", body)
        result = self._request(
            "POST",
            "/api/v1/links",
            content=canonical.encode("utf-8"),
            content_type="application/json",
            extra_headers={"Idempotency-Key": key},
        )
        result.idempotency_key = key
        return result

    def get_link(self, link_id: str) -> CallResult:
        return self._request("GET", f"/api/v1/links/{require_id(link_id, 'link id')}")

    def lookup_link(self, domain_id: str, slug: str) -> CallResult:
        domain = require_id(domain_id, "domainId")
        exact = require_id(slug, "slug")
        return self._request("GET", f"/api/v1/domains/{domain}/links/{exact}")

    def patch_link(self, link_id: str, etag: str, changes: Mapping[str, Any]) -> CallResult:
        if not changes:
            raise ClientError("Provide at least one update.")
        canonical = canonical_json(changes)
        return self._request(
            "PATCH",
            f"/api/v1/links/{require_id(link_id, 'link id')}",
            content=canonical.encode("utf-8"),
            content_type="application/json",
            extra_headers={"If-Match": _etag(etag)},
        )

    def delete_link(self, link_id: str, etag: str) -> CallResult:
        return self._request(
            "DELETE",
            f"/api/v1/links/{require_id(link_id, 'link id')}",
            extra_headers={"If-Match": _etag(etag)},
        )

    def tenant_stats(self, start: str, end: str, response_format: str = "json") -> CallResult:
        query, accept = self._stats_query(start, end, response_format)
        return self._request("GET", "/api/v1/stats", query=query, accept=accept)

    def link_stats(
        self, link_id: str, start: str, end: str, response_format: str = "json"
    ) -> CallResult:
        query, accept = self._stats_query(start, end, response_format)
        link = require_id(link_id, "link id")
        return self._request("GET", f"/api/v1/links/{link}/stats", query=query, accept=accept)

    def tenant_analytics(
        self, start: str, end: str, group_by: str, response_format: str = "json"
    ) -> CallResult:
        grouping = _group_by(group_by)
        query, accept = self._stats_query(start, end, response_format)
        query["groupBy"] = grouping
        return self._request("GET", "/api/v1/analytics", query=query, accept=accept)

    def link_analytics(
        self, link_id: str, start: str, end: str, group_by: str, response_format: str = "json"
    ) -> CallResult:
        grouping = _group_by(group_by)
        query, accept = self._stats_query(start, end, response_format)
        query["groupBy"] = grouping
        link = require_id(link_id, "link id")
        return self._request(
            "GET", f"/api/v1/links/{link}/analytics", query=query, accept=accept
        )

    def disable_analytics(self) -> CallResult:
        return self._request("POST", "/api/v1/analytics/disable")

    def add_user(self, body: Mapping[str, Any]) -> CallResult:
        key, canonical = resolve_idempotency(self.idempotency, "add_user", body)
        result = self._request(
            "POST",
            "/api/v1/users",
            content=canonical.encode("utf-8"),
            content_type="application/json",
            extra_headers={"Idempotency-Key": key},
        )
        result.idempotency_key = key
        return result

    def _stats_query(self, start: str, end: str, response_format: str) -> tuple[dict[str, str], str]:
        validate_date_range(start, end)
        if response_format not in {"json", "csv"}:
            raise ClientError("format must be json or csv.")
        accept = "text/csv" if response_format == "csv" else "application/json"
        return {"start": start, "end": end, "format": response_format}, accept

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
        extra_headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
    ) -> CallResult:
        guard_path(path)
        headers = {"Accept": accept}
        if authenticated:
            token = normalize_access_token(self.access_token)
            if not token:
                raise ClientError("The access JWT is missing, expired, or not the access token.")
            tenant = self.tenant_id.strip() if isinstance(self.tenant_id, str) else ""
            if not tenant:
                raise ClientError("Tenant id is required.")
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Tenant-Id"] = tenant
        if content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)
        try:
            with httpx.Client(timeout=10.0, follow_redirects=False) as http:
                response = http.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=dict(query) if query else None,
                    content=content,
                )
        except httpx.TimeoutException:
            return CallResult(method=method, path=path, transport_error="timeout")
        except httpx.LocalProtocolError:
            return CallResult(method=method, path=path, transport_error="header")
        except httpx.TransportError:
            return CallResult(method=method, path=path, transport_error="connection")
        return _result_from_response(method, path, response)
