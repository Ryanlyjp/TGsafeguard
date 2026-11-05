import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import (
    ScheduledMessage,
    ScheduledMessageDailyRun,
    ScheduledMessageRule,
    TgAccount,
)
from app.services import scheduled_messages
from app.services.scheduled_messages import (
    ScheduledMessageService,
    apply_rule_accounts,
    choose_run_at,
    update_rule_template,
    update_schedule,
    validate_bot_target,
    validate_message_text,
    validate_schedule_values,
)


def test_schedule_time_is_randomized_inside_singapore_day() -> None:
    selected = choose_run_at(
        date(2026, 11, 2),
        12 * 60,
        60,
        randint=lambda lower, upper: upper,
    )
    assert selected == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)


def test_schedule_window_is_clamped_at_day_boundary() -> None:
    early = choose_run_at(
        date(2026, 11, 2),
        30,
        60,
        randint=lambda lower, upper: lower,
    )
    assert early == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)


def test_scheduled_message_values_are_validated() -> None:
    assert validate_bot_target(" @ExampleBot ") == "@examplebot"
    assert validate_message_text(" /checkin ") == "/checkin"
    assert validate_schedule_values(12, 5, 60) == 725
    with pytest.raises(ValueError, match="@username"):
        validate_bot_target("example")
    with pytest.raises(ValueError, match="4096"):
        validate_message_text("x" * 4097)
    with pytest.raises(ValueError, match="00:00"):
        validate_schedule_values(24, 0, 0)
    with pytest.raises(ValueError, match="720"):
        validate_schedule_values(12, 0, 721)


def test_one_account_can_store_multiple_scheduled_rules() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TgAccount.__table__.create(engine)
    ScheduledMessageRule.__table__.create(engine)
    ScheduledMessage.__table__.create(engine)
    with Session(engine) as session:
        session.add(
            TgAccount(
                id=7,
                phone_encrypted="encrypted",
                phone_masked="+65****0000",
            )
        )
        session.add_all(
            [
                ScheduledMessageRule(
                    id=1,
                    target_ref="@first_bot",
                    message_text="/one",
                    base_minute=720,
                    random_offset_minutes=60,
                ),
                ScheduledMessageRule(
                    id=2,
                    target_ref="@second_bot",
                    message_text="/two",
                    base_minute=1080,
                    random_offset_minutes=30,
                ),
                ScheduledMessage(
                    rule_id=1,
                    account_id=7,
                    target_ref="@first_bot",
                    message_text="/one",
                    base_minute=720,
                    random_offset_minutes=60,
                    next_run_at=datetime(2026, 11, 3, 4, 0, tzinfo=UTC),
                ),
                ScheduledMessage(
                    rule_id=2,
                    account_id=7,
                    target_ref="@second_bot",
                    message_text="/two",
                    base_minute=1080,
                    random_offset_minutes=30,
                    next_run_at=datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
                ),
            ]
        )
        session.commit()

        rules = session.scalars(
            select(ScheduledMessage).where(ScheduledMessage.account_id == 7)
        ).all()
        assert [rule.message_text for rule in rules] == ["/one", "/two"]


def test_account_override_does_not_change_master_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = SimpleNamespace(
        target_ref="@master_bot",
        message_text="/master",
        base_minute=720,
        random_offset_minutes=60,
    )
    assignment = SimpleNamespace(
        target_ref=master.target_ref,
        message_text=master.message_text,
        base_minute=master.base_minute,
        random_offset_minutes=master.random_offset_minutes,
        next_run_at=None,
    )
    monkeypatch.setattr(
        scheduled_messages,
        "ensure_bot_target",
        AsyncMock(return_value="@account_bot"),
    )
    monkeypatch.setattr(
        scheduled_messages,
        "choose_run_at",
        lambda *args, **kwargs: datetime(2026, 11, 4, 4, 0, tzinfo=UTC),
    )

    asyncio.run(
        update_schedule(
            assignment,
            "@account_bot",
            "/account",
            13,
            30,
            15,
            SimpleNamespace(),
        )
    )

    assert assignment.target_ref == "@account_bot"
    assert assignment.message_text == "/account"
    assert master.target_ref == "@master_bot"
    assert master.message_text == "/master"


