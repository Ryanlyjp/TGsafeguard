import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import RuntimeSetting, TempMailMessage
from app.services import login_email_protection
from app.services.login_email_protection import (
    DOMAIN_BACKENDS_KEY,
    DOMAINS_KEY,
    RoutedCodeReader,
    SelfHostedTempMailCodeReader,
    TempMailCodeReader,
    get_domain_backends,
    set_domain_backend,
)
from app.services.temp_mail import (
    ALLOWED_TEMP_MAIL_DOMAINS,
    _received_at,
    normalize_recipient,
    parse_allowed_recipients,
)


def test_all_configured_domains_accept_arbitrary_local_parts() -> None:
    for domain in ALLOWED_TEMP_MAIL_DOMAINS:
        address = f"arbitrary-123_{len(domain)}@{domain}"
        assert normalize_recipient(address) == (address, domain)


def test_recipient_parser_keeps_only_allowed_domains() -> None:
    assert parse_allowed_recipients(
        "Alice <ABC@cf-mail.example.com>, attacker@example.com",
        {"cf-mail.example.com"},
    ) == (("abc@cf-mail.example.com", "cf-mail.example.com"),)


def test_recipient_parser_can_restrict_webhook_to_cloudflare_domains() -> None:
    assert parse_allowed_recipients(
        "cf@cf-mail.example.com, gmail@gmail-mail.example.net",
        {"cf-mail.example.com"},
    ) == (("cf@cf-mail.example.com", "cf-mail.example.com"),)


def test_invalid_recipient_is_rejected_for_queries() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        normalize_recipient("abc@example.com")


def test_temp_mail_reader_extracts_matching_telegram_code(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.login_email_sender", "noreply@telegram.org")
    message = TempMailMessage(
        id="cloudflare-message-1",
        recipient="abc@cf-mail.example.com",
        sender="Telegram <noreply@telegram.org>",
        domain="cf-mail.example.com",
        subject="Your Code - 853353",
        parsed_text="Your code is: 853353. Use it to verify your email for Login.",
        received_at=datetime.now(UTC),
    )
    assert TempMailCodeReader._extract_code(message, "abc@cf-mail.example.com") == "853353"
    assert TempMailCodeReader._extract_code(message, "other@cf-mail.example.com") is None


def test_routed_reader_selects_backend_from_target_domain(monkeypatch) -> None:
    class Sessionmaker:
        def __call__(self):
            class Context:
                async def __aenter__(self):
                    return SimpleNamespace()

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            return Context()

    reader = RoutedCodeReader(Sessionmaker())
    cloudflare_reader = SimpleNamespace(wait_for_code=AsyncMock(return_value="111111"))
    gmail_reader = SimpleNamespace(wait_for_code=AsyncMock(return_value="222222"))
    selfhosted_reader = SimpleNamespace(wait_for_code=AsyncMock(return_value="333333"))
    reader.readers = {
        "cloudflare": cloudflare_reader,
        "gmail": gmail_reader,
        "selfhosted": selfhosted_reader,
    }
    selected_backend = AsyncMock(side_effect=["cloudflare", "gmail", "selfhosted"])
    monkeypatch.setattr(login_email_protection, "get_domain_backend", selected_backend)
    requested_at = datetime.now(UTC)

    async def exercise() -> None:
        assert (
            await reader.wait_for_code("a@cf-mail.example.com", requested_at, 30)
            == "111111"
        )
        assert (
            await reader.wait_for_code("b@gmail-mail.example.net", requested_at, 30)
            == "222222"
        )
        assert (
            await reader.wait_for_code("c@selfhosted-mail.example.net", requested_at, 30)
            == "333333"
        )

    asyncio.run(exercise())
    cloudflare_reader.wait_for_code.assert_awaited_once_with(
        "a@cf-mail.example.com", requested_at, 30
    )
    gmail_reader.wait_for_code.assert_awaited_once_with(
        "b@gmail-mail.example.net", requested_at, 30
    )
    selfhosted_reader.wait_for_code.assert_awaited_once_with(
        "c@selfhosted-mail.example.net", requested_at, 30
    )


class FakeHTTPResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, content_type=None):
        return self.payload

    async def text(self):
        return str(self.payload)


def test_selfhosted_reader_precreates_exact_mailbox(monkeypatch) -> None:
    requests: list[tuple[str, str, object]] = []

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def get(self, url, **kwargs):
            requests.append(("GET", url, kwargs.get("params")))
            return FakeHTTPResponse(404, {"error": "mailbox not found"})

        def post(self, url, **kwargs):
            requests.append(("POST", url, kwargs.get("json")))
            return FakeHTTPResponse(
                201,
                {"mailbox": {"id": "mailbox-1", "full_address": "alias@mail.example.com"}},
            )

    monkeypatch.setattr("app.services.login_email_protection.aiohttp.ClientSession", Session)
    monkeypatch.setattr(
        "app.config.settings.login_email_selfhosted_tempmail_url",
        "https://tempmail.example",
    )
    monkeypatch.setattr(
        "app.config.settings.login_email_selfhosted_tempmail_api_key",
        "tm_test_key",
    )

    asyncio.run(SelfHostedTempMailCodeReader(None).prepare_mailbox("alias@mail.example.com"))

    assert requests[-1] == (
        "POST",
        "https://tempmail.example/api/mailboxes",
        {
            "address": "alias",
            "domain": "mail.example.com",
            "subdomain_mode": "off",
            "source": "tgmag",
        },
    )


