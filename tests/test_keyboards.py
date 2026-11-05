import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import message_handlers
from app.bot.keyboards import (
    account_actions_panel,
    account_delete_confirm_panel,
    accounts_panel,
    bot_menu_button,
    login_email_account_panel,
    login_email_domains_panel,
    login_email_guard_panel,
    login_phone_panel,
    main_menu,
)
from app.bot.handlers import BOT_COMMANDS, cancel_current
from app.bot.interaction_cleanup import reset_message_flow, tracked_answer
from app.bot.message_keyboards import (
    account_picker_panel,
    hour_panel,
    master_rule_detail_panel,
    master_schedules_panel,
    message_history_panel,
    message_home_panel,
    minute_panel,
    offset_panel,
    schedules_panel,
)
from app.config import settings
from app.main import restore_admin_keyboards


def test_main_menu_is_collapsible_and_reopenable() -> None:
    payload = main_menu().model_dump(exclude_none=True)
    assert payload["resize_keyboard"] is True
    assert payload["is_persistent"] is False
    assert payload["one_time_keyboard"] is False
    assert "remove_keyboard" not in payload


def test_project_never_sends_reply_keyboard_remove() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py"))
    assert "ReplyKeyboardRemove" not in source
    assert "remove_keyboard" not in source


def test_project_has_one_canonical_reply_keyboard_constructor() -> None:
    constructors: list[tuple[Path, int]] = []
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name == "ReplyKeyboardMarkup":
                constructors.append((path, node.lineno))

    assert len(constructors) == 1
    assert constructors[0][0] == Path("app/bot/keyboards.py")


def test_service_restart_restores_collapsible_keyboard_for_every_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "admin_ids", [101, 202])
    bot = type("BotStub", (), {"send_message": AsyncMock()})()

    asyncio.run(restore_admin_keyboards(bot))

    assert bot.send_message.await_count == 2
    assert [call.args[0] for call in bot.send_message.await_args_list] == [101, 202]
    for call in bot.send_message.await_args_list:
        payload = call.kwargs["reply_markup"].model_dump(exclude_none=True)
        assert payload["resize_keyboard"] is True
        assert payload["is_persistent"] is False
        assert payload["one_time_keyboard"] is False


def test_main_navigation_labels_are_reply_keyboard_buttons() -> None:
    labels = {button.text for row in main_menu().keyboard for button in row}
    assert labels == {
        "系统状态",
        "账号管理",
        "登录账号",
        "扫码登录",
        "安全防护",
        "发送消息",
    }
    assert len(main_menu().keyboard) == 3


def test_bot_only_message_commands_are_published() -> None:
    commands = {item.command for item in BOT_COMMANDS}
    assert "messages" in commands
    assert "auto_send" not in commands


def test_message_router_runs_before_existing_text_fallback() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert source.index("dp.include_router(message_router)") < source.index(
        "dp.include_router(router)"
    )


def test_message_picker_supports_multi_select_and_paging() -> None:
    accounts = [
        SimpleNamespace(
            id=value,
            user_id=1000 + value,
            username=None,
            phone_masked="+65****0000",
            first_name=f"Account{value}",
            last_name=None,
        )
        for value in range(1, 4)
    ]
    panel = account_picker_panel(accounts, {1, 3}, "schedule", 2, 4)

    assert [row[0].text[0] for row in panel.inline_keyboard[:3]] == ["✓", "○", "✓"]
    assert panel.inline_keyboard[0][0].text == "✓ #1 · 1001 · Account1"
    assert panel.inline_keyboard[0][0].callback_data == "msgpick:schedule:toggle:1:2"
    assert [button.callback_data for button in panel.inline_keyboard[3]] == [
        "msgpick:schedule:page:1",
        "msgpick:schedule:page:2",
        "msgpick:schedule:page:3",
    ]
    assert [button.text for button in panel.inline_keyboard[4]] == ["全选", "全不选"]
    assert [button.callback_data for button in panel.inline_keyboard[4]] == [
        "msgpick:schedule:all:2",
        "msgpick:schedule:none:2",
    ]
    assert panel.inline_keyboard[5][0].text == "确认选择（2）"


def test_message_home_actions_use_two_columns() -> None:
    rows = message_home_panel().inline_keyboard

    assert [[button.text for button in row] for row in rows] == [
        ["发送消息", "新增自动发送"],
        ["管理自动发送", "发送历史"],
        ["返回主菜单"],
    ]