def test_master_rule_update_overwrites_account_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = SimpleNamespace(
        id=4,
        target_ref="@old_bot",
        message_text="/old",
        base_minute=600,
        random_offset_minutes=15,
        timezone="Asia/Singapore",
        enabled=False,
    )
    assignments = [
        SimpleNamespace(
            account_id=1,
            target_ref="@custom_bot",
            message_text="/custom",
            base_minute=900,
            random_offset_minutes=120,
            timezone="Asia/Singapore",
            enabled=True,
            next_run_at=None,
        ),
        SimpleNamespace(
            account_id=2,
            target_ref="@other_bot",
            message_text="/other",
            base_minute=300,
            random_offset_minutes=0,
            timezone="Asia/Singapore",
            enabled=True,
            next_run_at=None,
        ),
    ]
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: assignments))
    )
    monkeypatch.setattr(
        scheduled_messages,
        "ensure_bot_target",
        AsyncMock(return_value="@master_bot"),
    )
    run_at = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
    monkeypatch.setattr(scheduled_messages, "choose_run_at", lambda *args, **kwargs: run_at)

    asyncio.run(
        update_rule_template(
            master,
            "@master_bot",
            "/master",
            12,
            0,
            60,
            session,
        )
    )

    assert master.target_ref == "@master_bot"
    assert master.message_text == "/master"
    for assignment in assignments:
        assert assignment.target_ref == "@master_bot"
        assert assignment.message_text == "/master"
        assert assignment.base_minute == 720
        assert assignment.random_offset_minutes == 60
        assert assignment.enabled is False
        assert assignment.next_run_at == run_at


def test_master_rule_accounts_can_be_added_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = SimpleNamespace(
        id=4,
        target_ref="@master_bot",
        message_text="/master",
        base_minute=720,
        random_offset_minutes=60,
        timezone="Asia/Singapore",
        enabled=True,
    )
    removed = SimpleNamespace(account_id=1)
    retained = SimpleNamespace(account_id=2)
    session = SimpleNamespace(
        scalars=AsyncMock(
            return_value=SimpleNamespace(all=lambda: [removed, retained])
        ),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
        delete=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(
        scheduled_messages,
        "active_account_ids",
        AsyncMock(return_value=[2, 3]),
    )
    run_at = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)
    monkeypatch.setattr(scheduled_messages, "choose_run_at", lambda *args, **kwargs: run_at)

    result = asyncio.run(apply_rule_accounts(master, [2, 3], session))

    session.delete.assert_awaited_once_with(removed)
    added = session.add.call_args.args[0]
    assert isinstance(added, ScheduledMessage)
    assert added.rule_id == 4
    assert added.account_id == 3
    assert added.message_text == "/master"
    assert added.next_run_at == run_at
    assert [assignment.account_id for assignment in result] == [2, 3]


def test_scheduler_starts_once_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()

    async def loop(service):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ScheduledMessageService, "_loop", loop)
    recover = AsyncMock()
    monkeypatch.setattr(ScheduledMessageService, "_recover_incomplete_daily_runs", recover)

    async def scenario() -> None:
        service = ScheduledMessageService(SimpleNamespace(), SimpleNamespace())
        await service.start()
        first = service._task
        await service.start()
        await entered.wait()
        assert service._task is first
        assert service.running
        await service.stop()
        assert not service.running
        recover.assert_awaited_once()

    asyncio.run(scenario())


def test_due_schedule_is_sent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    send = AsyncMock(return_value=SimpleNamespace(failed=0))
    monkeypatch.setattr(scheduled_messages, "run_message_job", send)

    async def scenario() -> None:
        service = ScheduledMessageService(SimpleNamespace(), SimpleNamespace())
        service._complete_daily_run = AsyncMock()
        service._notify_completed_daily_runs = AsyncMock()
        service._claim_due = AsyncMock(
            side_effect=[
                (
                    7,
                    4,
                    3,
                    "@examplebot",
                    "/checkin",
                    datetime(2026, 11, 2, tzinfo=UTC),
                    False,
                ),
                None,
            ]
        )
        processed = await service.process_due(datetime(2026, 11, 2, tzinfo=UTC))
        assert processed == 1
        send.assert_awaited_once()
        assert send.await_args.kwargs["schedule_id"] == 7
        assert send.await_args.kwargs["master_rule_id"] == 4
        service._complete_daily_run.assert_awaited_once_with(
            4,
            date(2026, 11, 2),
            "succeeded",
        )
        service._notify_completed_daily_runs.assert_awaited_once()

    asyncio.run(scenario())


