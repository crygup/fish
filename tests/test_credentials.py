import pytest
from cryptography.fernet import Fernet

from utils.credentials import PREFIX, decrypt_credential, encrypt_credential


def test_credentials_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FISHIE_CREDENTIAL_KEY", Fernet.generate_key().decode())
    encrypted = encrypt_credential("top-secret")
    assert encrypted.startswith(PREFIX)
    assert "top-secret" not in encrypted
    assert decrypt_credential(encrypted) == "top-secret"
    assert encrypt_credential(encrypted) == encrypted


def test_encrypt_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FISHIE_CREDENTIAL_KEY", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        encrypt_credential("secret")


def test_legacy_plaintext_can_be_read_during_migration() -> None:
    assert decrypt_credential("legacy") == "legacy"
