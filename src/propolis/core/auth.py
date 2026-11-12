"""Signing in to GitHub with OAuth device flow, and keeping the token."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import httpx2
import keyring
from keyring.errors import PasswordDeleteError

from propolis.core.accounts import DEFAULT

CLIENT_ID = "Ov23ligJYqr0ZNQGPQhL"
"""Propolis's OAuth App. Device flow needs no client secret."""
SCOPES = "notifications"

_KEYRING_SERVICE = "com.kattni.propolis"


class SignInError(Exception):
    pass


@dataclass(frozen=True)
class DeviceCode:
    user_code: str
    """What you type in at ``verification_uri``."""
    verification_uri: str
    device_code: str
    expires_in: int
    interval: int


async def sign_in(show_code: Callable[[DeviceCode], None], account: str = DEFAULT) -> str:
    """Sign in to GitHub, store the account's token in the keychain, and return
    it.

    ``show_code`` is called with the code to show you. This then waits until
    you've approved Propolis on GitHub, or the code expires.
    """
    async with httpx2.AsyncClient(
        base_url="https://github.com", headers={"Accept": "application/json"}
    ) as http:
        response = await http.post(
            "/login/device/code", data={"client_id": CLIENT_ID, "scope": SCOPES}
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
                    "client_id": CLIENT_ID,
                    "device_code": code.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            response.raise_for_status()
            result = response.json()
            if token := result.get("access_token"):
                save_token(token, account)
                return token
            match result.get("error"):
                case "authorization_pending":
                    pass
                case "slow_down":
                    interval = result.get("interval", interval + 5)
                case error:
                    raise SignInError(result.get("error_description") or error)
    raise SignInError("The sign-in code expired before it was approved.")


def load_token(account: str = DEFAULT) -> str | None:
    return keyring.get_password(_KEYRING_SERVICE, _keyring_user(account))


def save_token(token: str, account: str = DEFAULT) -> None:
    keyring.set_password(_KEYRING_SERVICE, _keyring_user(account), token)


def forget_token(account: str = DEFAULT) -> None:
    try:
        keyring.delete_password(_KEYRING_SERVICE, _keyring_user(account))
    except PasswordDeleteError:
        pass


def _keyring_user(account: str) -> str:
    # The first account keeps the entry Propolis has always used.
    return "github" if account == DEFAULT else f"github:{account}"