def test_schedule_time_and_random_range_are_button_driven() -> None:
    hours = [button for row in hour_panel().inline_keyboard[:-1] for button in row]
    minutes = [button for row in minute_panel(12).inline_keyboard[:3] for button in row]
    offsets = [row[0] for row in offset_panel().inline_keyboard[:5]]

    assert len(hours) == 24
    assert hours[0].callback_data == "msgtime:hour:0"
    assert hours[-1].callback_data == "msgtime:hour:23"
    assert [button.text for button in minutes] == [f"{value:02d}分" for value in range(0, 60, 5)]
    assert minute_panel(12).inline_keyboard[3][0].text == "输入其他分钟"
    assert [button.text for button in offsets] == [
        "不随机",
        "±15 分钟",
        "±30 分钟",
        "±60 分钟",
        "±120 分钟",
    ]


def test_message_history_and_account_actions_are_reusable() -> None:
    history = message_history_panel(
        [
            SimpleNamespace(
                id=17,
                params_json={
                    "accounts": [9],
                    "target": "@example_bot",
                    "text": "/checkin",
                },
            ),
            SimpleNamespace(
                id=18,
                params_json={
                    "accounts": [9],
                    "target": "@example_bot",
                    "payload": {"text": "/legacy"},
                },
            ),
            SimpleNamespace(
                id=19,
                params_json={"accounts": [9], "target": "@example_bot"},
            ),
        ]
    )
    actions = account_actions_panel(9).inline_keyboard

    assert [row[0].callback_data for row in history.inline_keyboard[:2]] == [
        "msghistory:reuse:17",
        "msghistory:reuse:18",
    ]
    assert [(button.text, button.callback_data) for button in actions[-3]] == [
        ("发送消息", "msgsend:start:9"),
        ("设置自动发送", "msgsched:list:9"),
    ]


def test_schedule_list_has_paging_controls() -> None:
    rule = SimpleNamespace(
        id=17,
        rule_id=4,
        account_id=9,
        target_ref="@example_bot",
        enabled=True,
        next_run_at=datetime(2026, 11, 2, 4, 0, tzinfo=UTC),
    )
    panel = schedules_panel([rule], None, 2, 4)
    pager = panel.inline_keyboard[1]

    assert panel.inline_keyboard[0][0].text.startswith("✓ 规则 #4")
    assert [button.callback_data for button in pager] == [
        "msgsched:list:all:1",
        "msgsched:list:all:2",
        "msgsched:list:all:3",
    ]


def test_master_rule_list_is_sorted_and_labeled_by_rule() -> None:
    rules = [
        (SimpleNamespace(id=3, target_ref="@third_bot", enabled=True), 2),
        (SimpleNamespace(id=8, target_ref="@eighth_bot", enabled=False), 1),
    ]

    panel = master_schedules_panel(rules, page=1, pages=1)

    assert [row[0].text for row in panel.inline_keyboard[:2]] == [
        "✓ 规则 #3 @third_bot · 2 个账号",
        "○ 规则 #8 @eighth_bot · 1 个账号",
    ]
    assert [row[0].callback_data for row in panel.inline_keyboard[:2]] == [
        "msgmaster:view:3",
        "msgmaster:view:8",
    ]


def test_master_rule_detail_can_change_applied_accounts() -> None:
    panel = master_rule_detail_panel(SimpleNamespace(id=3, enabled=True))
    callbacks = {
        button.callback_data
        for row in panel.inline_keyboard
        for button in row
    }

    assert "msgmaster:accounts:3" in callbacks
    assert "msgmaster:target:3" in callbacks
    assert "msgmaster:time:3" in callbacks
    assert "msghistory:master:3" in callbacks


def test_master_rule_list_handler_renders_master_rules_in_rule_order() -> None:
    rules = [
        (SimpleNamespace(id=3, target_ref="@third_bot", enabled=True), 2),
        (SimpleNamespace(id=8, target_ref="@eighth_bot", enabled=False), 1),
    ]
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=2),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rules)),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )

    asyncio.run(
        message_handlers._render_schedule_list(
            callback,
            lambda: SessionContext(),
            account_id=None,
        )
    )

    query = str(session.execute.await_args.args[0])
    assert "ORDER BY scheduled_message_rules.id" in query
    panel = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert [row[0].callback_data for row in panel.inline_keyboard[:2]] == [
        "msgmaster:view:3",
        "msgmaster:view:8",
    ]


