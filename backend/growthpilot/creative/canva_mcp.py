"""Restricted Canva MCP client for private design previews.

The module exposes only read, generation, asset, and export tools. It does not
provide publish, folder, comment, or telemetry operations. The MCP SDK is loaded
lazily so the rest of GrowthPilot remains usable without Canva MCP installed.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

MCP_URL = "https://api.canva.com/connect/v1/mcp"
_PREVIEW_SCOPES = frozenset({
    "profile:read", "design:meta:read", "design:content:read", "design:content:write",
    "asset:read", "asset:write",
})

_CANVA_URL = re.compile(
    r"(?i)https?://[^\s\"'<>]+|(?:www\.)?canva\.com/[^\s\"'<>]+|"
    r"design\.canva\.ai/[^\s\"'<>]+"
)
_SENSITIVE_QUERY = re.compile(r"\?[^\s\"'<>]*=[^\s\"'<>]*")
_DESIGN_PATH_ID = re.compile(r"(?i)/(?:api/)?design/([A-Za-z0-9_-]{6,})")


def redact_canva_urls(value: Any, *, design_id: str | None = None) -> Any:
    """Redact Canva links and URL query data before logging or persisting responses."""
    if isinstance(value, dict):
        identity = value.get("design_id") or value.get("id") or design_id
        result = {}
        for key, item in value.items():
            if str(key).casefold() in {"edit_url", "view_url", "url", "download_url", "thumbnail_url"}:
                result[key] = f"[design:{identity}]" if identity else "[redacted-url]"
            else:
                result[key] = redact_canva_urls(item, design_id=str(identity) if identity else None)
        return result
    if isinstance(value, list):
        return [redact_canva_urls(item, design_id=design_id) for item in value]
    if not isinstance(value, str):
        return value

    def replace_url(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlparse(url if "://" in url else "https://" + url)
        design = _DESIGN_PATH_ID.search(parsed.path)
        is_canva = parsed.hostname and (parsed.hostname == "canva.com" or
            parsed.hostname.endswith(".canva.com") or parsed.hostname == "design.canva.ai")
        is_signed_canva_path = bool(is_canva and (parsed.hostname == "design.canva.ai" or
            "/api/design/" in parsed.path.casefold() or "/design/" in parsed.path.casefold()))
        if parsed.query or is_signed_canva_path:
            return f"[design:{design.group(1)}]" if design else "[redacted-url]"
        return url

    safe = _CANVA_URL.sub(replace_url, value)
    return _SENSITIVE_QUERY.sub("[redacted-url]", safe)
ALLOWED_TOOLS = frozenset({
    "generate-design", "get-create-design-async-job", "get-design-candidates",
    "create-design-from-candidate", "create-design", "create-upload-url",
    "upload-asset-from-url", "generate-image", "get-generate-image-job",
    "remove-background", "export-design", "get-export-formats", "get-design",
    "get-design-pages", "search-designs",
})


class CanvaMCPError(RuntimeError):
    """Safe error from the restricted Canva MCP surface."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 exception_type: str | None = None, redacted_message: str = "") -> None:
        super().__init__(message)
        self.http_status = http_status
        self.exception_type = exception_type
        self.redacted_message = redacted_message

    @classmethod
    def from_exception(cls, message: str, exc: BaseException) -> CanvaMCPError:
        detail = _safe_exception_detail(exc)
        error = cls(f"{message}: {detail}", http_status=_http_status(exc),
                    exception_type=type(exc).__name__, redacted_message=detail)
        error.__cause__ = exc
        return error


class SlidingWindowLimiter:
    """Process-local request limiter matching Canva's published minute ceilings."""

    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = {"generate-design": deque(), "generate-image": deque(), "upload": deque()}
        self._lock = threading.Lock()

    async def acquire(self, operation: str) -> None:
        limits = {"generate-design": 20, "generate-image": 10, "upload": 30}
        key = operation if operation in limits else "upload"
        maximum = limits[key]
        while True:
            with self._lock:
                now = time.monotonic()
                calls = self._calls[key]
                while calls and now - calls[0] >= 60:
                    calls.popleft()
                if len(calls) < maximum:
                    calls.append(now)
                    return
                delay = max(0.01, 60 - (now - calls[0]))
            await asyncio.sleep(delay)


