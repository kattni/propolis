"""Keep the GitHub token in the macOS Keychain."""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

__all__ = ["KeyringError", "delete_token", "load_token", "save_token"]

ACCOUNT = "github-token"


def load_token(service: str) -> str | None:
    return keyring.get_password(service, ACCOUNT)


def save_token(service: str, token: str) -> None:
    keyring.set_password(service, ACCOUNT, token)


def delete_token(service: str) -> None:
    try:
        keyring.delete_password(service, ACCOUNT)
    except PasswordDeleteError:
        pass  # There was no token to delete.
