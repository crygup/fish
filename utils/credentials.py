from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "fernet:v1:"
KEY_ENV = "FISHIE_CREDENTIAL_KEY"
KEY_FILE_ENV = "FISHIE_CREDENTIAL_KEY_FILE"


def _credential_key() -> str:
    if key := os.environ.get(KEY_ENV):
        return key
    if key_file := os.environ.get(KEY_FILE_ENV):
        try:
            key = Path(key_file).read_text(encoding="ascii").strip()
        except OSError as error:
            raise RuntimeError(
                f"Could not read the credential key file from {KEY_FILE_ENV}"
            ) from error
        if key:
            return key
    raise RuntimeError(
        f"{KEY_ENV} or {KEY_FILE_ENV} is required to store or use OAuth credentials."
    )


def _fernet() -> Fernet:
    key = _credential_key()
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("The configured credential key is not a valid Fernet key") from error


def validate_credential_key() -> None:
    _fernet()


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
