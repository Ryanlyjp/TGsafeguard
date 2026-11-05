from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Admin,
    AllowedTarget,
    Job,
    JobItem,
    ScheduledMessage,
    ScheduledMessageDailyRun,
    ScheduledMessageRule,
    TgAccount,
    TgSession,
)
from app.config import settings
from app.services.audit import audit
from app.services.jobs import add_job_item, create_job, finish_job
from app.services.rate_limit import RateGate, get_rate
from app.services.targets import canonicalize_target_ref
from app.tg import batch_ops
from app.tg.client_pool import ClientPool

logger = logging.getLogger(__name__)
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")
SCHEDULE_INTERVAL_SECONDS = 15
SCHEDULE_GRACE_SECONDS = 90
MAX_SCHEDULES_PER_ACCOUNT = 50


@dataclass(frozen=True)
class MessageJobResult:
    job_id: int
    succeeded: int
    failed: int


def validate_bot_target(target_ref: str) -> str:
    normalized = canonicalize_target_ref(target_ref)
    username = normalized.removeprefix("@")
    if not normalized.startswith("@") or not 5 <= len(username) <= 32:
        raise ValueError("目标 Bot 必须填写 @username")
    if not username[0].isalpha() or not all(char.isalnum() or char == "_" for char in username):
        raise ValueError("目标 Bot 用户名格式无效")
    return normalized


def validate_message_text(message_text: str) -> str:
    text = message_text.strip()
    if not text:
        raise ValueError("消息内容不能为空")
    if len(text) > 4096:
        raise ValueError("消息内容不能超过 4096 个字符")
    return text


def validate_schedule_values(hour: int, minute: int, random_offset_minutes: int) -> int:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("时间必须是 00:00 到 23:59")
    if not 0 <= random_offset_minutes <= 720:
        raise ValueError("随机范围必须是 0 到 720 分钟")
    return hour * 60 + minute


def choose_run_at(
    run_date: date,
    base_minute: int,
    random_offset_minutes: int,
    *,
    randint=random.randint,
) -> datetime:
    lower = max(0, base_minute - random_offset_minutes)
    upper = min(1439, base_minute + random_offset_minutes)
    selected_minute = randint(lower, upper)
    local_midnight = datetime.combine(run_date, datetime.min.time(), tzinfo=SINGAPORE_TZ)
    return (local_midnight + timedelta(minutes=selected_minute)).astimezone(UTC)


def next_singapore_date(now: datetime | None = None) -> date:
    current = (now or datetime.now(UTC)).astimezone(SINGAPORE_TZ)
    return current.date() + timedelta(days=1)


def singapore_day_bounds(run_date: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(run_date, datetime.min.time(), tzinfo=SINGAPORE_TZ)
    return local_start.astimezone(UTC), (local_start + timedelta(days=1)).astimezone(UTC)


async def ensure_bot_target(session: AsyncSession, target_ref: str) -> str:
    normalized = validate_bot_target(target_ref)
    exists = await session.scalar(
        select(AllowedTarget.id).where(func.lower(AllowedTarget.target_ref) == normalized.lower())
    )
    if exists is None:
        session.add(
            AllowedTarget(
                target_type="bot",
                target_ref=normalized,
                title="消息 Bot",
            )
        )
    return normalized


async def active_account_ids(session: AsyncSession, account_ids: list[int]) -> list[int]:
    unique_ids = list(dict.fromkeys(account_ids))
    if not unique_ids or len(unique_ids) > 200:
        raise ValueError("请选择 1 到 200 个账号")
    rows = await session.scalars(
        select(TgAccount.id)
        .join(TgSession, TgSession.account_id == TgAccount.id)
        .where(TgAccount.id.in_(unique_ids), TgSession.is_active.is_(True))
    )
    active = set(rows.all())
    missing = [account_id for account_id in unique_ids if account_id not in active]
    if missing:
        raise ValueError(f"账号不可用：{','.join(str(value) for value in missing[:10])}")
    return unique_ids


async def run_message_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
    account_ids: list[int],
    target_ref: str,
    message_text: str,
    *,
    job_type: str,
    admin_telegram_user_id: int | None = None,
    schedule_id: int | None = None,
    master_rule_id: int | None = None,
    scheduled_for: datetime | None = None,
) -> MessageJobResult:
    async with sessionmaker() as session:
        accounts = await active_account_ids(session, account_ids)
        target = await ensure_bot_target(session, target_ref)
        text = validate_message_text(message_text)
        rate = await get_rate(session, "batch")
        params = {
            "target": target,
            "accounts": accounts,
            "text": text,
            "source": job_type,
        }
        if schedule_id is not None:
            params["schedule_id"] = schedule_id
        if master_rule_id is not None:
            params["master_rule_id"] = master_rule_id
        if scheduled_for is not None:
            params["scheduled_for"] = scheduled_for.isoformat()
        job = await create_job(session, job_type, params)
        admin = None
        if admin_telegram_user_id is not None:
            admin = await session.scalar(
                select(Admin).where(Admin.telegram_user_id == admin_telegram_user_id)
            )
        await audit(
            session,
            admin,
            job_type,
            "target",
            target,
            {"accounts": accounts, "schedule_id": schedule_id},
        )
        await session.commit()
        job_id = job.id

    gate = RateGate(rate)
    succeeded = failed = 0
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise RuntimeError("发送任务不存在")
        for account_id in accounts:
            await gate.wait()
            try:
                result = await batch_ops.send_message(client_pool, account_id, target, text)
                await add_job_item(session, job, account_id, target, "ok", result=result)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001 - one account must not abort the batch
                await add_job_item(session, job, account_id, target, "failed", error=str(exc))
                failed += 1
            await session.commit()
        await finish_job(session, job, "finished_with_errors" if failed else "finished")
        await session.commit()
    return MessageJobResult(job_id=job_id, succeeded=succeeded, failed=failed)