_limiter = SlidingWindowLimiter()


def reserve_daily_generation(db: Any, *, limit: int = 20, now: datetime | None = None,
                             operation: str = "generate-design") -> None:
    """Count the attempt before the external call so failures cannot evade the daily cap."""
    from sqlalchemy import select

    from ..models import UsageRecord

    now = now or datetime.now(UTC).replace(tzinfo=None)
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if operation not in {"generate-design", "generate-image"}:
        raise CanvaMCPError("Unsupported Canva generation operation.")
    count = db.execute(select(UsageRecord.id).where(
        UsageRecord.provider == "canva-mcp", UsageRecord.operation.in_(["generate-design", "generate-image"]),
        UsageRecord.created_at >= since,
    )).scalars().all()
    if len(count) >= limit:
        raise CanvaMCPError(f"Canva MCP daily generation cap reached ({limit}).")
    db.add(UsageRecord(provider="canva-mcp", operation=operation, model="Canva MCP",
                       units=1, cost_usd=0.0, created_at=now))
    db.flush()


def jwt_expiry(token: str) -> datetime | None:
    """Decode a JWT exp claim locally; return None for opaque or malformed tokens."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        exp = payload.get("exp")
        return datetime.fromtimestamp(exp, UTC) if isinstance(exp, (int, float)) else None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, OverflowError):
        return None


def _near_expiry(token: str, *, now: datetime | None = None) -> bool:
    expiry = jwt_expiry(token)
    return expiry is not None and expiry <= (now or datetime.now(UTC)) + timedelta(minutes=5)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def oauth_metadata(mcp_url: str = MCP_URL) -> dict[str, Any]:
    """Discover OAuth endpoints from the MCP resource challenge, never guessed URLs."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            challenge = await client.get(mcp_url)
            if challenge.status_code != 401:
                raise CanvaMCPError(f"Canva MCP discovery expected HTTP 401, received HTTP {challenge.status_code}.")
            header = challenge.headers.get("www-authenticate", "")
            resource_match = re.search(r'resource_metadata="([^"]+)"', header)
            scope_match = re.search(r'\bscope="([^"]+)"', header)
            if not resource_match:
                raise CanvaMCPError("Canva MCP authentication challenge omitted OAuth resource metadata.")
            resource_metadata_url = urlparse(resource_match.group(1))
            if resource_metadata_url.scheme != "https" or not resource_metadata_url.netloc:
                raise CanvaMCPError("Canva MCP challenge returned an invalid resource metadata URL.")
            resource_response = await client.get(resource_match.group(1))
            resource_response.raise_for_status()
            resource = resource_response.json()
            issuers = resource.get("authorization_servers") or []
            if not issuers:
                raise CanvaMCPError("Canva OAuth metadata omitted an authorization server.")
            issuer = urlparse(issuers[0])
            if issuer.scheme != "https" or not issuer.netloc:
                raise CanvaMCPError("Canva OAuth metadata returned an invalid authorization server.")
            metadata_url = (f"{issuer.scheme}://{issuer.netloc}/.well-known/oauth-authorization-server"
                            f"{issuer.path.rstrip('/')}")
            response = await client.get(metadata_url)
            response.raise_for_status()
            metadata = response.json()
            for field in ("authorization_endpoint", "token_endpoint"):
                parsed = urlparse(metadata.get(field, ""))
                if parsed.scheme != "https" or not parsed.netloc:
                    raise CanvaMCPError(f"Canva OAuth metadata omitted a valid HTTPS {field}.")
            # RFC 9728 resource metadata owns the resource's supported scopes.
            # Canva's authorization-server document does not advertise them.
            advertised_scopes = resource.get("scopes_supported") or metadata.get("scopes_supported")
            metadata["required_scopes"] = (
                scope_match.group(1).split() if scope_match else
                [scope for scope in advertised_scopes if isinstance(scope, str) and scope in _PREVIEW_SCOPES]
                if isinstance(advertised_scopes, list) else []
            )
            resource_uri = resource.get("resource")
            if isinstance(resource_uri, str) and urlparse(resource_uri).scheme == "https":
                metadata["resource"] = resource_uri
            return metadata
    except CanvaMCPError:
        raise
    except Exception as exc:
        raise CanvaMCPError.from_exception("Canva OAuth endpoint discovery failed", exc) from exc


