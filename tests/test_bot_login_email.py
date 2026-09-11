import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.bot import handlers
from app.bot.handlers import (
    login_email_configure,
    login_email_edit_address,
    login_email_edit_code,
)
from app.bot.keyboards import account_actions_panel
from app.services.login_email_protection import LoginEmailProtector


def test_account_has_separate_login_email_change_action() -> None:
    buttons = [button for row in account_actions_panel(42).inline_keyboard for button in row]
    assert any(
        button.text == "更换登录邮箱"
        and button.callback_data == "loginemail:configure:42"
        for button in buttons
    )
    assert any(button.callback_data == "emailguard:account:42" for button in buttons)


def test_login_email_change_rejects_account_without_existing_email(monkeypatch) -> None:
    callback = SimpleNamespace(data="loginemail:configure:42", answer=AsyncMock())
    state = AsyncMock()
    pool = SimpleNamespace(get_client=AsyncMock(return_value="client"))
    monkeypatch.setattr(
        handlers.account_ops,
        "get_2fa_info",
        AsyncMock(return_value={"login_email_pattern": None}),
    )

    asyncio.run(login_email_configure(callback, state, Mock(), pool))

    state.clear.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "首次设置" in callback.answer.await_args.args[0]


def test_invalid_email_does_not_request_code() -> None:
    message = SimpleNamespace(text="invalid", answer=AsyncMock())
    pool = SimpleNamespace(get_client=AsyncMock())
    asyncio.run(login_email_edit_address(message, AsyncMock(), Mock(), pool))
    pool.get_client.assert_not_awaited()


def test_manual_email_wait_blocks_other_changes() -> None:
    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector._change_locks = {}
    protector.manual_email_waits = {
        42: datetime.now(timezone.utc) + timedelta(minutes=5)
    }
    assert protector.has_change_in_progress(42)
    assert not protector.has_change_in_progress(43)
    protector.manual_email_waits[42] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not protector.has_change_in_progress(42)


def test_email_confirmation_saves_only_confirmed_address(monkeypatch) -> None:
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    state = AsyncMock()
    state.get_data.return_value = {
        "account_id": 42,
        "deadline": deadline.isoformat(),
        "email": "me@example.com",
        "code_length": 6,
    }
    security = SimpleNamespace(login_email_encrypted=None)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.get.return_value = security
    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector._change_locks = {}
    protector.manual_email_waits = {42: deadline}
    pool = SimpleNamespace(
        get_client=AsyncMock(return_value="client"),
        login_email_protector=protector,
    )
    message = SimpleNamespace(
        text="123456",
        from_user=SimpleNamespace(id=7),
        answer=AsyncMock(),
    )
    monkeypatch.setattr(
        handlers.account_ops,
        "confirm_login_email",
        AsyncMock(return_value=SimpleNamespace(email="me@example.com")),
    )
    monkeypatch.setattr(handlers, "encrypt_text", lambda text: "encrypted:" + text)
    monkeypatch.setattr(handlers, "audit", AsyncMock())

    asyncio.run(login_email_edit_code(message, state, lambda: session, pool))

    assert security.login_email_encrypted == "encrypted:me@example.com"
    session.commit.assert_awaited_once()
    state.clear.assert_awaited_once()
    assert 42 not in protector.manual_email_waits


def test_expired_code_does_not_call_telegram() -> None:
    state = AsyncMock()
    state.get_data.return_value = {
        "account_id": 42,
        "deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    }
    pool = SimpleNamespace(get_client=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock())

    asyncio.run(login_email_edit_code(message, state, Mock(), pool))

    pool.get_client.assert_not_awaited()
    state.clear.assert_awaited_once()
