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
    api_account_action,
    api_scheduled_messages,
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


def test_mini_app_registers_message_and_schedule_routes() -> None:
    app = asyncio.run(create_webapp(None, None))
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("GET", "/mini-app/api/message-accounts") in routes
    assert ("POST", "/mini-app/api/messages/send") in routes
    assert ("GET", "/mini-app/api/scheduled-messages") in routes
    assert ("POST", "/mini-app/api/scheduled-messages") in routes


def test_mini_app_bulk_schedule_creation_preserves_selected_accounts(monkeypatch) -> None:
    admin = SimpleNamespace(id=3)
    session = SimpleNamespace(scalar=AsyncMock(return_value=admin), commit=AsyncMock())

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    request = SimpleNamespace(
        method="POST",
        app={"sessionmaker": lambda: SessionContext()},
        json=AsyncMock(
            return_value={
                "action": "create",
                "account_ids": [7, 8],
                "target": "@example_bot",
                "text": "/checkin",
                "base_time": "12:05",
                "random_offset_minutes": 60,
            }
        ),
    )
    create_mock = AsyncMock(
        return_value=[
            SimpleNamespace(id=21, rule_id=11),
            SimpleNamespace(id=22, rule_id=11),
        ]
    )
    monkeypatch.setattr(
        "app.webapp.server.authenticated",
        AsyncMock(return_value=SimpleNamespace(id=99)),
    )
    monkeypatch.setattr("app.webapp.server.create_schedules", create_mock)
    monkeypatch.setattr("app.webapp.server.audit", AsyncMock())

    response = asyncio.run(api_scheduled_messages(request))

    assert response.status == 200
    assert json.loads(response.text)["message"] == "已为 2 个账号创建自动发送规则"
    assert create_mock.await_args.args[1:] == (
        [7, 8],
        "@example_bot",
        "/checkin",
        12,
        5,
        60,
    )


def test_mini_app_account_action_runs_one_click_login_email_rotation(monkeypatch) -> None:
    account = SimpleNamespace(id=17)
    admin = SimpleNamespace(id=3)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=admin),
        get=AsyncMock(return_value=account),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    client = object()
    protector = SimpleNamespace(change_now=AsyncMock(return_value="new@mail.example"))
    client_pool = SimpleNamespace(
        get_client=AsyncMock(return_value=client),
        login_email_protector=protector,
    )
    request = SimpleNamespace(
        app={"sessionmaker": lambda: SessionContext(), "client_pool": client_pool},
        match_info={"account_id": "17"},
        json=AsyncMock(return_value={"action": "rotate_login_email"}),
    )
    audit_mock = AsyncMock()
    monkeypatch.setattr(
        "app.webapp.server.authenticated",
        AsyncMock(return_value=SimpleNamespace(id=9)),
    )
    monkeypatch.setattr("app.webapp.server.audit", audit_mock)

    response = asyncio.run(api_account_action(request))

    assert response.status == 200
    assert json.loads(response.text) == {
        "ok": True,
        "message": "账号 #17 一键换绑邮箱成功：new@mail.example",
    }
    client_pool.get_client.assert_awaited_once_with(17)
    protector.change_now.assert_awaited_once_with(17, client)
    audit_mock.assert_awaited_once()


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
    assert "20261102-daily-summary1" in index
    assert "document.body.dataset.view = view" in script
    assert "tg.disableVerticalSwipes?.()" in script
    assert 'height: var(--tg-viewport-stable-height, 100dvh)' in styles
    assert "overflow-y: auto" in styles
    assert 'const shell = qs(".shell")' in script
    assert 'body[data-view="actions"] .action-pane .panel' in styles
    assert "const ACCOUNT_PAGE_SIZE = 10" in script
    assert 'id="accountPagination"' in index
    assert 'id="deleteAccountBtn"' in script
    assert 'data-account-action="rotate_login_email">一键换绑邮箱</button>' in script
    assert script.index('data-account-action="export_session"') < script.index(
        'data-account-action="rotate_login_email"'
    )
    assert 'method: "DELETE"' in script
    assert script.count("await confirmAction(`") >= 2
    assert ".account-danger-zone" in styles
    assert 'id="messageAccountPicker"' in index
    assert 'id="scheduleAccountPicker"' in index
    assert 'id="selectAllMessageAccounts"' in index
    assert 'id="clearMessageAccounts"' in index
    assert 'id="selectAllScheduleAccounts"' in index
    assert 'id="clearScheduleAccounts"' in index
    assert 'function setAccountSelection(kind, selectAll)' in script
    assert 'id="scheduleHistory"' in index
    assert 'data-history-reuse' in script
    assert 'data-schedule-action="reuse"' in script
    assert 'timeZone: "Asia/Singapore"' in script