def test_master_account_picker_confirm_applies_selected_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_rule = SimpleNamespace(id=4)
    apply_accounts = AsyncMock(
        return_value=[SimpleNamespace(account_id=1), SimpleNamespace(account_id=3)]
    )
    audit_action = AsyncMock()
    render_detail = AsyncMock()
    monkeypatch.setattr(message_handlers, "apply_rule_accounts", apply_accounts)
    monkeypatch.setattr(message_handlers, "_audit_rule_action", audit_action)
    monkeypatch.setattr(message_handlers, "_render_master_detail", render_detail)
    session = SimpleNamespace(
        get=AsyncMock(return_value=master_rule),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    sessionmaker = lambda: SessionContext()
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"selected_account_ids": [3, 1]}),
        clear=AsyncMock(),
    )
    callback = SimpleNamespace(
        data="msgpick:master-4:done",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=99),
        message=SimpleNamespace(),
    )

    asyncio.run(
        message_handlers.account_picker_callback(
            callback,
            state,
            sessionmaker,
        )
    )

    apply_accounts.assert_awaited_once_with(master_rule, [1, 3], session)
    audit_action.assert_awaited_once_with(
        session,
        99,
        "accounts",
        4,
        {"account_ids": [1, 3]},
    )
    session.commit.assert_awaited_once()
    state.clear.assert_awaited_once()
    render_detail.assert_awaited_once_with(
        callback,
        sessionmaker,
        4,
        notice="已应用到 2 个账号",
    )


def test_master_time_edit_uses_template_overwrite_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_rule = SimpleNamespace(
        id=4,
        target_ref="@master_bot",
        message_text="/master",
        random_offset_minutes=60,
    )
    update_template = AsyncMock()
    audit_action = AsyncMock()
    render_detail = AsyncMock()
    monkeypatch.setattr(message_handlers, "update_rule_template", update_template)
    monkeypatch.setattr(message_handlers, "_audit_rule_action", audit_action)
    monkeypatch.setattr(message_handlers, "_render_master_detail", render_detail)
    session = SimpleNamespace(
        get=AsyncMock(return_value=master_rule),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    sessionmaker = lambda: SessionContext()
    callback = SimpleNamespace(
        data="msgedittime:master:4:minute:12:30",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=99),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )

    asyncio.run(
        message_handlers.edit_time_callback(
            callback,
            SimpleNamespace(),
            sessionmaker,
        )
    )

    update_template.assert_awaited_once_with(
        master_rule,
        "@master_bot",
        "/master",
        12,
        30,
        60,
        session,
    )
    audit_action.assert_awaited_once_with(
        session,
        99,
        "update",
        4,
        {"field": "time", "scope": "master"},
    )
    session.commit.assert_awaited_once()
    render_detail.assert_awaited_once_with(callback, sessionmaker, 4)


def test_account_picker_select_all_uses_every_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_active = AsyncMock(return_value=[1, 2, 9, 17])
    show_picker = AsyncMock()
    monkeypatch.setattr(message_handlers, "_all_active_account_ids", all_active)
    monkeypatch.setattr(message_handlers, "_show_picker", show_picker)
    session = SimpleNamespace()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    sessionmaker = lambda: SessionContext()
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"selected_account_ids": [2]}),
        update_data=AsyncMock(),
    )
    callback = SimpleNamespace(data="msgpick:schedule:all:2")

    asyncio.run(
        message_handlers.account_picker_callback(
            callback,
            state,
            sessionmaker,
        )
    )

    all_active.assert_awaited_once_with(session)
    state.update_data.assert_awaited_once_with(selected_account_ids=[1, 2, 9, 17])
    show_picker.assert_awaited_once_with(callback, state, sessionmaker, "schedule", 2)


def test_account_picker_select_none_clears_every_selected_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    show_picker = AsyncMock()
    monkeypatch.setattr(message_handlers, "_show_picker", show_picker)
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"selected_account_ids": [1, 2, 9, 17]}),
        update_data=AsyncMock(),
    )
    callback = SimpleNamespace(data="msgpick:send:none:3")

    asyncio.run(
        message_handlers.account_picker_callback(
            callback,
            state,
            SimpleNamespace(),
        )
    )

    state.update_data.assert_awaited_once_with(selected_account_ids=[])
    show_picker.assert_awaited_once_with(
        callback,
        state,
        SimpleNamespace(),
        "send",
        3,
    )


