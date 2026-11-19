"""Keep secrets (the GitHub token, remote control keys) in the macOS Keychain."""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

__all__ = ["ACCOUNT", "KeyringError", "delete_secret", "load_secret", "save_secret"]

#: The Keychain account name for the GitHub token.
ACCOUNT = "github-token"


def load_secret(service: str, name: str) -> str | None:
    return keyring.get_password(service, name)


def save_secret(service: str, name: str, value: str) -> None:
    keyring.set_password(service, name, value)


def delete_secret(service: str, name: str) -> None:
    try:
        keyring.delete_password(service, name)
    except PasswordDeleteError:
        pass  # There was nothing to delete.
