from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.services.login_email_protection import parse_login_email_window_hours
from app.services.pagination import require_account_capacity
from app.webapp.server import (
    account_pagination,
    create_webhook_app,
    create_webapp,
    qr_login_payload,
    reusable_phone_login,
    start_webapp,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0), (24, 24), ("8", 8), (720, 720)],
)
def test_parse_login_email_window_hours(raw: object, expected: int) -> None:
    assert parse_login_email_window_hours(raw) == expected


@pytest.mark.parametrize("raw", [None, True, -1, 721, 1.5, "abc"])
def test_parse_login_email_window_hours_rejects_invalid_values(raw: object) -> None:
    with pytest.raises(ValueError, match="0–720"):
        parse_login_email_window_hours(raw)


def test_qr_login_payload_contains_local_png_and_expiry() -> None:
    expires = datetime.now(UTC) + timedelta(seconds=30)
    payload = qr_login_payload(
        SimpleNamespace(url="tg://login?token=test-token", expires=expires)
    )

    prefix = "data:image/png;base64,"
    assert payload["qr_image"].startswith(prefix)
    assert base64.b64decode(payload["qr_image"][len(prefix) :]).startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    assert payload["expires_at"] == expires.isoformat()


def test_recent_phone_login_is_reused_without_requesting_another_code() -> None:
    app = {
        "pending_logins": {
            "7:recent": {
                "method": "phone",
                "phone": "+447700900123",
                "created_at": time.time() - 10,
                "delivery": {"timeout": 60, "label": "Telegram App 内验证码"},
            },
            "7:expired": {
                "method": "phone",
                "phone": "+441234567890",
                "created_at": time.time() - 120,
                "delivery": {"timeout": 60},
            },
        }
    }

    reusable = reusable_phone_login(app, 7, "+447700900123")
    assert reusable is not None and reusable[0] == "recent"
    assert reusable_phone_login(app, 7, "+441234567890") is None
    assert reusable_phone_login(app, 8, "+447700900123") is None


@pytest.mark.parametrize(
    ("total", "requested", "expected"),
    [
        (0, 1, (1, 0, 0)),
        (10, 1, (1, 1, 0)),
        (11, 2, (2, 2, 10)),
        (25, 99, (3, 3, 20)),
    ],
)
def test_account_pagination_uses_ten_accounts_per_page(
    total: int,
    requested: int,
    expected: tuple[int, int, int],
) -> None:
    assert account_pagination(total, requested) == expected


def test_system_account_limit_is_fifty() -> None:
    require_account_capacity(49)
    with pytest.raises(ValueError, match="50 个"):
        require_account_capacity(50)


def test_mini_app_registers_account_delete_route() -> None:
    app = asyncio.run(create_webapp(None, None))
    delete_routes = [
        route
        for route in app.router.routes()
        if route.method == "DELETE" and route.handler.__name__ == "api_account_delete"
    ]

    assert len(delete_routes) == 1


def test_selfhosted_webhook_verifies_hmac_and_routes_exact_domain(monkeypatch) -> None:
    secret = "s" * 32
    timestamp = str(int(time.time()))
    delivery_id = str(uuid.uuid4())
    payload = {
        "id": "mail-1",
        "to": "alias@mail.example.com",
        "from": "noreply@telegram.org",
        "subject": "Your Code - 853353",
        "parsedText": "Your code is 853353 for Login",
        "receivedAt": "2026-08-16T12:00:02Z",
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    stored = AsyncMock(return_value=1)
    notified = Mock()

    class Sessionmaker:
        def __call__(self):
            class Context:
                async def __aenter__(self):
                    return SimpleNamespace()

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            return Context()

    monkeypatch.setattr(
        "app.config.settings.login_email_selfhosted_webhook_secret", secret
    )
    monkeypatch.setattr(
        "app.webapp.server.get_domain_backends",
        AsyncMock(return_value={"mail.example.com": "selfhosted"}),
    )
    monkeypatch.setattr("app.webapp.server.store_temp_mail_payload", stored)
    monkeypatch.setattr("app.webapp.server.notify_temp_mail_recipient", notified)

    async def exercise() -> None:
        client = TestClient(TestServer(create_webhook_app(Sessionmaker())))
        await client.start_server()
        try:
            response = await client.post(
                "/webhooks/selfhosted-tempmail",
                data=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-TempMail-Delivery": delivery_id,
                    "X-TempMail-Timestamp": timestamp,
                    "X-TempMail-Signature": signature,
                },
            )
            assert response.status == 200
            assert await response.json() == {"ok": True, "stored": 1}
        finally:
            await client.close()

    asyncio.run(exercise())
    assert stored.await_args.kwargs["allowed_domains"] == {"mail.example.com"}
    notified.assert_called_once_with("alias@mail.example.com")


def test_selfhosted_webhook_rejects_bad_signature_before_database(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.config.settings.login_email_selfhosted_webhook_secret", "s" * 32
    )

    async def exercise() -> None:
        client = TestClient(TestServer(create_webhook_app(None)))
        await client.start_server()
        try:
            response = await client.post(
                "/webhooks/selfhosted-tempmail",
                json={"id": "mail-1", "to": "alias@mail.example.com"},
                headers={
                    "X-TempMail-Delivery": str(uuid.uuid4()),
                    "X-TempMail-Timestamp": str(int(time.time())),
                    "X-TempMail-Signature": "0" * 64,
                },
            )
            assert response.status == 401
        finally:
            await client.close()

    asyncio.run(exercise())


def test_selfhosted_webhook_starts_without_enabling_mini_app(monkeypatch) -> None:
    started: list[object] = []

    class Runner:
        def __init__(self, app):
            self.app = app

        async def setup(self):
            return None

    class Site:
        def __init__(self, runner, host, port):
            started.append((runner, host, port))

        async def start(self):
            return None

    monkeypatch.setattr("app.config.settings.mini_app_enabled", False)
    monkeypatch.setattr("app.config.settings.temp_mail_webhook_secret", "")
    monkeypatch.setattr(
        "app.config.settings.login_email_selfhosted_webhook_secret", "s" * 32
    )
    monkeypatch.setattr("app.webapp.server.web.AppRunner", Runner)
    monkeypatch.setattr("app.webapp.server.web.TCPSite", Site)

    runner = asyncio.run(start_webapp(None, None))

    assert runner is not None
    assert len(started) == 1
    paths = {route.resource.canonical for route in runner.app.router.routes()}
    assert "/webhooks/selfhosted-tempmail" in paths
    assert "/mini-app" not in paths


def test_mini_app_actions_enable_compact_view_specific_layout() -> None:
    index = Path("app/webapp/static/index.html").read_text(encoding="utf-8")
    script = Path("app/webapp/static/app.js").read_text(encoding="utf-8")
    styles = Path("app/webapp/static/styles.css").read_text(encoding="utf-8")

    assert '<body data-view="dashboard">' in index
    assert "20260813-delete1" in index
    assert "document.body.dataset.view = view" in script
    assert "tg.disableVerticalSwipes?.()" in script
    assert 'height: var(--tg-viewport-stable-height, 100dvh)' in styles
    assert "overflow-y: auto" in styles
    assert 'const shell = qs(".shell")' in script
    assert 'body[data-view="actions"] .action-pane .panel' in styles
    assert "const ACCOUNT_PAGE_SIZE = 10" in script
    assert 'id="accountPagination"' in index
    assert 'id="deleteAccountBtn"' in script
    assert 'method: "DELETE"' in script
    assert script.count("await confirmAction(`") >= 2
    assert ".account-danger-zone" in styles
