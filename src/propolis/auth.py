"""Signing in to GitHub with OAuth device flow.

This needs a GitHub OAuth App: GitHub App tokens and fine-grained personal
access tokens can't read notifications. Device flow needs only the app's public
client ID, and no secret. The app must have "Expire user access tokens" turned
off, because refreshing short-lived tokens needs the secret.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import httpx

#: The client ID of Propolis's OAuth App, which has Device Flow enabled.
CLIENT_ID = "Ov23ligJYqr0ZNQGPQhL"
#: Reading notifications, marking them read or done, and managing
#: subscriptions. Public repositories' timelines and checks need no scope.
SCOPES = "notifications"
VERIFICATION_URL = "https://github.com/login/device"


class SignInError(Exception):
    """Signing in didn't work, in words for the user."""


class CodeExpired(SignInError):
    """The code wasn't entered on GitHub in time."""


@dataclass(frozen=True)
class DeviceCode:
    #: What you type in at ``verification_uri``.
    user_code: str
    verification_uri: str
    device_code: str
    expires_in: int
    interval: int


async def sign_in(
    show_code: Callable[[DeviceCode], None],
    *,
    client_id: str = CLIENT_ID,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Sign in to GitHub, and return the token.

    ``show_code`` is called with the code to show. This then waits until you've
    approved the app on GitHub, or the code expires.
    """
    async with httpx.AsyncClient(
        base_url="https://github.com",
        headers={"Accept": "application/json"},
        transport=transport,
        timeout=30,
    ) as http:
        try:
            response = await http.post(
                "/login/device/code", data={"client_id": client_id, "scope": SCOPES}
            )
            response.raise_for_status()
            data = response.json()
            code = DeviceCode(
                user_code=data["user_code"],
                verification_uri=data["verification_uri"],
                device_code=data["device_code"],
                expires_in=data["expires_in"],
                interval=data["interval"],
            )
            show_code(code)

            interval = code.interval
            loop = asyncio.get_running_loop()
            deadline = loop.time() + code.expires_in
            while loop.time() < deadline:
                await asyncio.sleep(interval)
                response = await http.post(
                    "/login/oauth/access_token",
                    data={
                        "client_id": client_id,
                        "device_code": code.device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
                response.raise_for_status()
                result = response.json()
                if token := result.get("access_token"):
                    return token
                match result.get("error"):
                    case "authorization_pending":
                        pass
                    case "slow_down":
                        interval = result.get("interval", interval + 5)
                    case "expired_token":
                        raise CodeExpired("The code expired.")
                    case error:
                        raise SignInError(result.get("error_description") or error)
        except httpx.HTTPError as exc:
            raise SignInError(f"Couldn't reach GitHub ({exc}).") from exc
    raise CodeExpired("The code expired.")