async def create_schedules(
    session: AsyncSession,
    account_ids: list[int],
    target_ref: str,
    message_text: str,
    hour: int,
    minute: int,
    random_offset_minutes: int,
    *,
    now: datetime | None = None,
) -> list[ScheduledMessage]:
    accounts = await active_account_ids(session, account_ids)
    target = await ensure_bot_target(session, target_ref)
    text = validate_message_text(message_text)
    base_minute = validate_schedule_values(hour, minute, random_offset_minutes)
    counts = dict(
        (
            await session.execute(
                select(ScheduledMessage.account_id, func.count())
                .where(ScheduledMessage.account_id.in_(accounts))
                .group_by(ScheduledMessage.account_id)
            )
        ).all()
    )
    full = [account_id for account_id in accounts if counts.get(account_id, 0) >= MAX_SCHEDULES_PER_ACCOUNT]
    if full:
        raise ValueError(f"账号自动发送规则已达上限：{','.join(str(value) for value in full)}")
    run_date = next_singapore_date(now)
    master_rule = ScheduledMessageRule(
        target_ref=target,
        message_text=text,
        base_minute=base_minute,
        random_offset_minutes=random_offset_minutes,
        timezone="Asia/Singapore",
        enabled=True,
    )
    session.add(master_rule)
    await session.flush()
    rules = []
    for account_id in accounts:
        rule = ScheduledMessage(
            rule_id=master_rule.id,
            account_id=account_id,
            target_ref=target,
            message_text=text,
            base_minute=base_minute,
            random_offset_minutes=random_offset_minutes,
            timezone="Asia/Singapore",
            enabled=True,
            next_run_at=choose_run_at(run_date, base_minute, random_offset_minutes),
        )
        session.add(rule)
        rules.append(rule)
    await session.flush()
    return rules