def test_selfhosted_reader_skips_old_mail_and_reads_matching_login_code(
    monkeypatch, caplog
) -> None:
    requested_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    detail_requests: list[str] = []

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def get(self, url, **kwargs):
            if url.endswith("/api/mailboxes/lookup"):
                return FakeHTTPResponse(
                    200,
                    {"mailbox": {"id": "mailbox-1", "full_address": "alias@mail.example.com"}},
                )
            if url.endswith("/emails"):
                return FakeHTTPResponse(
                    200,
                    {
                        "data": [
                            {"id": "new-mail", "received_at": "2026-08-16T12:00:02Z"},
                            {"id": "old-mail", "received_at": "2026-08-16T11:59:59Z"},
                        ]
                    },
                )
            detail_requests.append(url)
            return FakeHTTPResponse(
                200,
                {
                    "email": {
                        "sender": "Telegram <noreply@telegram.org>",
                        "subject": "Your Code - 853353",
                        "body_text": (
                            "Your code is: 853353. Use it to verify your email for Login."
                        ),
                    }
                },
            )

    monkeypatch.setattr("app.services.login_email_protection.aiohttp.ClientSession", Session)
    monkeypatch.setattr(
        "app.config.settings.login_email_selfhosted_tempmail_url",
        "https://tempmail.example",
    )
    monkeypatch.setattr(
        "app.config.settings.login_email_selfhosted_tempmail_api_key",
        "tm_test_key",
    )

    reader = SelfHostedTempMailCodeReader(None)
    reader.stored_reader.find_code = AsyncMock(return_value=None)
    with caplog.at_level(logging.INFO, logger=login_email_protection.__name__):
        code = asyncio.run(
            reader.wait_for_code(
                "alias@mail.example.com",
                requested_at,
                1,
            )
        )

    assert code == "853353"
    assert "source=polling domain=mail.example.com" in caplog.text
    assert "alias@mail.example.com" not in caplog.text
    assert "853353" not in caplog.text
    assert detail_requests == [
        "https://tempmail.example/api/mailboxes/mailbox-1/emails/new-mail"
    ]


def test_selfhosted_reader_prefers_webhook_message_before_api_lookup(
    monkeypatch, caplog
) -> None:
    reader = SelfHostedTempMailCodeReader(None)
    reader.stored_reader.find_code = AsyncMock(return_value="853353")
    reader._lookup_mailbox = AsyncMock()

    with caplog.at_level(logging.INFO, logger=login_email_protection.__name__):
        code = asyncio.run(
            reader.wait_for_code(
                "alias@mail.example.com",
                datetime.now(UTC),
                1,
            )
        )

    assert code == "853353"
    assert "source=webhook domain=mail.example.com" in caplog.text
    assert "alias@mail.example.com" not in caplog.text
    assert "853353" not in caplog.text
    reader._lookup_mailbox.assert_not_awaited()


def test_selfhosted_reader_keeps_waiting_for_webhook_when_api_is_down(monkeypatch) -> None:
    reader = SelfHostedTempMailCodeReader(None)
    reader.stored_reader.find_code = AsyncMock(side_effect=[None, "853353"])
    reader._poll_api_for_code = AsyncMock(
        side_effect=login_email_protection.aiohttp.ClientConnectionError("offline")
    )
    monkeypatch.setattr(
        "app.config.settings.login_email_poll_interval_seconds", 0.01
    )

    code = asyncio.run(
        reader.wait_for_code(
            "alias@mail.example.com",
            datetime.now(UTC),
            1,
        )
    )

    assert code == "853353"
    reader._poll_api_for_code.assert_awaited_once()


def test_webhook_received_at_uses_source_timestamp() -> None:
    assert _received_at("2026-08-16T12:00:02Z") == datetime(
        2026, 8, 16, 12, 0, 2, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="receivedAt"):
        _received_at("not-a-time")


def test_domain_backend_selection_is_persisted(monkeypatch) -> None:
    rows = {
        DOMAINS_KEY: RuntimeSetting(
            key=DOMAINS_KEY,
            value='["cf-mail.example.com", "gmail-mail.example.net"]',
        )
    }

    class Session:
        committed = False

        async def get(self, model, key):
            assert model is RuntimeSetting
            return rows.get(key)

        def add(self, row):
            rows[row.key] = row

        async def commit(self):
            self.committed = True

    session = Session()
    monkeypatch.setattr("app.config.settings.temp_mail_webhook_secret", "0" * 32)
    monkeypatch.setattr(
        "app.config.settings.login_email_gmail_username", "owner@gmail.com"
    )
    monkeypatch.setattr(
        "app.config.settings.login_email_gmail_app_password", "app-password"
    )

    async def exercise() -> None:
        await set_domain_backend(session, "gmail-mail.example.net", "gmail")
        routes = await get_domain_backends(session)
        assert routes == {
            "cf-mail.example.com": "cloudflare",
            "gmail-mail.example.net": "gmail",
        }

    asyncio.run(exercise())
    assert session.committed is True
    assert '"gmail-mail.example.net": "gmail"' in rows[DOMAIN_BACKENDS_KEY].value