def test_schedule_account_picker_loads_custom_page_size() -> None:
    accounts = [SimpleNamespace(id=value) for value in range(9, 11)]
    scalar_result = SimpleNamespace(all=lambda: accounts)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=10),
        scalars=AsyncMock(return_value=scalar_result),
    )

    loaded, page, pages = asyncio.run(message_handlers._active_account_page(session, 2))

    assert loaded == accounts
    assert (page, pages) == (2, 2)


def test_schedule_rule_list_loads_custom_page_size() -> None:
    rules = [
        SimpleNamespace(
            id=11,
            rule_id=5,
            account_id=9,
            target_ref="@example_bot",
            enabled=True,
            next_run_at=datetime(2026, 11, 3, 4, 0, tzinfo=UTC),
        )
    ]
    scalar_result = SimpleNamespace(all=lambda: rules)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=11),
        scalars=AsyncMock(return_value=scalar_result),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    asyncio.run(
        message_handlers._render_schedule_list(
            callback,
            lambda: SessionContext(),
            account_id=9,
            requested_page=2,
        )
    )

    assert callback.message.edit_text.await_args.args[0] == "自动发送规则（共 11 条）"
    panel = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert panel.inline_keyboard[0][0].callback_data == "msgrule:view:11"


def test_completed_manual_send_deletes_recorded_interaction_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        message_handlers,
        "run_message_job",
        AsyncMock(return_value=SimpleNamespace(succeeded=1, failed=0, job_id=27)),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "message_flow_message_ids": [101, 102, 103],
                "selected_account_ids": [2],
                "target": "@example_bot",
                "message_text": "/checkin",
            }
        ),
        clear=AsyncMock(),
    )
    bot = SimpleNamespace(delete_messages=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=88),
        message_id=103,
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        answer=AsyncMock(),
        bot=bot,
        from_user=SimpleNamespace(id=99),
        message=message,
    )

    asyncio.run(
        message_handlers.confirm_manual_send(
            callback,
            state,
            sessionmaker=SimpleNamespace(),
            client_pool=SimpleNamespace(),
        )
    )

    bot.delete_messages.assert_awaited_once_with(
        chat_id=88,
        message_ids=[101, 102, 103],
    )
    assert message.answer.await_args.args[0].startswith("发送完成：成功 1，失败 0")


def test_completed_schedule_deletes_recorded_interaction_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        message_handlers,
        "create_schedules",
        AsyncMock(return_value=[SimpleNamespace(id=31, rule_id=7)]),
    )
    monkeypatch.setattr(message_handlers, "audit", AsyncMock())
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(id=1)),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "message_flow_message_ids": [301, 302, 303],
                "selected_account_ids": [2],
                "target": "@example_bot",
                "message_text": "/checkin",
                "hour": 12,
                "minute": 0,
                "random_offset_minutes": 60,
            }
        ),
        clear=AsyncMock(),
    )
    bot = SimpleNamespace(delete_messages=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=88),
        message_id=303,
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        answer=AsyncMock(),
        bot=bot,
        from_user=SimpleNamespace(id=99),
        message=message,
    )

    asyncio.run(
        message_handlers.confirm_schedule(
            callback,
            state,
            sessionmaker=lambda: SessionContext(),
        )
    )

    bot.delete_messages.assert_awaited_once_with(
        chat_id=88,
        message_ids=[301, 302, 303],
    )
    assert message.answer.await_args.args[0].startswith("已创建主规则 #7，应用到 1 个账号")


def test_message_flow_records_entry_prompt_and_user_input() -> None:
    class MemoryState:
        def __init__(self) -> None:
            self.data = {}

        async def get_data(self):
            return dict(self.data)

        async def clear(self):
            self.data.clear()

        async def update_data(self, **values):
            self.data.update(values)

    state = MemoryState()
    prompt = SimpleNamespace(message_id=402)
    user_message = SimpleNamespace(
        message_id=403,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=404)),
    )

    async def scenario() -> None:
        await reset_message_flow(state, SimpleNamespace(message_id=401), prompt)
        await tracked_answer(user_message, state, "下一步")

    asyncio.run(scenario())

    assert state.data["message_flow_message_ids"] == [401, 402, 403, 404]


def test_cancelled_message_flow_deletes_recorded_interaction_messages() -> None:
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"message_flow_message_ids": [201, 202]}),
        clear=AsyncMock(),
    )
    bot = SimpleNamespace(delete_messages=AsyncMock())
    message = SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=77),
        message_id=203,
        answer=AsyncMock(),
    )

    asyncio.run(cancel_current(message, state))

    bot.delete_messages.assert_awaited_once_with(
        chat_id=77,
        message_ids=[201, 202, 203],
    )
    assert message.answer.await_args.args[0].startswith("已取消当前操作")


