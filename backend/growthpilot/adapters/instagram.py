"""Instagram Graph API adapter using Instagram Login."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

from ..config import get_settings
from ..models import Platform
from ..research.net import client
from .base import (
    AccountInfo,
    AdapterError,
    Capabilities,
    ConnectionContext,
    Metrics,
    PublishPayload,
    PublishResult,
    StatusResult,
)

_sleep = time.sleep

AUTH_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"  # noqa: S105
GRAPH = "https://graph.instagram.com"
FACEBOOK_GRAPH = "https://graph.facebook.com"
DIALOG_URL = "https://www.facebook.com/dialog/oauth"

SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_insights",
]

INSIGHT_METRICS = [
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "views",
]

def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _raise(resp, operation: str) -> None:
    if resp.is_success:
        return

    message = ""
    code = None

    try:
        error = resp.json().get("error", {})
        message = str(
            error.get("message", error)
        )[:300]
        code = error.get("code")
    except Exception:
        message = resp.text[:200]

    reauth = resp.status_code == 401 or code == 190
    retryable = (
        resp.status_code == 429
        or resp.status_code >= 500
        or code in (1, 2, 4, 17, 32, 341, 613)
    )

    raise AdapterError(
        f"Instagram {operation} failed "
        f"(HTTP {resp.status_code}): {message}",
        retryable=retryable,
        reauth=reauth,
    )


class InstagramAdapter:
    key = "instagram"
    platform = Platform.INSTAGRAM.value
    label = "Instagram"
    external = True
    maturity = "implemented-unverified"

    capabilities = Capabilities(
        oauth=True,
        identity=True,
        publish=True,
        scheduling="app-managed",
        status_verification=True,
        analytics=True,
        content_types=("post", "carousel"),
    )

    @property
    def version(self) -> str:
        return get_settings().instagram_api_version

    def authorize_url(self, state: str) -> str:
        """Use direct Instagram Login when available, otherwise this app's supported Unified Login."""
        settings = get_settings()
        if not settings.instagram_login_configured:
            params = {
                "client_id": settings.meta_app_id,
                "redirect_uri": settings.instagram_redirect_uri,
                "state": state,
                "config_id": settings.meta_login_config_id,
                "response_type": "code",
                "override_default_response_type": "true",
                "display": "popup",
                "prompt": "login",
            }
            return f"{DIALOG_URL}?{urlencode(params)}"

        params = {
            "client_id": settings.instagram_app_id,
            "redirect_uri": settings.instagram_redirect_uri,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": state,
            "enable_fb_login": "0",
        }

        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange Instagram's authorization code for tokens."""
        settings = get_settings()
        if not settings.instagram_login_configured:
            return self._exchange_unified_code(code, settings)

        # Exchange the authorization code for a short-lived token.
        with client() as http:
            response = http.post(
                TOKEN_URL,
                data={
                    "client_id": settings.instagram_app_id,
                    "client_secret": settings.instagram_app_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.instagram_redirect_uri,
                    "code": code,
                },
            )

            _raise(response, "authorization-code exchange")
            short_data = response.json()

            # Exchange the short-lived token for a long-lived token.
            if isinstance(short_data.get("data"), list):
                rows = short_data["data"]
                if len(rows) != 1:
                    raise AdapterError("Instagram returned an ambiguous token response.")
                short_data = rows[0]

            short_token = short_data.get("access_token")
            user_id = short_data.get("user_id")
            if not short_token or not user_id:
                raise AdapterError("Instagram did not return the expected access token and user ID.")

            long_response = http.get(
                f"{GRAPH}/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": settings.instagram_app_secret,
                    "access_token": short_token,
                },
            )

            _raise(long_response, "long-lived token exchange")
            long_data = long_response.json()

            access_token = (
                long_data.get("access_token") or short_token
            )

            expires_in = int(
                long_data.get("expires_in")
                or short_data.get("expires_in")
                or 0
            )

            # Retrieve the connected Instagram professional account.
            profile_response = http.get(
                f"{GRAPH}/{self.version}/me",
                params={
                    "fields": (
                        "user_id,id,username,account_type,"
                        "media_count"
                    ),
                },
                headers=_bearer(access_token),
            )

            _raise(profile_response, "profile lookup")
            profile = profile_response.json()

        account_id = str(
            profile.get("user_id")
            or profile.get("id")
            or user_id
        )

        now = int(time.time())

        return {
            "access_token": access_token,
            "user_id": account_id,
            "issued_at": now,
            "expires_at": (
                now + expires_in if expires_in else 0
            ),
            "instagram_username": str(
                profile.get("username") or ""
            ),
            "instagram_account_type": str(
                profile.get("account_type") or ""
            ),
            "login_type": "instagram_login",
        }

    def _exchange_unified_code(self, code: str, settings) -> dict[str, Any]:
        """Exchange Unified Login code and discover Instagram assets shared by the business."""
        token_url = f"{FACEBOOK_GRAPH}/{self.version}/oauth/access_token"
        with client() as http:
            response = http.get(token_url, params={
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "redirect_uri": settings.instagram_redirect_uri,
                "code": code,
            })
            _raise(response, "authorization-code exchange")
            token_data = response.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise AdapterError("Meta did not return an access token.")

            response = http.get(
                f"{FACEBOOK_GRAPH}/{self.version}/me",
                params={"fields": "client_business_id"},
                headers=_bearer(access_token),
            )
            _raise(response, "business lookup")
            business_id = str(response.json().get("client_business_id") or "")

            headers = _bearer(access_token)
            response = http.get(
                f"{FACEBOOK_GRAPH}/{self.version}/me/assigned_instagram_accounts",
                params={"fields": "id,username", "limit": 100},
                headers=headers,
            )
            _raise(response, "Instagram account discovery")
            accounts = response.json().get("data", [])

            # Some Facebook Login for Business configurations grant the assets on
            # the business edge rather than the user edge. Check both asset sets.
            if not accounts and business_id:
                for edge in ("owned_instagram_accounts", "client_instagram_accounts"):
                    asset_response = http.get(
                        f"{FACEBOOK_GRAPH}/{self.version}/{business_id}/{edge}",
                        params={"fields": "id,username", "limit": 100},
                        headers=headers,
                    )
                    if asset_response.is_success:
                        accounts = asset_response.json().get("data", [])
                    if accounts:
                        break

            if not accounts:
                permission_response = http.get(
                    f"{FACEBOOK_GRAPH}/{self.version}/me/permissions", headers=headers
                )
                granted = []
                if permission_response.is_success:
                    granted = [
                        str(item.get("permission"))
                        for item in permission_response.json().get("data", [])
                        if item.get("status") == "granted"
                    ]

        if not accounts:
            missing = sorted({"instagram_basic", "business_management"} - set(granted))
            hint = (" Meta did not grant: " + ", ".join(missing) + "." if missing else
                    " Meta reports the required permissions as granted, but no Instagram asset is assigned to this login.")
            raise AdapterError(
                "Meta did not share an Instagram account with GrowthPilot." + hint +
                " In the Facebook login prompt, choose the GrowthPilot business portfolio and select the "
                "growth.pilotai Instagram account."
            )
        chosen = accounts[0]
        account_id = str(chosen.get("id") or "")
        if not account_id:
            raise AdapterError("Meta shared an Instagram account without an account ID.")
        now = int(time.time())
        expires_in = int(token_data.get("expires_in") or 0)
        return {
            "access_token": access_token,
            "user_id": account_id,
            "business_id": business_id,
            "issued_at": now,
            "expires_at": now + expires_in if expires_in else 0,
            "instagram_username": chosen.get("username", ""),
            "accounts": [{"id": str(account.get("id") or ""), "username": account.get("username", "")}
                         for account in accounts],
            "login_type": "unified_login",
        }

    @staticmethod
    def _graph(ctx: ConnectionContext) -> str:
        return FACEBOOK_GRAPH if ctx.credentials.get("login_type") in {"facebook_unified", "unified_login"} else GRAPH

    def _token(self, ctx: ConnectionContext) -> str:
        credentials = ctx.credentials
        token = credentials.get("access_token")

        if not token:
            raise AdapterError(
                "Instagram is not connected. Reconnect the account.",
                reauth=True,
            )

        expires_at = int(
            credentials.get("expires_at") or 0
        )

        if expires_at and expires_at <= time.time():
            raise AdapterError(
                "Instagram authorization has expired. Reconnect.",
                reauth=True,
            )

        issued_at = int(credentials.get("issued_at") or 0)
        if (credentials.get("login_type") != "unified_login" and expires_at
                and expires_at - time.time() < 7 * 86400 and time.time() - issued_at > 86400):
            with client() as http:
                response = http.get(
                    f"{GRAPH}/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": token},
                )
            _raise(response, "token refresh")
            data = response.json()
            refreshed = dict(
                credentials,
                access_token=data["access_token"],
                issued_at=int(time.time()),
                expires_at=int(time.time()) + int(data.get("expires_in", 0)),
            )
            ctx.credentials = refreshed
            ctx.save_credentials(refreshed)
            token = refreshed["access_token"]

        return token

    def _get(
        self,
        ctx: ConnectionContext,
        path: str,
        operation: str,
        **params,
    ) -> dict:
        with client() as http:
            response = http.get(
                f"{self._graph(ctx)}/{self.version}/{path}",
                params=params,
                headers=_bearer(self._token(ctx)),
            )

        _raise(response, operation)
        return response.json()

    def _post(
        self,
        ctx: ConnectionContext,
        path: str,
        operation: str,
        **data,
    ) -> dict:
        with client() as http:
            response = http.post(
                f"{self._graph(ctx)}/{self.version}/{path}",
                data=data,
                headers=_bearer(self._token(ctx)),
            )

        _raise(response, operation)
        return response.json()

    def verify_connection(
        self,
        ctx: ConnectionContext,
    ) -> AccountInfo:
        unified_login = ctx.credentials.get("login_type") == "unified_login"
        account = self._get(
            ctx,
            ctx.account_id if unified_login else "me",
            "account lookup",
            fields="id,username" if unified_login else "user_id,id,username,account_type,media_count",
        )

        account_id = str(
            account.get("user_id")
            or account.get("id")
            or ctx.account_id
            or ctx.credentials.get("user_id")
            or ""
        )

        if not account_id:
            raise AdapterError(
                "Instagram did not return an account ID."
            )

        username = account.get("username") or ""
        account_type = str(
            account.get("account_type") or ""
        ).upper()

        if not unified_login and account_type == "PERSONAL":
            raise AdapterError("Instagram requires a professional account.")

        return AccountInfo(
            account_id=account_id,
            label=f"@{username}" if username else "Instagram account",
            detail={
                "account_type": account_type,
            },
        )

    def _wait_ready(
        self,
        ctx: ConnectionContext,
        container_id: str,
    ) -> None:
        for _ in range(6):
            result = self._get(
                ctx,
                container_id,
                "media container status",
                fields="status_code",
            )

            status = result.get("status_code")

            if status in ("FINISHED", "PUBLISHED"):
                return

            if status in ("ERROR", "EXPIRED"):
                raise AdapterError(
                    f"Instagram media processing failed: {status}."
                )

            _sleep(5)

        raise AdapterError(
            "Instagram is still processing the media. Retry later.",
            retryable=True,
        )

    def publish(
        self,
        ctx: ConnectionContext,
        payload: PublishPayload,
    ) -> PublishResult:
        instagram_id = (
            ctx.account_id
            or str(ctx.credentials.get("user_id") or "")
        )

        if not instagram_id:
            raise AdapterError(
                "Instagram account ID is missing. Reconnect the account."
            )

        if not payload.image_urls:
            raise AdapterError(
                "An Instagram post requires an image."
            )

        for image_url in payload.image_urls:
            if not image_url.startswith("https://"):
                raise AdapterError(
                    "Instagram requires a public HTTPS image URL that Meta can access."
                )

        limit = self._get(
            ctx,
            f"{instagram_id}/content_publishing_limit",
            "publishing limit check",
            fields="quota_usage,config",
        )
        rows = limit.get("data") or [{}]
        usage = rows[0].get("quota_usage")
        total = (rows[0].get("config") or {}).get("quota_total")
        if usage is not None and total is not None and usage >= total:
            raise AdapterError(
                f"Instagram's 24-hour publishing limit is used up ({usage}/{total}).",
                retryable=True,
            )

        if (
            payload.content_type == "carousel"
            and len(payload.image_urls) > 1
        ):
            children = []

            for image_url in payload.image_urls[:10]:
                child = self._post(
                    ctx,
                    f"{instagram_id}/media",
                    "carousel item creation",
                    image_url=image_url,
                    is_carousel_item="true",
                )

                children.append(child["id"])

            for child_id in children:
                self._wait_ready(ctx, child_id)

            container = self._post(
                ctx,
                f"{instagram_id}/media",
                "carousel creation",
                media_type="CAROUSEL",
                children=",".join(children),
                caption=payload.caption,
            )
        else:
            container = self._post(
                ctx,
                f"{instagram_id}/media",
                "media creation",
                image_url=payload.image_urls[0],
                caption=payload.caption,
            )

        container_id = container["id"]
        self._wait_ready(ctx, container_id)

        published = self._post(
            ctx,
            f"{instagram_id}/media_publish",
            "publishing",
            creation_id=container_id,
        )

        media_id = str(published["id"])

        return PublishResult(
            external_id=media_id,
            url=None,
            provider_status="PUBLISH_ACCEPTED",
            summary=f"Instagram accepted media {media_id}.",
        )

    def publication_status(
        self,
        ctx: ConnectionContext,
        external_id: str,
    ) -> StatusResult:
        media = self._get(
            ctx,
            external_id,
            "publication lookup",
            fields="id,permalink,timestamp,media_type",
        )

        if media.get("id"):
            return StatusResult(
                state="published",
                url=media.get("permalink"),
                detail=(
                    f"Instagram media {media['id']} was found."
                ),
            )

        return StatusResult(
            state="unknown",
            detail="Instagram did not return a media ID.",
        )

    def fetch_metrics(
        self,
        ctx: ConnectionContext,
        external_id: str,
        *,
        hours_since: float,
        hints: dict,
    ) -> Metrics | None:
        result = self._get(
            ctx,
            f"{external_id}/insights",
            "insights lookup",
            metric=",".join(INSIGHT_METRICS),
        )

        values = {}

        for row in result.get("data", []):
            metric_values = row.get("values") or []

            if metric_values:
                value = metric_values[0].get("value")
            else:
                value = (
                    row.get("total_value") or {}
                ).get("value")

            if isinstance(value, (int, float)):
                values[row["name"]] = int(value)

        return Metrics(
            reach=values.get("reach"),
            views=values.get("views"),
            likes=values.get("likes"),
            comments=values.get("comments"),
            shares=values.get("shares"),
            saves=values.get("saved"),
            raw={"insights": values},
        )
