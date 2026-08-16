from pathlib import Path

import pytest

from app.config import Settings


def write_env(path: Path, admin_ids: str) -> Path:
    path.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "TG_API_ID=123456",
                "TG_API_HASH=test-api-hash",
                f"ADMIN_IDS={admin_ids}",
                "DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1/database",
                "FERNET_KEY=test-fernet-key",
                "LOGIN_EMAIL_ALIAS_DOMAINS=mail.example.com",
                "LOGIN_EMAIL_DOMAIN_BACKENDS=mail.example.com=cloudflare",
                "TEMP_MAIL_WEBHOOK_SECRET=0123456789abcdef0123456789abcdef",
                "LOGIN_EMAIL_GMAIL_USERNAME=test@example.com",
                "LOGIN_EMAIL_GMAIL_APP_PASSWORD=test-app-password",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("123456789,987654321", [123456789, 987654321]),
        ("[123456789, 987654321]", [123456789, 987654321]),
    ],
)
def test_admin_ids_load_from_documented_env_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
    expected: list[int],
) -> None:
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    monkeypatch.delenv("LOGIN_EMAIL_PROTECTION_ENABLED", raising=False)
    env_file = write_env(tmp_path / ".env", raw_value)
    loaded = Settings(_env_file=env_file)
    assert loaded.admin_ids == expected
    assert loaded.login_email_protection_enabled is True
    assert loaded.login_email_catchup_seconds == 600
    assert loaded.login_email_poll_timeout_seconds == 300
    assert loaded.login_email_domain_backends == {"mail.example.com": "cloudflare"}
    assert loaded.configured_login_email_backends == {"cloudflare", "gmail"}


def test_gmail_only_configuration_remains_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMP_MAIL_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("LOGIN_EMAIL_PROTECTION_ENABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "TG_API_ID=123456",
                "TG_API_HASH=test-api-hash",
                "ADMIN_IDS=123456789",
                "DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1/database",
                "FERNET_KEY=test-fernet-key",
                "LOGIN_EMAIL_ALIAS_DOMAINS=mail.example.com",
                "LOGIN_EMAIL_DOMAIN_BACKENDS=mail.example.com=gmail",
                "LOGIN_EMAIL_GMAIL_USERNAME=owner@gmail.com",
                "LOGIN_EMAIL_GMAIL_APP_PASSWORD=app-password",
            ]
        ),
        encoding="utf-8",
    )

    loaded = Settings(_env_file=env_file)

    assert loaded.default_login_email_backend == "gmail"
    assert loaded.configured_login_email_backends == {"gmail"}


def test_domain_backend_requires_matching_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMP_MAIL_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("LOGIN_EMAIL_PROTECTION_ENABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "TG_API_ID=123456",
                "TG_API_HASH=test-api-hash",
                "ADMIN_IDS=123456789",
                "DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1/database",
                "FERNET_KEY=test-fernet-key",
                "LOGIN_EMAIL_ALIAS_DOMAINS=mail.example.com",
                "LOGIN_EMAIL_DOMAIN_BACKENDS=mail.example.com=cloudflare",
                "LOGIN_EMAIL_GMAIL_USERNAME=owner@gmail.com",
                "LOGIN_EMAIL_GMAIL_APP_PASSWORD=app-password",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="TEMP_MAIL_WEBHOOK_SECRET"):
        Settings(_env_file=env_file)


def test_selfhosted_tempmail_only_configuration_is_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGIN_EMAIL_PROTECTION_ENABLED", raising=False)
    for name in (
        "TEMP_MAIL_WEBHOOK_SECRET",
        "LOGIN_EMAIL_GMAIL_USERNAME",
        "LOGIN_EMAIL_GMAIL_APP_PASSWORD",
        "LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_URL",
        "LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "TG_API_ID=123456",
                "TG_API_HASH=test-api-hash",
                "ADMIN_IDS=123456789",
                "DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1/database",
                "FERNET_KEY=test-fernet-key",
                "LOGIN_EMAIL_ALIAS_DOMAINS=mail.example.com",
                "LOGIN_EMAIL_DOMAIN_BACKENDS=mail.example.com=selfhosted",
                "LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_URL=https://mail.example.net/",
                "LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_API_KEY=tm_test_key",
            ]
        ),
        encoding="utf-8",
    )

    loaded = Settings(_env_file=env_file)

    assert loaded.login_email_selfhosted_tempmail_url == "https://mail.example.net"
    assert loaded.default_login_email_backend == "selfhosted"
    assert loaded.configured_login_email_backends == {"selfhosted"}


def test_selfhosted_tempmail_url_and_key_must_be_configured_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGIN_EMAIL_PROTECTION_ENABLED", raising=False)
    for name in (
        "TEMP_MAIL_WEBHOOK_SECRET",
        "LOGIN_EMAIL_GMAIL_USERNAME",
        "LOGIN_EMAIL_GMAIL_APP_PASSWORD",
        "LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "TG_API_ID=123456",
                "TG_API_HASH=test-api-hash",
                "ADMIN_IDS=123456789",
                "DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1/database",
                "FERNET_KEY=test-fernet-key",
                "LOGIN_EMAIL_ALIAS_DOMAINS=mail.example.com",
                "LOGIN_EMAIL_DOMAIN_BACKENDS=mail.example.com=selfhosted",
                "LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_URL=https://mail.example.net",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be configured together"):
        Settings(_env_file=env_file)


def test_selfhosted_webhook_secret_requires_32_non_whitespace_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGIN_EMAIL_SELFHOSTED_WEBHOOK_SECRET", raising=False)
    env_file = write_env(tmp_path / ".env", "123456789")
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write("\nLOGIN_EMAIL_SELFHOSTED_WEBHOOK_SECRET=too-short\n")

    with pytest.raises(ValueError, match="LOGIN_EMAIL_SELFHOSTED_WEBHOOK_SECRET"):
        Settings(_env_file=env_file)