def test_missed_schedule_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr(scheduled_messages, "run_message_job", send)

    async def scenario() -> None:
        service = ScheduledMessageService(SimpleNamespace(), SimpleNamespace())
        service._notify_completed_daily_runs = AsyncMock()
        service._claim_due = AsyncMock(
            side_effect=[
                (
                    7,
                    4,
                    3,
                    "@examplebot",
                    "/checkin",
                    datetime(2026, 11, 2, tzinfo=UTC),
                    True,
                ),
                None,
            ]
        )
        assert await service.process_due(datetime(2026, 11, 2, tzinfo=UTC)) == 1
        send.assert_not_awaited()
        service._notify_completed_daily_runs.assert_awaited_once()

    asyncio.run(scenario())


def test_missed_schedule_is_advanced_and_recorded_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_for = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    next_run = datetime(2026, 11, 3, 4, 30, tzinfo=UTC)
    rule = SimpleNamespace(
        id=7,
        rule_id=4,
        account_id=3,
        target_ref="@examplebot",
        message_text="/checkin",
        base_minute=12 * 60,
        random_offset_minutes=60,
        next_run_at=scheduled_for,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[rule, None]),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    create_job = AsyncMock(return_value=SimpleNamespace(id=99))
    add_item = AsyncMock()
    finish_job = AsyncMock()
    monkeypatch.setattr(scheduled_messages, "choose_run_at", lambda *args, **kwargs: next_run)
    monkeypatch.setattr(scheduled_messages, "create_job", create_job)
    monkeypatch.setattr(scheduled_messages, "add_job_item", add_item)
    monkeypatch.setattr(scheduled_messages, "finish_job", finish_job)

    async def scenario() -> None:
        service = ScheduledMessageService(lambda: SessionContext(), SimpleNamespace())
        claim = await service._claim_due(scheduled_for.replace(second=0) + scheduled_messages.timedelta(seconds=91))

        assert claim == (7, 4, 3, "@examplebot", "/checkin", scheduled_for, True)
        assert rule.next_run_at == next_run
        assert add_item.await_args.args[4] == "skipped"
        assert finish_job.await_args.args[2] == "skipped"
        daily_run = session.add.call_args.args[0]
        assert isinstance(daily_run, ScheduledMessageDailyRun)
        assert daily_run.claimed_count == 1
        assert daily_run.completed_count == 1
        assert daily_run.skipped_count == 1
        assert create_job.await_args.args[2]["master_rule_id"] == 4
        session.commit.assert_awaited_once()

    asyncio.run(scenario())


def test_daily_summary_is_sent_once_after_rule_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_run = SimpleNamespace(
        id=12,
        rule_id=4,
        run_date=date(2026, 11, 2),
        claimed_count=4,
        completed_count=4,
        succeeded_count=2,
        failed_count=1,
        skipped_count=1,
        notified_at=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=daily_run),
        scalar=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScheduledMessageService(
        lambda: SessionContext(),
        SimpleNamespace(bot=bot),
    )
    monkeypatch.setattr(scheduled_messages.settings, "admin_ids", [99])

    asyncio.run(service._notify_daily_run(12))

    bot.send_message.assert_awaited_once_with(
        99,
        "自动发送规则 #4 · 2026-11-02\n"
        "当日执行完成\n成功 2/4\n失败 1 · 跳过 1",
    )
    assert daily_run.notified_at is not None
    session.commit.assert_awaited_once()

    asyncio.run(service._notify_daily_run(12))
    assert bot.send_message.await_count == 1


def test_daily_summary_waits_while_same_rule_has_pending_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_run = SimpleNamespace(
        id=12,
        rule_id=4,
        run_date=date(2026, 11, 2),
        claimed_count=1,
        completed_count=1,
        succeeded_count=1,
        failed_count=0,
        skipped_count=0,
        notified_at=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=daily_run),
        scalar=AsyncMock(return_value=31),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScheduledMessageService(
        lambda: SessionContext(),
        SimpleNamespace(bot=bot),
    )
    monkeypatch.setattr(scheduled_messages.settings, "admin_ids", [99])

    asyncio.run(service._notify_daily_run(12))

    bot.send_message.assert_not_awaited()
    session.commit.assert_not_awaited()
    assert daily_run.notified_at is None


