from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.auth import AdminOnlyMiddleware
from app.bot.interaction_cleanup import (
    finish_message_flow,
    reset_message_flow,
    tracked_answer,
)
from app.bot.keyboards import cancel_inline, force_reply, main_menu
from app.bot.message_keyboards import (
    account_picker_panel,
    confirm_panel,
    hour_panel,
    master_rule_detail_panel,
    master_schedules_panel,
    message_history_panel,
    message_home_panel,
    minute_panel,
    offset_panel,
    rule_detail_panel,
    schedules_panel,
)
from app.db.models import (
    Admin,
    Job,
    JobItem,
    ScheduledMessage,
    ScheduledMessageRule,
    TgAccount,
    TgSession,
)
from app.services.audit import audit
from app.services.pagination import account_page_window
from app.services.scheduled_messages import (
    SINGAPORE_TZ,
    apply_rule_accounts,
    create_schedules,
    run_message_job,
    set_rule_template_enabled,
    update_rule_template,
    update_schedule,
    validate_bot_target,
    validate_message_text,
    validate_schedule_values,
)
from app.tg.client_pool import ClientPool

router = Router()
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())
PICKER_PAGE_SIZE = 8
RULE_PAGE_SIZE = 10


class ManualMessageFlow(StatesGroup):
    target = State()
    text = State()


class ScheduleCreateFlow(StatesGroup):
    target = State()
    text = State()
    minute = State()
    offset = State()


class ScheduleEditFlow(StatesGroup):
    value = State()
    minute = State()