async def update_rule_template(
    master_rule: ScheduledMessageRule,
    target_ref: str,
    message_text: str,
    hour: int,
    minute: int,
    random_offset_minutes: int,
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[ScheduledMessage]:
    target = await ensure_bot_target(session, target_ref)
    text = validate_message_text(message_text)
    base_minute = validate_schedule_values(hour, minute, random_offset_minutes)
    master_rule.target_ref = target
    master_rule.message_text = text
    master_rule.base_minute = base_minute
    master_rule.random_offset_minutes = random_offset_minutes
    assignments = list(
        (
            await session.scalars(
                select(ScheduledMessage)
                .where(ScheduledMessage.rule_id == master_rule.id)
                .order_by(ScheduledMessage.account_id)
            )
        ).all()
    )
    run_date = next_singapore_date(now)
    for assignment in assignments:
        assignment.target_ref = target
        assignment.message_text = text
        assignment.base_minute = base_minute
        assignment.random_offset_minutes = random_offset_minutes
        assignment.timezone = master_rule.timezone
        assignment.enabled = master_rule.enabled
        assignment.next_run_at = choose_run_at(
            run_date,
            base_minute,
            random_offset_minutes,
        )
    return assignments


async def set_rule_template_enabled(
    master_rule: ScheduledMessageRule,
    enabled: bool,
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[ScheduledMessage]:
    master_rule.enabled = enabled
    assignments = list(
        (
            await session.scalars(
                select(ScheduledMessage)
                .where(ScheduledMessage.rule_id == master_rule.id)
                .order_by(ScheduledMessage.account_id)
            )
        ).all()
    )
    run_date = next_singapore_date(now)
    for assignment in assignments:
        assignment.enabled = enabled
        if enabled:
            assignment.next_run_at = choose_run_at(
                run_date,
                master_rule.base_minute,
                master_rule.random_offset_minutes,
            )
    return assignments


async def apply_rule_accounts(
    master_rule: ScheduledMessageRule,
    account_ids: list[int],
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[ScheduledMessage]:
    accounts = await active_account_ids(session, account_ids)
    existing = list(
        (
            await session.scalars(
                select(ScheduledMessage)
                .where(ScheduledMessage.rule_id == master_rule.id)
                .order_by(ScheduledMessage.account_id)
            )
        ).all()
    )
    existing_by_account = {assignment.account_id: assignment for assignment in existing}
    selected = set(accounts)
    added_ids = [account_id for account_id in accounts if account_id not in existing_by_account]
    if added_ids:
        counts = dict(
            (
                await session.execute(
                    select(ScheduledMessage.account_id, func.count())
                    .where(ScheduledMessage.account_id.in_(added_ids))
                    .group_by(ScheduledMessage.account_id)
                )
            ).all()
        )
        full = [
            account_id
            for account_id in added_ids
            if counts.get(account_id, 0) >= MAX_SCHEDULES_PER_ACCOUNT
        ]
        if full:
            raise ValueError(
                f"账号自动发送规则已达上限：{','.join(str(value) for value in full)}"
            )
    for assignment in existing:
        if assignment.account_id not in selected:
            await session.delete(assignment)
    run_date = next_singapore_date(now)
    result = [
        existing_by_account[account_id]
        for account_id in accounts
        if account_id in existing_by_account
    ]
    for account_id in added_ids:
        assignment = ScheduledMessage(
            rule_id=master_rule.id,
            account_id=account_id,
            target_ref=master_rule.target_ref,
            message_text=master_rule.message_text,
            base_minute=master_rule.base_minute,
            random_offset_minutes=master_rule.random_offset_minutes,
            timezone=master_rule.timezone,
            enabled=master_rule.enabled,
            next_run_at=choose_run_at(
                run_date,
                master_rule.base_minute,
                master_rule.random_offset_minutes,
            ),
        )
        session.add(assignment)
        result.append(assignment)
    await session.flush()
    return result


async def update_schedule(
    rule: ScheduledMessage,
    target_ref: str,
    message_text: str,
    hour: int,
    minute: int,
    random_offset_minutes: int,
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> None:
    rule.target_ref = await ensure_bot_target(session, target_ref)
    rule.message_text = validate_message_text(message_text)
    rule.base_minute = validate_schedule_values(hour, minute, random_offset_minutes)
    rule.random_offset_minutes = random_offset_minutes
    rule.next_run_at = choose_run_at(
        next_singapore_date(now),
        rule.base_minute,
        random_offset_minutes,
    )


class ScheduledMessageService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client_pool: ClientPool,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.client_pool = client_pool
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        await self._recover_incomplete_daily_runs()
        self._task = asyncio.create_task(self._loop(), name="scheduled-message-service")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.process_due()
            except Exception:
                logger.exception("Scheduled message processing failed")
            await asyncio.sleep(SCHEDULE_INTERVAL_SECONDS)

    async def process_due(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        processed = 0
        while claim := await self._claim_due(current):
            processed += 1
            (
                schedule_id,
                master_rule_id,
                account_id,
                target,
                text,
                scheduled_for,
                missed,
            ) = claim
            if missed:
                continue
            try:
                result = await run_message_job(
                    self.sessionmaker,
                    self.client_pool,
                    [account_id],
                    target,
                    text,
                    job_type="scheduled_send",
                    schedule_id=schedule_id,
                    master_rule_id=master_rule_id,
                    scheduled_for=scheduled_for,
                )
                await self._complete_daily_run(
                    master_rule_id,
                    scheduled_for.astimezone(SINGAPORE_TZ).date(),
                    "failed" if result.failed else "succeeded",
                )
            except Exception as exc:
                logger.exception("Scheduled message %s failed", schedule_id)
                await self._record_setup_failure(
                    schedule_id,
                    master_rule_id,
                    account_id,
                    target,
                    text,
                    scheduled_for,
                    exc,
                )
        await self._notify_completed_daily_runs()
        return processed

    async def _claim_due(
        self,
        now: datetime,
    ) -> tuple[int, int, int, str, str, datetime, bool] | None:
        async with self.sessionmaker() as session:
            rule = await session.scalar(
                select(ScheduledMessage)
                .where(
                    ScheduledMessage.enabled.is_(True),
                    ScheduledMessage.next_run_at <= now,
                )
                .order_by(ScheduledMessage.next_run_at, ScheduledMessage.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if rule is None:
                return None
            scheduled_for = rule.next_run_at
            scheduled_date = scheduled_for.astimezone(SINGAPORE_TZ).date()
            daily_run = await session.scalar(
                select(ScheduledMessageDailyRun)
                .where(
                    ScheduledMessageDailyRun.rule_id == rule.rule_id,
                    ScheduledMessageDailyRun.run_date == scheduled_date,
                )
                .with_for_update()
            )
            if daily_run is None:
                daily_run = ScheduledMessageDailyRun(
                    rule_id=rule.rule_id,
                    run_date=scheduled_date,
                    claimed_count=0,
                    completed_count=0,
                    succeeded_count=0,
                    failed_count=0,
                    skipped_count=0,
                )
                session.add(daily_run)
                await session.flush()
            daily_run.claimed_count += 1
            rule.next_run_at = choose_run_at(
                scheduled_date + timedelta(days=1),
                rule.base_minute,
                rule.random_offset_minutes,
            )
            missed = (now - scheduled_for).total_seconds() > SCHEDULE_GRACE_SECONDS
            if missed:
                job = await create_job(
                    session,
                    "scheduled_send",
                    {
                        "target": rule.target_ref,
                        "accounts": [rule.account_id],
                        "text": rule.message_text,
                        "source": "scheduled_send",
                        "schedule_id": rule.id,
                        "master_rule_id": rule.rule_id,
                        "scheduled_for": scheduled_for.isoformat(),
                    },
                )
                await add_job_item(
                    session,
                    job,
                    rule.account_id,
                    rule.target_ref,
                    "skipped",
                    error="服务错过计划时间，已按设置跳过",
                )
                await finish_job(session, job, "skipped", "服务错过计划时间，已跳过")
                daily_run.completed_count += 1
                daily_run.skipped_count += 1
            await session.commit()
            return (
                rule.id,
                rule.rule_id,
                rule.account_id,
                rule.target_ref,
                rule.message_text,
                scheduled_for,
                missed,
            )

    async def _complete_daily_run(
        self,
        master_rule_id: int,
        run_date: date,
        outcome: str,
    ) -> None:
        async with self.sessionmaker() as session:
            daily_run = await session.scalar(
                select(ScheduledMessageDailyRun)
                .where(
                    ScheduledMessageDailyRun.rule_id == master_rule_id,
                    ScheduledMessageDailyRun.run_date == run_date,
                )
                .with_for_update()
            )
            if daily_run is None:
                raise RuntimeError("自动发送每日汇总记录不存在")
            if daily_run.completed_count >= daily_run.claimed_count:
                logger.warning(
                    "Ignoring duplicate scheduled completion for master rule %s on %s",
                    master_rule_id,
                    run_date,
                )
                return
            daily_run.completed_count += 1
            if outcome == "succeeded":
                daily_run.succeeded_count += 1
            elif outcome == "failed":
                daily_run.failed_count += 1
            else:
                raise ValueError(f"未知自动发送结果：{outcome}")
            await session.commit()

    async def _record_setup_failure(
        self,
        schedule_id: int,
        master_rule_id: int,
        account_id: int,
        target: str,
        text: str,
        scheduled_for: datetime,
        error: Exception,
    ) -> None:
        error_text = str(error)[:2000] or type(error).__name__
        async with self.sessionmaker() as session:
            job = await create_job(
                session,
                "scheduled_send",
                {
                    "target": target,
                    "accounts": [account_id],
                    "text": text,
                    "source": "scheduled_send",
                    "schedule_id": schedule_id,
                    "master_rule_id": master_rule_id,
                    "scheduled_for": scheduled_for.isoformat(),
                },
            )
            await add_job_item(
                session,
                job,
                account_id,
                target,
                "failed",
                error=error_text,
            )
            await finish_job(session, job, "finished_with_errors", error_text)
            daily_run = await session.scalar(
                select(ScheduledMessageDailyRun)
                .where(
                    ScheduledMessageDailyRun.rule_id == master_rule_id,
                    ScheduledMessageDailyRun.run_date
                    == scheduled_for.astimezone(SINGAPORE_TZ).date(),
                )
                .with_for_update()
            )
            if daily_run is None:
                raise RuntimeError("自动发送每日汇总记录不存在")
            daily_run.completed_count += 1
            daily_run.failed_count += 1
            await session.commit()

    async def _recover_incomplete_daily_runs(self) -> None:
        async with self.sessionmaker() as session:
            runs = list(
                (
                    await session.scalars(
                        select(ScheduledMessageDailyRun)
                        .where(
                            ScheduledMessageDailyRun.notified_at.is_(None),
                            ScheduledMessageDailyRun.completed_count
                            < ScheduledMessageDailyRun.claimed_count,
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for daily_run in runs:
                interrupted = daily_run.claimed_count - daily_run.completed_count
                daily_run.completed_count += interrupted
                daily_run.failed_count += interrupted
            if runs:
                await session.commit()
                logger.warning(
                    "Recovered %s incomplete automatic-send daily reports as failed",
                    len(runs),
                )

    async def _notify_completed_daily_runs(self) -> None:
        async with self.sessionmaker() as session:
            run_ids = list(
                (
                    await session.scalars(
                        select(ScheduledMessageDailyRun.id)
                        .where(
                            ScheduledMessageDailyRun.notified_at.is_(None),
                            ScheduledMessageDailyRun.claimed_count > 0,
                            ScheduledMessageDailyRun.completed_count
                            == ScheduledMessageDailyRun.claimed_count,
                        )
                        .order_by(
                            ScheduledMessageDailyRun.run_date,
                            ScheduledMessageDailyRun.rule_id,
                        )
                    )
                ).all()
            )
        for run_id in run_ids:
            await self._notify_daily_run(run_id)

    async def _notify_daily_run(self, run_id: int) -> None:
        async with self.sessionmaker() as session:
            daily_run = await session.get(
                ScheduledMessageDailyRun,
                run_id,
                with_for_update=True,
            )
            if daily_run is None or daily_run.notified_at is not None:
                return
            day_start, day_end = singapore_day_bounds(daily_run.run_date)
            pending = await session.scalar(
                select(ScheduledMessage.id)
                .where(
                    ScheduledMessage.rule_id == daily_run.rule_id,
                    ScheduledMessage.enabled.is_(True),
                    ScheduledMessage.next_run_at >= day_start,
                    ScheduledMessage.next_run_at < day_end,
                )
                .limit(1)
            )
            if pending is not None:
                return
            text = (
                f"自动发送规则 #{daily_run.rule_id} · {daily_run.run_date:%Y-%m-%d}\n"
                f"当日执行完成\n成功 {daily_run.succeeded_count}/{daily_run.claimed_count}\n"
                f"失败 {daily_run.failed_count} · 跳过 {daily_run.skipped_count}"
            )
            delivered = False
            for admin_id in settings.admin_ids:
                try:
                    await self.client_pool.bot.send_message(admin_id, text)
                    delivered = True
                except Exception:
                    logger.exception(
                        "Failed to notify admin %s for automatic-send daily run %s",
                        admin_id,
                        run_id,
                    )
            if delivered:
                daily_run.notified_at = datetime.now(UTC)
                await session.commit()