def test_multiple_due_accounts_trigger_only_one_summary_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = AsyncMock(
        side_effect=[
            SimpleNamespace(failed=0),
            SimpleNamespace(failed=1),
        ]
    )
    monkeypatch.setattr(scheduled_messages, "run_message_job", send)
    scheduled_for = datetime(2026, 11, 2, tzinfo=UTC)

    async def scenario() -> None:
        service = ScheduledMessageService(SimpleNamespace(), SimpleNamespace())
        service._claim_due = AsyncMock(
            side_effect=[
                (7, 4, 3, "@examplebot", "/checkin", scheduled_for, False),
                (8, 4, 5, "@examplebot", "/checkin", scheduled_for, False),
                None,
            ]
        )
        service._complete_daily_run = AsyncMock()
        service._notify_completed_daily_runs = AsyncMock()

        assert await service.process_due(scheduled_for) == 2
        assert service._complete_daily_run.await_args_list[0].args == (
            4,
            date(2026, 11, 2),
            "succeeded",
        )
        assert service._complete_daily_run.await_args_list[1].args == (
            4,
            date(2026, 11, 2),
            "failed",
        )
        service._notify_completed_daily_runs.assert_awaited_once()

    asyncio.run(scenario())


def test_setup_failure_is_recorded_in_job_and_daily_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_for = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    daily_run = SimpleNamespace(completed_count=0, failed_count=0)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=daily_run),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    job = SimpleNamespace(id=91)
    create_job = AsyncMock(return_value=job)
    add_item = AsyncMock()
    finish_job = AsyncMock()
    monkeypatch.setattr(scheduled_messages, "create_job", create_job)
    monkeypatch.setattr(scheduled_messages, "add_job_item", add_item)
    monkeypatch.setattr(scheduled_messages, "finish_job", finish_job)

    service = ScheduledMessageService(lambda: SessionContext(), SimpleNamespace())
    asyncio.run(
        service._record_setup_failure(
            7,
            4,
            3,
            "@examplebot",
            "/checkin",
            scheduled_for,
            RuntimeError("connection failed"),
        )
    )

    assert create_job.await_args.args[2]["master_rule_id"] == 4
    assert add_item.await_args.args[4] == "failed"
    assert add_item.await_args.kwargs["error"] == "connection failed"
    finish_job.assert_awaited_once_with(
        session,
        job,
        "finished_with_errors",
        "connection failed",
    )
    assert daily_run.completed_count == 1
    assert daily_run.failed_count == 1
    session.commit.assert_awaited_once()


def test_restart_recovers_incomplete_daily_runs_as_failed() -> None:
    first = SimpleNamespace(
        claimed_count=4,
        completed_count=2,
        failed_count=1,
    )
    second = SimpleNamespace(
        claimed_count=1,
        completed_count=0,
        failed_count=0,
    )
    scalar_result = SimpleNamespace(all=lambda: [first, second])
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=scalar_result),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    service = ScheduledMessageService(lambda: SessionContext(), SimpleNamespace())
    asyncio.run(service._recover_incomplete_daily_runs())

    assert (first.completed_count, first.failed_count) == (4, 3)
    assert (second.completed_count, second.failed_count) == (1, 1)
    session.commit.assert_awaited_once()


def test_failed_daily_summary_delivery_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_run = SimpleNamespace(
        id=12,
        rule_id=4,
        run_date=date(2026, 11, 2),
        claimed_count=1,
        completed_count=1,
        succeeded_count=1,
        failed_count=0,
        skipped_count=0,
        notified_at=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=daily_run),
        scalar=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
    service = ScheduledMessageService(
        lambda: SessionContext(),
        SimpleNamespace(bot=bot),
    )
    monkeypatch.setattr(scheduled_messages.settings, "admin_ids", [99])

    asyncio.run(service._notify_daily_run(12))

    bot.send_message.assert_awaited_once()
    assert daily_run.notified_at is None
    session.commit.assert_not_awaited()
