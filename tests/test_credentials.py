import pytest
from cryptography.fernet import Fernet

from utils.credentials import (
    PREFIX,
    decrypt_credential,
    encrypt_credential,
    validate_credential_key,
)


def test_credentials_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FISHIE_CREDENTIAL_KEY", Fernet.generate_key().decode())
    encrypted = encrypt_credential("top-secret")
    assert encrypted.startswith(PREFIX)
    assert "top-secret" not in encrypted
    assert decrypt_credential(encrypted) == "top-secret"
    assert encrypt_credential(encrypted) == encrypted


def test_encrypt_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FISHIE_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("FISHIE_CREDENTIAL_KEY_FILE", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        encrypt_credential("secret")


def test_legacy_plaintext_can_be_read_during_migration() -> None:
    assert decrypt_credential("legacy") == "legacy"


def test_credentials_can_use_a_mounted_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    key_file = tmp_path / "credential.key"
    key_file.write_bytes(Fernet.generate_key())
    monkeypatch.delenv("FISHIE_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("FISHIE_CREDENTIAL_KEY_FILE", str(key_file))

    validate_credential_key()
    encrypted = encrypt_credential("secret")
    assert decrypt_credential(encrypted) == "secret"