def test_account_inline_keyboard_pages_ten_accounts_and_preserves_page() -> None:
    accounts = [
        type(
            "Account",
            (),
            {
                "id": account_id,
                "user_id": 1000 + account_id,
                "username": None,
                "phone_masked": "+44****123",
            },
        )()
        for account_id in range(11, 21)
    ]

    panel = accounts_panel(accounts, page=2, pages=4)
    account_buttons = [row[0] for row in panel.inline_keyboard[:10]]

    assert len(account_buttons) == 10
    assert account_buttons[0].callback_data == "acct:11:2"
    assert account_buttons[-1].callback_data == "acct:20:2"
    pager = panel.inline_keyboard[10]
    assert [button.callback_data for button in pager] == [
        "nav:accounts:1",
        "nav:accounts:2",
        "nav:accounts:3",
    ]


def test_account_deletion_uses_two_confirmations_and_preserves_page() -> None:
    actions = {
        button.callback_data
        for row in account_actions_panel(17, accounts_page=3).inline_keyboard
        for button in row
    }
    first = account_delete_confirm_panel(17, 3)
    second = account_delete_confirm_panel(17, 3, final=True)

    assert "acct_delete:ask:17:3" in actions
    assert first.inline_keyboard[0][0].callback_data == "acct_delete:verify:17:3"
    assert second.inline_keyboard[0][0].callback_data == "acct_delete:confirm:17:3"
    assert first.inline_keyboard[1][0].callback_data == "acct:17:3"
    assert second.inline_keyboard[1][0].callback_data == "acct:17:3"


def test_account_places_one_click_login_email_rotation_left_of_session_export() -> None:
    rows = account_actions_panel(17).inline_keyboard
    action_row = next(
        row
        for row in rows
        if any(button.callback_data == "acct_action:rotate_login_email:17" for button in row)
    )

    assert [(button.text, button.callback_data) for button in action_row] == [
        ("一键换绑邮箱", "acct_action:rotate_login_email:17"),
        ("导出 Session", "acct_action:export_session:17"),
    ]


def test_advanced_features_stay_out_of_reply_keyboard() -> None:
    labels = {button.text for row in main_menu().keyboard for button in row}
    assert labels.isdisjoint(
        {
            "导入Session",
            "导出Session",
            "批量导入Session",
            "批量任务",
            "目标与速率",
            "监控中心",
            "内置应用",
        }
    )


def test_phone_login_offers_qr_fallback() -> None:
    callbacks = {
        button.callback_data for row in login_phone_panel().inline_keyboard for button in row
    }
    assert callbacks == {"login:qr", "flow:cancel"}


def test_native_bot_menu_opens_configured_mini_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mini_app_enabled", True)
    monkeypatch.setattr(settings, "mini_app_public_url", "https://bot.example/mini-app")
    payload = bot_menu_button().model_dump(exclude_none=True)
    assert payload["type"] == "web_app"
    assert payload["text"] == "打开"
    assert payload["web_app"]["url"].endswith("/mini-app")


def test_security_center_separates_domains_whitelist_events_and_temp_mail() -> None:
    callbacks = {
        button.callback_data for row in login_email_guard_panel().inline_keyboard for button in row
    }
    assert {
        "emailguard:domains",
        "emailguard:whitelist",
        "emailguard:events",
        "emailguard:testimap",
        "emailguard:checkall",
    } <= callbacks


def test_domain_deletion_requires_confirmation() -> None:
    panel = login_email_domains_panel(
        ("one.example", "two.example", "three.example"),
        "one.example",
        {
            "one.example": "cloudflare",
            "two.example": "gmail",
            "three.example": "selfhosted",
        },
    )
    callbacks = [button.callback_data for row in panel.inline_keyboard for button in row]
    assert "emailguard:deleteask:0" in callbacks
    assert "emailguard:delete:0" not in callbacks
    assert "emailguard:backend:0" in callbacks
    labels = [button.text for row in panel.inline_keyboard for button in row]
    assert "☁️ CF TempMail" in labels
    assert "✉️ Gmail" in labels
    assert "🏠 自建 TempMail" in labels


def test_account_email_guard_has_window_setting_button() -> None:
    callbacks = {
        button.callback_data
        for row in login_email_account_panel(15, False).inline_keyboard
        for button in row
    }
    assert "emailguard:window:15" in callbacks