def build_authorization_url(metadata: dict[str, Any], *, client_id: str, redirect_uri: str,
                            state: str, challenge: str) -> str:
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
             "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    scopes = metadata.get("required_scopes") or []
    if scopes:
        query["scope"] = " ".join(scopes)
    if metadata.get("resource"):
        query["resource"] = metadata["resource"]
    return metadata["authorization_endpoint"] + "?" + urlencode(query)


async def exchange_authorization_code(*, metadata: dict[str, Any], client_id: str, client_secret: str,
                                      redirect_uri: str, code: str, verifier: str) -> dict[str, Any]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            form = {
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
            if metadata.get("resource"):
                form["resource"] = metadata["resource"]
            response = await client.post(metadata["token_endpoint"], data=form, auth=(client_id, client_secret),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            if response.is_error:
                raise CanvaMCPError(f"Canva OAuth code exchange failed: HTTP {response.status_code}.",
                                    http_status=response.status_code, exception_type="HTTPStatusError")
            payload = response.json()
            access, refresh = payload.get("access_token"), payload.get("refresh_token")
            if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
                raise CanvaMCPError("Canva OAuth response omitted required tokens.")
            return {"access_token": access, "refresh_token": refresh,
                    "expires_in": payload.get("expires_in"), "token_type": payload.get("token_type", "Bearer")}
    except CanvaMCPError:
        raise
    except Exception as exc:
        raise CanvaMCPError.from_exception("Canva OAuth code exchange failed", exc) from exc


async def refresh_mcp_tokens(*, client_id: str, client_secret: str, refresh_token: str,
                             persist_tokens: Callable[[str, str], None], status_sink: list[int] | None = None,
                             mcp_url: str = MCP_URL) -> tuple[str, str]:
    """Discover OAuth metadata and persist rotated token pair through encrypted storage."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            challenge = await client.get(mcp_url)
            if challenge.status_code != 401:
                raise CanvaMCPError(f"OAuth discovery expected HTTP 401, received HTTP {challenge.status_code}.")
            resource_url = re.search(r'resource_metadata="([^"]+)"', challenge.headers.get("www-authenticate", ""))
            if not resource_url:
                raise CanvaMCPError("Canva MCP 401 challenge did not advertise resource metadata.")
            resource_metadata_url = urlparse(resource_url.group(1))
            if resource_metadata_url.scheme != "https" or not resource_metadata_url.netloc:
                raise CanvaMCPError("Canva MCP challenge returned an invalid resource metadata URL.")
            resource_response = await client.get(resource_url.group(1))
            resource_response.raise_for_status()
            issuers = resource_response.json().get("authorization_servers") or []
            if not issuers:
                raise CanvaMCPError("Canva OAuth resource metadata did not list an authorization server.")
            issuer = urlparse(issuers[0])
            if issuer.scheme != "https" or not issuer.netloc:
                raise CanvaMCPError("Canva OAuth metadata listed an invalid authorization server.")
            resource_uri = resource_response.json().get("resource")
            metadata_url = f"{issuer.scheme}://{issuer.netloc}/.well-known/oauth-authorization-server{issuer.path.rstrip('/')}"
            metadata_response = await client.get(metadata_url)
            metadata_response.raise_for_status()
            token_endpoint = metadata_response.json().get("token_endpoint", "")
            token_parsed = urlparse(token_endpoint)
            if token_parsed.scheme != "https" or not token_parsed.netloc:
                raise CanvaMCPError("Canva OAuth metadata did not list an HTTPS token endpoint.")
            form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
            if isinstance(resource_uri, str) and urlparse(resource_uri).scheme == "https":
                form["resource"] = resource_uri
            response = await client.post(token_endpoint, data=form, auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            if status_sink is not None:
                status_sink.append(response.status_code)
            if response.is_error:
                body = _redact_error(response.text)
                raise CanvaMCPError(f"Canva OAuth refresh failed: HTTP {response.status_code}: {body}",
                                    http_status=response.status_code, exception_type="HTTPStatusError",
                                    redacted_message=body)
            payload = response.json()
            access, rotated = payload.get("access_token"), payload.get("refresh_token")
            if not isinstance(access, str) or not access or not isinstance(rotated, str) or not rotated:
                raise CanvaMCPError("Canva OAuth refresh response omitted rotated MCP tokens.")
            # Refresh tokens may be single-use, so save both rotated values before returning either.
            persist_tokens(access, rotated)
            return access, rotated
    except CanvaMCPError:
        raise
    except Exception as exc:  # details are sanitized before they enter the error or logs
        raise CanvaMCPError.from_exception("Canva MCP token refresh failed", exc) from exc


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            found = _http_status(child)
            if found is not None:
                return found
    response = getattr(exc, "response", None)
    value = (getattr(response, "status_code", None) or getattr(exc, "http_status", None)
             or getattr(exc, "status_code", None))
    return value if isinstance(value, int) else None


@asynccontextmanager
async def connected_client(*, access_token: str, refresh_token: str, client_id: str, client_secret: str,
                           persist_tokens: Callable[[str, str], None], mcp_url: str = MCP_URL,
                           timeout: float = 60.0, db: Any = None,
                           max_daily_generations: int = 20):
    """Refresh within five minutes of expiry or once after 401, then connect."""
    refreshed = False
    if _near_expiry(access_token):
        try:
            access_token, refresh_token = await refresh_mcp_tokens(
                client_id=client_id, client_secret=client_secret, refresh_token=refresh_token,
                persist_tokens=persist_tokens, mcp_url=mcp_url,
            )
            refreshed = True
        except Exception as exc:
            raise CanvaMCPError.from_exception(
                "Saved Canva MCP token is expired or within five minutes of expiry; refresh failed", exc
            ) from exc
    client = CanvaMCPClient(access_token, timeout=timeout, db=db, mcp_url=mcp_url,
                            max_daily_generations=max_daily_generations)
    client._diagnostic_secrets = (refresh_token, client_secret)
    try:
        await client.__aenter__()
    except Exception as exc:
        await _close_partial(client)
        if _http_status(exc) != 401 or refreshed:
            raise CanvaMCPError.from_exception("Canva MCP connection failed", exc) from exc
        try:
            access_token, refresh_token = await refresh_mcp_tokens(
                client_id=client_id, client_secret=client_secret, refresh_token=refresh_token,
                persist_tokens=persist_tokens, mcp_url=mcp_url,
            )
        except Exception as refresh_exc:
            raise CanvaMCPError.from_exception(
                "Saved Canva MCP refresh failed; reauthorization is required", refresh_exc
            ) from refresh_exc
        client = CanvaMCPClient(access_token, timeout=timeout, db=db, mcp_url=mcp_url,
                                max_daily_generations=max_daily_generations)
        client._diagnostic_secrets = (refresh_token, client_secret)
        try:
            await client.__aenter__()
        except Exception as retry_exc:
            await _close_partial(client)
            raise CanvaMCPError.from_exception(
                "Canva MCP connection after token refresh failed", retry_exc
            ) from retry_exc
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


async def _close_partial(client: CanvaMCPClient) -> None:
    try:
        await client.__aexit__(None, None, None)
    except Exception as exc:
        _ = type(exc).__name__


_credential_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def connected_workspace_client(db: Any, workspace_id: int, settings: Any):
    """Connect using workspace-encrypted tokens; persist rotations before reuse."""
    from sqlalchemy import select

    from ..models import ProviderCredential, utcnow
    from ..security import decrypt_json, encrypt_json

    lock = _credential_locks.setdefault(workspace_id, asyncio.Lock())
    async with lock:
        row = db.execute(select(ProviderCredential).where(
            ProviderCredential.workspace_id == workspace_id,
            ProviderCredential.provider == "canva_mcp",
        ).with_for_update()).scalar_one_or_none()
        if row is None:
            raise CanvaMCPError("Canva is not connected for this workspace.")
        credentials = decrypt_json(row.credentials_enc)

        def persist(access: str, refresh: str) -> None:
            credentials.update(access_token=access, refresh_token=refresh)
            row.credentials_enc = encrypt_json(credentials)
            row.updated_at = utcnow()
            db.commit()

        async with connected_client(
            access_token=credentials.get("access_token", ""),
            refresh_token=credentials.get("refresh_token", ""),
            client_id=settings.canva_client_id,
            client_secret=settings.canva_client_secret,
            persist_tokens=persist,
            mcp_url=settings.canva_mcp_url,
            db=db,
            max_daily_generations=getattr(settings, "canva_mcp_daily_cap", 10),
        ) as client:
            yield client, row


def safe_tool_name(name: str) -> str:
    normalized = name.replace("_", "-")
    if normalized not in ALLOWED_TOOLS:
        raise CanvaMCPError(f"Canva MCP tool is not allow-listed: {normalized}")
    return normalized


def json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def _redact_error(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    value = redact_canva_urls(value)
    sensitive = re.compile(r"token|secret|authorization|upload.?url|code|verifier", re.I)
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if sensitive.search(str(key)) else _redact_error(item, secrets=secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_error(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[^\s\"',;]+", "Bearer [REDACTED]", value)
        # Handle JSON (including quoted values containing spaces), form fields,
        # and a truncated final field before applying any output length limit.
        field = r"(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|code|code_verifier|upload.?url)"
        value = re.sub(
            rf'''(?ix)(["']?{field}["']?\s*[:=]\s*)("(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;}}]+)''',
            r'\1"[REDACTED]"', value,
        )
        value = re.sub(r"(https://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", value)
        value = re.sub(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
                       "[REDACTED_JWT]", value)
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value[:1200]
    return value


def safe_canva_message(value: Any, *, secrets: tuple[str, ...] = ()) -> str:
    """Return a display-safe Canva error without tokens, codes, or signed URLs."""
    safe = _redact_error(value, secrets=secrets)
    return safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=True, default=str)[:1200]


def _safe_exception_detail(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        details = [_safe_exception_detail(child) for child in exc.exceptions]
        return "; ".join(dict.fromkeys(details))[:1200]
    status = _http_status(exc)
    status_text = f"HTTP {status}" if status is not None else "HTTP status unavailable"
    message = str(_redact_error(str(exc))) or "(empty message)"
    return f"{type(exc).__name__}, {status_text}: {message}"


class CanvaMCPClient:
    """A scoped facade over the official MCP Python SDK Streamable HTTP client."""

    def __init__(self, access_token: str, *, timeout: float = 60.0, db: Any = None,
                 max_daily_generations: int = 20, mcp_url: str = MCP_URL):
        if not access_token:
            raise CanvaMCPError("A Canva MCP access token is not configured.")
        self._token = access_token
        self._timeout = timeout
        self._db = db
        self._max_daily_generations = max_daily_generations
        self._mcp_url = mcp_url
        self._session: Any = None
        self._transport_context: Any = None
        self._session_context: Any = None
        self._http_client: Any = None
        self._sdk_v2 = False
        self._transport_owned_by_session = False
        self._tools: dict[str, Any] = {}
        self._transport_error: CanvaMCPError | None = None
        self._diagnostic_secrets: tuple[str, ...] = ()

    async def _capture_response(self, response: Any) -> None:
        """Capture failed MCP POSTs before the SDK discards HTTP diagnostics.

        Successful/SSE responses and optional background GETs remain streaming.
        Only a bounded, sanitized error body is made available to the SDK, too,
        so a JSON-RPC error cannot reintroduce secrets through SDK logging.
        """
        if response.request.method != "POST" or 200 <= response.status_code < 300:
            return
        chunks = bytearray()
        limit = 8192
        truncated = False
        try:
            async for chunk in response.aiter_bytes(chunk_size=1024):
                remaining = limit - len(chunks)
                chunks.extend(chunk[:remaining])
                if len(chunks) >= limit:
                    truncated = True
                    break
        except Exception:
            # Never stringify read failures: they can include request credentials.
            truncated = True
        finally:
            await response.aclose()
        raw = chunks.decode("utf-8", errors="replace")
        safe = safe_canva_message(raw, secrets=(self._token, *self._diagnostic_secrets))
        if truncated:
            safe = safe[:1100] + " [body truncated or unavailable]"
        # Both supported httpx versions cache read bodies in _content. The stream
        # is closed above; replay only the safe, bounded bytes to the SDK.
        response._content = safe.encode("utf-8")
        status = response.status_code
        self._transport_error = CanvaMCPError(
            f"Canva MCP transport failed: HTTP {status}: {safe or '(empty body)'}",
            http_status=status, exception_type="HTTPStatusError", redacted_message=safe,
        )

    def _connection_error(self, message: str, exc: BaseException) -> CanvaMCPError:
        return CanvaMCPError.from_exception(message, self._transport_error or exc)

    async def __aenter__(self) -> CanvaMCPClient:
        self._transport_error = None
        try:
            return await self._connect()
        except Exception as exc:
            raise self._connection_error("Canva MCP connection failed", exc) from None

    async def _connect(self) -> CanvaMCPClient:
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client

            self._sdk_v2 = True
            self._http_client = httpx2.AsyncClient(
                timeout=self._timeout, headers={"Authorization": f"Bearer {self._token}"},
                event_hooks={"response": [self._capture_response]},
            )
            self._transport_context = streamable_http_client(self._mcp_url, http_client=self._http_client)
            # Client owns and enters the transport context; entering it separately breaks
            # AnyIO's task-group lifetime on Windows.
            # Use Canva's supported initialize handshake directly rather than
            # sending the SDK v2 server/discover probe first.
            self._session_context = Client(
                self._transport_context, mode="legacy", read_timeout_seconds=self._timeout,
            )
            self._transport_owned_by_session = True
            self._session = await self._session_context.__aenter__()
        except ImportError as exc:
            try:
                from mcp.client.session import ClientSession
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError:
                raise CanvaMCPError("The official Python MCP SDK is unavailable in this runtime.") from exc

            def http_client_factory(**kwargs):
                import httpx

                hooks = kwargs.setdefault("event_hooks", {})
                hooks.setdefault("response", []).append(self._capture_response)
                return httpx.AsyncClient(**kwargs)

            self._transport_context = streamablehttp_client(
                self._mcp_url, headers={"Authorization": f"Bearer {self._token}"}, timeout=self._timeout,
                httpx_client_factory=http_client_factory,
            )
            read_stream, write_stream, _ = await self._transport_context.__aenter__()
            self._session_context = ClientSession(read_stream, write_stream)
            self._session = await self._session_context.__aenter__()
            await self._session.initialize()
        listed = await self._session.list_tools()
        self._tools = {str(tool.name).replace("_", "-"): tool for tool in listed.tools
                       if str(tool.name).replace("_", "-") in ALLOWED_TOOLS}
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(exc_type, exc, tb)
        if self._transport_context is not None and not self._transport_owned_by_session:
            await self._transport_context.__aexit__(exc_type, exc, tb)
        if self._http_client is not None:
            await self._http_client.aclose()
        self._token = ""
        self._diagnostic_secrets = ()

    @property
    def tools(self) -> dict[str, Any]:
        return dict(self._tools)

    def schema(self, name: str) -> dict[str, Any]:
        tool = self._tools.get(safe_tool_name(name))
        if tool is None:
            raise CanvaMCPError(f"Canva MCP did not list the required tool: {name}")
        return json_value(getattr(tool, "inputSchema", getattr(tool, "input_schema", {}))) or {}

    async def call(self, name: str, arguments: dict[str, Any], *, reservation_made: bool = False) -> dict[str, Any]:
        name = safe_tool_name(name)
        tool = self._tools.get(name)
        if tool is None:
            raise CanvaMCPError(f"Canva MCP did not list the requested tool: {name}")
        schema = self.schema(name)
        properties = schema.get("properties", {})
        unknown = set(arguments) - set(properties)
        required = set(schema.get("required", []))
        missing = required - set(arguments)
        if unknown or missing:
            raise CanvaMCPError(
                f"Canva MCP arguments do not match {name} schema "
                f"(unknown={sorted(unknown)}, missing={sorted(missing)})."
            )
        if name == "generate-design":
            if self._db is None:
                raise CanvaMCPError("A database session is required to enforce the Canva daily generation cap.")
            if not reservation_made:
                reserve_daily_generation(self._db, limit=self._max_daily_generations)
            await _limiter.acquire("generate-design")
        elif name in {"create-upload-url", "upload-asset-from-url"}:
            await _limiter.acquire("upload")
        elif name == "generate-image":
            await _limiter.acquire("generate-image")
        self._transport_error = None
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as exc:  # SDK exceptions may contain request headers; do not stringify them.
            raise self._connection_error(f"Canva MCP {name} call failed", exc) from None
        payload = json_value(getattr(result, "structuredContent", None)
                             or getattr(result, "structured_content", None))
        if payload is None:
            texts = [getattr(block, "text", "") for block in (getattr(result, "content", None) or [])]
            raw = "\n".join(text for text in texts if text)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"text": raw}
        if getattr(result, "isError", getattr(result, "is_error", False)):
            message = payload.get("text", payload) if isinstance(payload, dict) else payload
            raise CanvaMCPError(f"Canva MCP {name} failed: {_redact_error(message)}")
        return payload if isinstance(payload, dict) else {"result": payload}

    async def poll_design_job(self, payload: dict[str, Any], *, max_seconds: float = 120.0) -> dict[str, Any]:
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        job_id = job.get("job_id") or job.get("jobId") or job.get("id") or _nested(payload, "job_id", "jobId")
        if not isinstance(job_id, str):
            return payload
        if _candidate_rows(payload):
            return {**payload, "job_id": job_id}
        start = time.monotonic()
        continuation = _nested(payload, "continuation_token", "continuationToken")
        while time.monotonic() - start < max_seconds:
            await asyncio.sleep(3)
            name = "get-design-candidates" if "get-design-candidates" in self._tools else "get-create-design-async-job"
            schema = self.schema(name).get("properties", {})
            args: dict[str, Any] = {"job_id": job_id}
            if "continuation_token" in schema and isinstance(continuation, str):
                args["continuation_token"] = continuation
            if "user_intent" in schema:
                args["user_intent"] = "Check the result of the requested private Instagram design preview."
            payload = await self.call(name, args)
            continuation = _nested(payload, "continuation_token", "continuationToken") or continuation
            if _candidate_rows(payload):
                # Candidate-list responses can omit the generation job id, but
                # create-design-from-candidate still requires the original id.
                return {**payload, "job_id": job_id}
            state = str(_nested(payload, "status", "state", "job_status") or "").lower()
            if state in {"failed", "error", "cancelled", "canceled"}:
                raise CanvaMCPError(f"Canva design generation job ended with status {state}.")
        raise CanvaMCPError("Canva design generation job exceeded the 120-second polling limit.")


def _nested(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) is not None:
                return value[key]
        for child in value.values():
            found = _nested(child, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested(child, *keys)
            if found is not None:
                return found
    return None


def _candidate_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    body = payload.get("job") if isinstance(payload.get("job"), dict) else payload
    body = body.get("result") if isinstance(body.get("result"), dict) else body
    rows = body.get("generated_designs", []) if isinstance(body, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def generate_design_args(tool: Any, *, query: str, user_intent: str, asset_ids: list[str] | None = None) -> dict[str, Any]:
    schema = (tool if isinstance(tool, dict) else
              json_value(getattr(tool, "inputSchema", getattr(tool, "input_schema", {})))) or {}
    if "inputSchema" in schema:
        schema = schema["inputSchema"]
    props = schema.get("properties", {})
    args: dict[str, Any] = {}
    if "query" not in props or "design_type" not in props:
        raise CanvaMCPError("generate-design schema lacks query or design_type; no arguments were guessed.")
    args["query"] = query
    args["design_type"] = "instagram_post"
    if "user_intent" in props:
        args["user_intent"] = user_intent[:255]
    if asset_ids and "asset_ids" in props:
        args["asset_ids"] = list(asset_ids)
    missing = set(schema.get("required", [])) - set(args)
    if missing:
        raise CanvaMCPError(f"generate-design has unmapped required schema fields: {sorted(missing)}")
    return args