async def _edit(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(text, reply_markup=reply_markup)


async def _active_account_page(
    session: AsyncSession, page: int
) -> tuple[list[TgAccount], int, int]:
    base = (
        select(TgAccount)
        .join(TgSession, TgSession.account_id == TgAccount.id)
        .where(TgSession.is_active.is_(True))
    )
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    current, pages, offset = account_page_window(
        total, page, page_size=PICKER_PAGE_SIZE
    )
    accounts = list(
        (
            await session.scalars(
                base.order_by(TgAccount.id).offset(offset).limit(PICKER_PAGE_SIZE)
            )
        ).all()
    )
    return accounts, current, pages


async def _all_active_account_ids(session: AsyncSession) -> list[int]:
    rows = await session.scalars(
        select(TgAccount.id)
        .join(TgSession, TgSession.account_id == TgAccount.id)
        .where(TgSession.is_active.is_(True))
        .order_by(TgAccount.id)
    )
    return list(rows.all())


async def _audit_rule_action(
    session: AsyncSession,
    telegram_user_id: int,
    action: str,
    rule_id: int,
    payload: dict | None = None,
) -> None:
    admin = await session.scalar(
        select(Admin).where(Admin.telegram_user_id == telegram_user_id)
    )
    await audit(
        session,
        admin,
        f"scheduled_message_{action}",
        "scheduled_message",
        str(rule_id),
        payload,
    )


async def _show_picker(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    mode: str,
    page: int = 1,
) -> None:
    data = await state.get_data()
    selected = {int(value) for value in data.get("selected_account_ids", [])}
    async with sessionmaker() as session:
        accounts, current, pages = await _active_account_page(session, page)
    await _edit(
        callback,
        f"选择账号（已选 {len(selected)} 个）",
        account_picker_panel(accounts, selected, mode, current, pages),
    )


async def _begin_target(message: Message, state: FSMContext, mode: str) -> None:
    await state.set_state(ManualMessageFlow.target if mode == "send" else ScheduleCreateFlow.target)
    await tracked_answer(
        message,
        state,
        "请输入目标 Bot 用户名，例如 @example_bot",
        reply_markup=force_reply("@目标Bot"),
    )
    await tracked_answer(message, state, "可以随时取消。", reply_markup=cancel_inline())


@router.message(Command("messages", "auto_send"))
@router.message(F.text.in_({"发送消息", "自动发送"}))
async def message_menu(message: Message, state: FSMContext) -> None:
    sent = await message.answer("消息发送与自动发送", reply_markup=message_home_panel())
    await reset_message_flow(state, message, sent)


@router.callback_query(F.data == "msgmenu:home")
async def message_menu_callback(callback: CallbackQuery) -> None:
    await _edit(callback, "消息发送与自动发送", message_home_panel())


@router.callback_query(F.data.startswith("msgsend:start:"))
@router.callback_query(F.data.startswith("msgsched:start:"))
async def start_message_flow(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    mode = "send" if parts[0] == "msgsend" else "schedule"
    target = parts[2]
    await reset_message_flow(state, callback.message, preserve_if_tracked=True)
    if target == "multi":
        await state.update_data(flow_mode=mode, selected_account_ids=[])
        await _show_picker(callback, state, sessionmaker, mode)
        return
    account_id = int(target)
    await state.update_data(flow_mode=mode, selected_account_ids=[account_id])
    await callback.answer()
    if callback.message:
        await _begin_target(callback.message, state, mode)


@router.callback_query(F.data.startswith("msgpick:"))
async def account_picker_callback(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    mode, action = parts[1], parts[2]
    data = await state.get_data()
    selected = {int(value) for value in data.get("selected_account_ids", [])}
    if action == "toggle":
        account_id, page = int(parts[3]), int(parts[4])
        if account_id in selected:
            selected.remove(account_id)
        else:
            selected.add(account_id)
        await state.update_data(selected_account_ids=sorted(selected))
        await _show_picker(callback, state, sessionmaker, mode, page)
    elif action == "page":
        await _show_picker(callback, state, sessionmaker, mode, int(parts[3]))
    elif action == "all":
        async with sessionmaker() as session:
            selected = set(await _all_active_account_ids(session))
        await state.update_data(selected_account_ids=sorted(selected))
        await _show_picker(callback, state, sessionmaker, mode, int(parts[3]))
    elif action == "none":
        await state.update_data(selected_account_ids=[])
        await _show_picker(callback, state, sessionmaker, mode, int(parts[3]))
    elif action == "done":
        if not selected:
            await callback.answer("请至少选择一个账号", show_alert=True)
            return
        if mode.startswith("master-"):
            master_id = int(mode.split("-", 1)[1])
            try:
                async with sessionmaker() as session:
                    master_rule = await session.get(ScheduledMessageRule, master_id)
                    if master_rule is None:
                        await callback.answer("主规则不存在", show_alert=True)
                        return
                    assignments = await apply_rule_accounts(
                        master_rule,
                        sorted(selected),
                        session,
                    )
                    await _audit_rule_action(
                        session,
                        callback.from_user.id,
                        "accounts",
                        master_id,
                        {"account_ids": sorted(selected)},
                    )
                    await session.commit()
            except ValueError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            await state.clear()
            await _render_master_detail(
                callback,
                sessionmaker,
                master_id,
                notice=f"已应用到 {len(assignments)} 个账号",
            )
            return
        await callback.answer()
        if callback.message:
            await _begin_target(callback.message, state, mode)


@router.message(ManualMessageFlow.target)
@router.message(ScheduleCreateFlow.target)
async def receive_target(message: Message, state: FSMContext) -> None:
    try:
        target = validate_bot_target(message.text or "")
    except ValueError as exc:
        await tracked_answer(message, state, str(exc), reply_markup=force_reply("@目标Bot"))
        return
    data = await state.get_data()
    mode = data.get("flow_mode")
    await state.update_data(target=target)
    await state.set_state(ManualMessageFlow.text if mode == "send" else ScheduleCreateFlow.text)
    await tracked_answer(
        message,
        state,
        "请输入要发送的消息或指令，例如 /checkin",
        reply_markup=force_reply("消息或指令"),
    )


@router.message(ManualMessageFlow.text)
async def receive_manual_text(message: Message, state: FSMContext) -> None:
    try:
        text = validate_message_text(message.text or "")
    except ValueError as exc:
        await tracked_answer(
            message, state, str(exc), reply_markup=force_reply("消息或指令")
        )
        return
    await state.update_data(message_text=text)
    data = await state.get_data()
    await tracked_answer(
        message,
        state,
        f"确认发送？\n账号：{len(data['selected_account_ids'])} 个\n目标：{data['target']}\n内容：{text}",
        reply_markup=confirm_panel("msgsend:confirm"),
    )


@router.callback_query(F.data == "msgsend:confirm")
async def confirm_manual_send(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    data = await state.get_data()
    await callback.answer("正在发送")
    try:
        result = await run_message_job(
            sessionmaker,
            client_pool,
            [int(value) for value in data["selected_account_ids"]],
            data["target"],
            data["message_text"],
            job_type="manual_send",
            admin_telegram_user_id=callback.from_user.id,
        )
    except ValueError as exc:
        if callback.message:
            await callback.message.answer(f"发送失败：{exc}", reply_markup=message_home_panel())
        return
    if callback.message:
        await finish_message_flow(
            state,
            callback.bot,
            callback.message.chat.id,
            callback.message,
        )
        await callback.message.answer(
            f"发送完成：成功 {result.succeeded}，失败 {result.failed}\n任务 #{result.job_id}",
            reply_markup=message_home_panel(),
        )
    else:
        await state.clear()


@router.message(ScheduleCreateFlow.text)
async def receive_schedule_text(message: Message, state: FSMContext) -> None:
    try:
        text = validate_message_text(message.text or "")
    except ValueError as exc:
        await tracked_answer(
            message, state, str(exc), reply_markup=force_reply("消息或指令")
        )
        return
    await state.update_data(message_text=text)
    await tracked_answer(
        message,
        state,
        "选择每天的基准小时（新加坡时间）",
        reply_markup=hour_panel(),
    )


@router.callback_query(F.data.startswith("msgtime:"))
async def schedule_time_callback(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1]
    if action == "hours":
        await _edit(callback, "选择每天的基准小时（新加坡时间）", hour_panel())
    elif action == "hour":
        hour = int(parts[2])
        await state.update_data(hour=hour)
        await _edit(callback, f"已选 {hour:02d} 时，请选择分钟", minute_panel(hour))
    elif action == "minute":
        hour, minute = int(parts[2]), int(parts[3])
        await state.update_data(hour=hour, minute=minute)
        await _edit(callback, f"基准时间 {hour:02d}:{minute:02d}\n选择前后随机范围", offset_panel())
    elif action == "minute_custom":
        await state.update_data(hour=int(parts[2]))
        await state.set_state(ScheduleCreateFlow.minute)
        await callback.answer()
        if callback.message:
            await tracked_answer(
                callback.message,
                state,
                "输入分钟数（0–59）",
                reply_markup=force_reply("0-59"),
            )


@router.message(ScheduleCreateFlow.minute)
async def receive_custom_minute(message: Message, state: FSMContext) -> None:
    try:
        minute = int(message.text or "")
        if not 0 <= minute <= 59:
            raise ValueError
    except ValueError:
        await tracked_answer(
            message, state, "分钟必须是 0 到 59", reply_markup=force_reply("0-59")
        )
        return
    await state.update_data(minute=minute)
    await tracked_answer(message, state, "选择前后随机范围", reply_markup=offset_panel())


async def _show_schedule_confirmation(message: Message, state: FSMContext, offset: int) -> None:
    data = await state.get_data()
    validate_schedule_values(int(data["hour"]), int(data["minute"]), offset)
    await state.update_data(random_offset_minutes=offset)
    await tracked_answer(
        message,
        state,
        f"确认创建自动发送？\n账号：{len(data['selected_account_ids'])} 个\n目标：{data['target']}\n指令：{data['message_text']}\n时间：{int(data['hour']):02d}:{int(data['minute']):02d} ±{offset} 分钟\n时区：新加坡",
        reply_markup=confirm_panel("msgsched:confirm"),
    )


@router.callback_query(F.data.startswith("msgoffset:"))
async def schedule_offset_callback(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if parts[1] == "set":
        await callback.answer()
        if callback.message:
            await _show_schedule_confirmation(callback.message, state, int(parts[2]))
    else:
        await state.set_state(ScheduleCreateFlow.offset)
        await callback.answer()
        if callback.message:
            await tracked_answer(
                callback.message,
                state,
                "输入随机分钟数（0–720）",
                reply_markup=force_reply("0-720"),
            )


@router.message(ScheduleCreateFlow.offset)
async def receive_custom_offset(message: Message, state: FSMContext) -> None:
    try:
        offset = int(message.text or "")
        if not 0 <= offset <= 720:
            raise ValueError
    except ValueError:
        await tracked_answer(
            message,
            state,
            "随机分钟必须是 0 到 720",
            reply_markup=force_reply("0-720"),
        )
        return
    await _show_schedule_confirmation(message, state, offset)


@router.callback_query(F.data == "msgsched:confirm")
async def confirm_schedule(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    try:
        async with sessionmaker() as session:
            rules = await create_schedules(
                session,
                [int(value) for value in data["selected_account_ids"]],
                data["target"],
                data["message_text"],
                int(data["hour"]),
                int(data["minute"]),
                int(data["random_offset_minutes"]),
            )
            admin = await session.scalar(
                select(Admin).where(Admin.telegram_user_id == callback.from_user.id)
            )
            await audit(
                session,
                admin,
                "scheduled_message_create",
                "scheduled_message_rule",
                str(rules[0].rule_id),
                {"account_ids": data["selected_account_ids"], "count": len(rules)},
            )
            await session.commit()
    except ValueError as exc:
        await callback.answer("创建失败", show_alert=True)
        if callback.message:
            await callback.message.answer(f"创建失败：{exc}", reply_markup=message_home_panel())
        return
    await callback.answer("创建完成")
    if callback.message:
        await finish_message_flow(
            state,
            callback.bot,
            callback.message.chat.id,
            callback.message,
        )
        await callback.message.answer(
            f"已创建主规则 #{rules[0].rule_id}，应用到 {len(rules)} 个账号，从下一个新加坡自然日生效。",
            reply_markup=message_home_panel(),
        )
    else:
        await state.clear()


def _rule_text(rule: ScheduledMessage) -> str:
    next_run = rule.next_run_at.astimezone(SINGAPORE_TZ)
    return (
        f"自动发送规则 #{rule.rule_id}\n"
        f"账号：#{rule.account_id}\n目标：{rule.target_ref}\n指令：{rule.message_text}\n"
        f"基准：{rule.base_minute // 60:02d}:{rule.base_minute % 60:02d} ±{rule.random_offset_minutes} 分钟\n"
        f"状态：{'启用' if rule.enabled else '停用'}\n下次：{next_run:%Y-%m-%d %H:%M}（新加坡）"
    )


def _master_rule_text(
    rule: ScheduledMessageRule,
    accounts: list[TgAccount],
    notice: str | None = None,
) -> str:
    account_names = [
        " ".join(part for part in (account.first_name, account.last_name) if part)
        or f"账号 #{account.id}"
        for account in accounts
    ]
    lines = [
        f"主规则 #{rule.id}",
        f"目标：{rule.target_ref}",
        f"指令：{rule.message_text}",
        f"基准：{rule.base_minute // 60:02d}:{rule.base_minute % 60:02d} ±{rule.random_offset_minutes} 分钟",
        f"状态：{'启用' if rule.enabled else '停用'}",
        f"应用账号（{len(accounts)}）：{'、'.join(account_names) or '-'}",
        "账号内修改只影响该账号；再次修改主规则会覆盖全部账号设置。",
    ]
    if notice:
        lines.insert(0, notice)
    return "\n".join(lines)


async def _render_master_detail(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
    master_id: int,
    notice: str | None = None,
) -> None:
    async with sessionmaker() as session:
        master_rule = await session.get(ScheduledMessageRule, master_id)
        if master_rule is None:
            await callback.answer("主规则不存在", show_alert=True)
            return
        accounts = list(
            (
                await session.scalars(
                    select(TgAccount)
                    .join(ScheduledMessage, ScheduledMessage.account_id == TgAccount.id)
                    .where(ScheduledMessage.rule_id == master_id)
                    .order_by(TgAccount.id)
                )
            ).all()
        )
    await _edit(
        callback,
        _master_rule_text(master_rule, accounts, notice),
        master_rule_detail_panel(master_rule),
    )


@router.callback_query(F.data.startswith("msgsched:list:"))
async def list_schedules(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    raw = (callback.data or "").split(":")[2]
    account_id = None if raw == "all" else int(raw)
    parts = (callback.data or "").split(":")
    page = int(parts[3]) if len(parts) > 3 else 1
    await _render_schedule_list(callback, sessionmaker, account_id, page)


async def _render_schedule_list(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
    account_id: int | None,
    requested_page: int = 1,
) -> None:
    if account_id is None:
        async with sessionmaker() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(ScheduledMessageRule)
                )
                or 0
            )
            page, pages, offset = account_page_window(
                total, requested_page, page_size=RULE_PAGE_SIZE
            )
            rows = list(
                (
                    await session.execute(
                        select(ScheduledMessageRule, func.count(ScheduledMessage.id))
                        .outerjoin(
                            ScheduledMessage,
                            ScheduledMessage.rule_id == ScheduledMessageRule.id,
                        )
                        .group_by(ScheduledMessageRule.id)
                        .order_by(ScheduledMessageRule.id)
                        .offset(offset)
                        .limit(RULE_PAGE_SIZE)
                    )
                ).all()
            )
        await _edit(
            callback,
            f"主规则管理（共 {total} 条）",
            master_schedules_panel(rows, page, pages),
        )
        return
    async with sessionmaker() as session:
        query = (
            select(ScheduledMessage)
            .where(ScheduledMessage.account_id == account_id)
            .order_by(ScheduledMessage.rule_id, ScheduledMessage.id)
        )
        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total = int(await session.scalar(count_query) or 0)
        page, pages, offset = account_page_window(
            total, requested_page, page_size=RULE_PAGE_SIZE
        )
        rules = list(
            (await session.scalars(query.offset(offset).limit(RULE_PAGE_SIZE))).all()
        )
    await _edit(
        callback,
        f"自动发送规则（共 {total} 条）",
        schedules_panel(rules, account_id, page, pages),
    )


@router.callback_query(F.data.startswith("msgmaster:"))
async def master_rule_action(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    action, master_id = parts[1], int(parts[2])
    if action == "view":
        await _render_master_detail(callback, sessionmaker, master_id)
        return
    async with sessionmaker() as session:
        master_rule = await session.get(ScheduledMessageRule, master_id)
        if master_rule is None:
            await callback.answer("主规则不存在", show_alert=True)
            return
        if action == "toggle":
            await set_rule_template_enabled(
                master_rule,
                not master_rule.enabled,
                session,
            )
            await _audit_rule_action(
                session,
                callback.from_user.id,
                "master_toggle",
                master_id,
                {"enabled": master_rule.enabled},
            )
            await session.commit()
        elif action == "delete":
            await _audit_rule_action(
                session,
                callback.from_user.id,
                "master_delete",
                master_id,
            )
            await session.delete(master_rule)
            await session.commit()
            await _render_schedule_list(callback, sessionmaker, None)
            return
        elif action in {"target", "text"}:
            await reset_message_flow(state, callback.message)
            await state.set_state(ScheduleEditFlow.value)
            await state.update_data(
                edit_scope="master",
                edit_rule_id=master_id,
                edit_field=action,
            )
            await callback.answer()
            if callback.message:
                prompt = "输入新的目标 Bot 用户名" if action == "target" else "输入新的消息或指令"
                await tracked_answer(
                    callback.message,
                    state,
                    prompt,
                    reply_markup=force_reply(prompt),
                )
            return
        elif action == "time":
            await state.update_data(edit_scope="master", edit_rule_id=master_id)
            await _edit(
                callback,
                "选择新的基准小时（新加坡时间）",
                hour_panel(f"msgedittime:master:{master_id}"),
            )
            return
        elif action == "offset":
            await state.update_data(edit_scope="master", edit_rule_id=master_id)
            await _edit(
                callback,
                "选择新的随机范围",
                offset_panel(f"msgeditoffset:master:{master_id}"),
            )
            return
        elif action == "accounts":
            selected = set(
                (
                    await session.scalars(
                        select(ScheduledMessage.account_id).where(
                            ScheduledMessage.rule_id == master_id
                        )
                    )
                ).all()
            )
            await state.clear()
            await state.update_data(
                selected_account_ids=sorted(selected),
                edit_rule_id=master_id,
            )
            await _show_picker(
                callback,
                state,
                sessionmaker,
                f"master-{master_id}",
            )
            return
    await _render_master_detail(callback, sessionmaker, master_id)


@router.callback_query(F.data.startswith("msgrule:"))
async def rule_action(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    action, rule_id = parts[1], int(parts[2])
    async with sessionmaker() as session:
        rule = await session.get(ScheduledMessage, rule_id)
        if rule is None:
            await callback.answer("规则不存在", show_alert=True)
            return
        if action == "view":
            await _edit(callback, _rule_text(rule), rule_detail_panel(rule))
            return
        if action == "toggle":
            rule.enabled = not rule.enabled
            if rule.enabled:
                await update_schedule(
                    rule,
                    rule.target_ref,
                    rule.message_text,
                    rule.base_minute // 60,
                    rule.base_minute % 60,
                    rule.random_offset_minutes,
                    session,
                )
            await _audit_rule_action(
                session,
                callback.from_user.id,
                "toggle",
                rule.id,
                {"enabled": rule.enabled},
            )
            await session.commit()
            await _edit(callback, _rule_text(rule), rule_detail_panel(rule))
            return
        if action == "delete":
            account_id = rule.account_id
            await _audit_rule_action(session, callback.from_user.id, "delete", rule.id)
            await session.delete(rule)
            await session.commit()
            await _render_schedule_list(callback, sessionmaker, account_id)
            return
        if action in {"target", "text"}:
            await reset_message_flow(state, callback.message)
            await state.set_state(ScheduleEditFlow.value)
            await state.update_data(
                edit_scope="account",
                edit_rule_id=rule_id,
                edit_field=action,
            )
            await callback.answer()
            if callback.message:
                prompt = "输入新的目标 Bot 用户名" if action == "target" else "输入新的消息或指令"
                await tracked_answer(
                    callback.message,
                    state,
                    prompt,
                    reply_markup=force_reply(prompt),
                )
            return
        if action == "time":
            await state.update_data(edit_scope="account", edit_rule_id=rule_id)
            await _edit(
                callback,
                "选择新的基准小时（新加坡时间）",
                hour_panel(f"msgedittime:account:{rule_id}"),
            )
            return
        if action == "offset":
            await state.update_data(edit_scope="account", edit_rule_id=rule_id)
            await _edit(
                callback,
                "选择新的随机范围",
                offset_panel(f"msgeditoffset:account:{rule_id}"),
            )


@router.message(ScheduleEditFlow.value)
async def receive_schedule_edit(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    scope = str(data.get("edit_scope") or "account")
    async with sessionmaker() as session:
        model = ScheduledMessageRule if scope == "master" else ScheduledMessage
        rule = await session.get(model, int(data["edit_rule_id"]))
        if rule is None:
            await finish_message_flow(state, message.bot, message.chat.id, message)
            await message.answer("规则不存在")
            return
        try:
            target = (
                validate_bot_target(message.text or "")
                if data["edit_field"] == "target"
                else rule.target_ref
            )
            text = (
                validate_message_text(message.text or "")
                if data["edit_field"] == "text"
                else rule.message_text
            )
            if data["edit_field"] in {"target", "text"}:
                offset = rule.random_offset_minutes
            else:
                try:
                    offset = int(message.text or "")
                except ValueError as exc:
                    raise ValueError("随机分钟必须是 0 到 720") from exc
            updater = update_rule_template if scope == "master" else update_schedule
            await updater(
                rule,
                target,
                text,
                rule.base_minute // 60,
                rule.base_minute % 60,
                offset,
                session,
            )
        except ValueError as exc:
            await tracked_answer(
                message, state, str(exc), reply_markup=force_reply("重新输入")
            )
            return
        await _audit_rule_action(
            session,
            message.from_user.id,
            "update",
            rule.id,
            {"field": data["edit_field"], "scope": scope},
        )
        await session.commit()
    await finish_message_flow(state, message.bot, message.chat.id, message)
    text = (
        "主规则已更新并覆盖全部应用账号，将从明天生效。"
        if scope == "master"
        else "账号规则已更新，将从明天生效。"
    )
    await message.answer(text, reply_markup=main_menu())


@router.callback_query(F.data.startswith("msgedittime:"))
async def edit_time_callback(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    scope, rule_id, action = parts[1], int(parts[2]), parts[3]
    if action == "hour":
        await _edit(
            callback,
            f"选择分钟（已选 {int(parts[4]):02d} 时）",
            minute_panel(
                int(parts[4]),
                f"msgedittime:{scope}:{rule_id}",
            ),
        )
        return
    if action == "minute_custom":
        await reset_message_flow(state, callback.message)
        await state.set_state(ScheduleEditFlow.minute)
        await state.update_data(
            edit_scope=scope,
            edit_rule_id=rule_id,
            edit_hour=int(parts[4]),
        )
        await callback.answer()
        if callback.message:
            await tracked_answer(
                callback.message,
                state,
                "输入分钟数（0–59）",
                reply_markup=force_reply("0-59"),
            )
        return
    if action != "minute":
        await callback.answer("请选择时间", show_alert=True)
        return
    hour, minute = int(parts[4]), int(parts[5])
    async with sessionmaker() as session:
        model = ScheduledMessageRule if scope == "master" else ScheduledMessage
        rule = await session.get(model, rule_id)
        if rule is None:
            await callback.answer("规则不存在", show_alert=True)
            return
        updater = update_rule_template if scope == "master" else update_schedule
        await updater(
            rule, rule.target_ref, rule.message_text, hour, minute,
            rule.random_offset_minutes, session,
        )
        await _audit_rule_action(
            session,
            callback.from_user.id,
            "update",
            rule.id,
            {"field": "time", "scope": scope},
        )
        await session.commit()
    if scope == "master":
        await _render_master_detail(callback, sessionmaker, rule_id)
    else:
        await _edit(callback, _rule_text(rule), rule_detail_panel(rule))


@router.message(ScheduleEditFlow.minute)
async def receive_schedule_edit_minute(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    try:
        minute = int(message.text or "")
        if not 0 <= minute <= 59:
            raise ValueError
    except ValueError:
        await tracked_answer(
            message, state, "分钟必须是 0 到 59", reply_markup=force_reply("0-59")
        )
        return
    data = await state.get_data()
    scope = str(data.get("edit_scope") or "account")
    async with sessionmaker() as session:
        model = ScheduledMessageRule if scope == "master" else ScheduledMessage
        rule = await session.get(model, int(data["edit_rule_id"]))
        if rule is None:
            await finish_message_flow(state, message.bot, message.chat.id, message)
            await message.answer("规则不存在")
            return
        updater = update_rule_template if scope == "master" else update_schedule
        await updater(
            rule,
            rule.target_ref,
            rule.message_text,
            int(data["edit_hour"]),
            minute,
            rule.random_offset_minutes,
            session,
        )
        await _audit_rule_action(
            session,
            message.from_user.id,
            "update",
            rule.id,
            {"field": "time", "scope": scope},
        )
        await session.commit()
    await finish_message_flow(state, message.bot, message.chat.id, message)
    text = (
        "主规则已更新并覆盖全部应用账号，将从明天生效。"
        if scope == "master"
        else "账号规则已更新，将从明天生效。"
    )
    await message.answer(text, reply_markup=main_menu())


@router.callback_query(F.data.startswith("msgeditoffset:"))
async def edit_offset_callback(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    scope, rule_id, action = parts[1], int(parts[2]), parts[3]
    if action == "custom":
        await reset_message_flow(state, callback.message)
        await state.set_state(ScheduleEditFlow.value)
        await state.update_data(
            edit_scope=scope,
            edit_rule_id=rule_id,
            edit_field="offset",
        )
        await callback.answer()
        if callback.message:
            await tracked_answer(
                callback.message,
                state,
                "输入随机分钟数（0–720）",
                reply_markup=force_reply("0-720"),
            )
        return
    offset = int(parts[4])
    async with sessionmaker() as session:
        model = ScheduledMessageRule if scope == "master" else ScheduledMessage
        rule = await session.get(model, rule_id)
        if rule is None:
            await callback.answer("规则不存在", show_alert=True)
            return
        updater = update_rule_template if scope == "master" else update_schedule
        await updater(
            rule, rule.target_ref, rule.message_text, rule.base_minute // 60,
            rule.base_minute % 60, offset, session,
        )
        await _audit_rule_action(
            session,
            callback.from_user.id,
            "update",
            rule.id,
            {"field": "offset", "scope": scope},
        )
        await session.commit()
    if scope == "master":
        await _render_master_detail(callback, sessionmaker, rule_id)
    else:
        await _edit(callback, _rule_text(rule), rule_detail_panel(rule))


@router.callback_query(F.data == "msghistory:all")
@router.callback_query(F.data.regexp(r"^msghistory:\d+$"))
@router.callback_query(F.data.regexp(r"^msghistory:rule:\d+$"))
@router.callback_query(F.data.regexp(r"^msghistory:master:\d+$"))
async def message_history(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    async with sessionmaker() as session:
        query = select(Job).where(
            Job.type.in_({"send", "send_all", "webapp_send", "manual_send", "scheduled_send"})
        )
        if parts[1] == "master":
            master_id = int(parts[2])
            assignment_ids = list(
                (
                    await session.scalars(
                        select(ScheduledMessage.id).where(
                            ScheduledMessage.rule_id == master_id
                        )
                    )
                ).all()
            )
            if assignment_ids:
                query = query.where(
                    Job.params_json["schedule_id"].as_integer().in_(assignment_ids)
                )
                jobs = list(
                    (await session.scalars(query.order_by(Job.id.desc()).limit(10))).all()
                )
            else:
                jobs = []
        elif parts[1] == "rule":
            rule_id = int(parts[2])
            query = query.where(Job.params_json["schedule_id"].as_integer() == rule_id)
            jobs = list((await session.scalars(query.order_by(Job.id.desc()).limit(10))).all())
        elif parts[1] != "all":
            account_id = int(parts[1])
            query = query.join(JobItem).where(JobItem.account_id == account_id).distinct()
            jobs = list((await session.scalars(query.order_by(Job.id.desc()).limit(10))).all())
        else:
            jobs = list((await session.scalars(query.order_by(Job.id.desc()).limit(10))).all())
    lines = []
    for job in jobs:
        params = job.params_json or {}
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        text = params.get("text") or payload.get("text") or ""
        lines.append(
            f"#{job.id} {job.type} · {job.status}\n{params.get('target', '-')} · {str(text)[:80]}"
        )
    await _edit(
        callback,
        "发送历史\n\n" + ("\n\n".join(lines) if lines else "暂无记录"),
        message_history_panel(jobs),
    )


@router.callback_query(F.data.startswith("msghistory:reuse:"))
async def reuse_message_history(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    job_id = int((callback.data or "").split(":")[2])
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
    if job is None:
        await callback.answer("历史任务不存在", show_alert=True)
        return
    params = job.params_json or {}
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    account_ids = [int(value) for value in params.get("accounts", [])]
    target = str(params.get("target") or "")
    text = str(params.get("text") or payload.get("text") or "")
    if not account_ids or not target or not text:
        await callback.answer("这条历史记录无法复用", show_alert=True)
        return
    mode = "schedule" if job.type == "scheduled_send" else "send"
    await reset_message_flow(state, callback.message)
    await state.update_data(
        flow_mode=mode,
        selected_account_ids=account_ids,
        target=target,
        message_text=text,
    )
    await callback.answer("已载入历史设置")
    if not callback.message:
        return
    if mode == "schedule":
        await tracked_answer(
            callback.message,
            state,
            f"已复用目标 {target} 和指令，请选择新的基准小时（新加坡时间）",
            reply_markup=hour_panel(),
        )
    else:
        await tracked_answer(
            callback.message,
            state,
            f"确认再次发送？\n账号：{len(account_ids)} 个\n目标：{target}\n内容：{text}",
            reply_markup=confirm_panel("msgsend:confirm"),
        )
