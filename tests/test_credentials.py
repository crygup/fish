import pytest
from cryptography.fernet import Fernet

from utils.credentials import (
    PREFIX,
    decrypt_credential,
    encrypt_credential,
    validate_credential_key,
)


def test_error_logs_redact_dynamic_credentials_and_tracebacks() -> None:
    import logging

    from core.bot import _redact_error_text
    from launcher import RedactingFormatter

    samples = (
        "https://discord.com/api/webhooks/123456789/TEST_SECRET/messages/123",
        "https://canary.discord.com/api/v10/webhooks/123456789/TEST_SECRET?wait=true",
        "https://discordapp.com/api/webhooks/123456789/TEST_SECRET",
        "{'Authorization': 'Bearer TEST_SECRET'}",
        '("Authorization", "Bot TEST_SECRET")',
        "Authorization: Bearer TEST_SECRET",
        '{"access_token": "TEST_SECRET", "status": 403}',
        "password='TEST_SECRET with spaces'",
        "X-API-Key=TEST_SECRET",
        "https://example.com/?key=TEST_SECRET",
    )
    formatter = RedactingFormatter("%(message)s")
    for sample in samples:
        cleaned = _redact_error_text(sample, lambda text: text)
        assert "TEST_SECRET" not in cleaned
        assert "[REDACTED]" in cleaned
        try:
            raise RuntimeError(sample)
        except RuntimeError as error:
            record = logging.LogRecord(
                "test",
                logging.ERROR,
                __file__,
                1,
                "Request failed",
                (),
                (type(error), error, error.__traceback__),
            )
            assert "TEST_SECRET" not in formatter.format(record)
    assert _redact_error_text("HTTP 403: Missing Permissions", str) == (
        "HTTP 403: Missing Permissions"
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
