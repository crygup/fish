from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "fernet:v1:"
KEY_ENV = "FISHIE_CREDENTIAL_KEY"


def _fernet() -> Fernet:
    key = os.environ.get(KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{KEY_ENV} is required to store or use OAuth credentials."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError(f"{KEY_ENV} is not a valid Fernet key") from error


def encrypt_credential(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt_credential(value: str | None) -> str | None:
    if value is None or not value.startswith(PREFIX):
        # Temporary compatibility for rows created before credential encryption.
        return value
    try:
        return _fernet().decrypt(value.removeprefix(PREFIX).encode("ascii")).decode(
            "utf-8"
        )
    except InvalidToken as error:
        raise RuntimeError("Stored OAuth credential could not be decrypted") from error
