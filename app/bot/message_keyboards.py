from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards import account_button_label
from app.db.models import Job, ScheduledMessage, ScheduledMessageRule, TgAccount
from app.services.scheduled_messages import SINGAPORE_TZ


def message_home_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="发送消息", callback_data="msgsend:start:multi"),
                InlineKeyboardButton(text="新增自动发送", callback_data="msgsched:start:multi"),
            ],
            [
                InlineKeyboardButton(text="管理自动发送", callback_data="msgsched:list:all"),
                InlineKeyboardButton(text="发送历史", callback_data="msghistory:all"),
            ],
            [InlineKeyboardButton(text="返回主菜单", callback_data="nav:home")],
        ]
    )


def account_picker_panel(
    accounts: list[TgAccount],
    selected: set[int],
    mode: str,
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✓' if account.id in selected else '○'} {account_button_label(account)}",
                callback_data=f"msgpick:{mode}:toggle:{account.id}:{page}",
            )
        ]
        for account in accounts
    ]
    if pages > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text="‹", callback_data=f"msgpick:{mode}:page:{max(1, page - 1)}"
                ),
                InlineKeyboardButton(
                    text=f"{page}/{pages}", callback_data=f"msgpick:{mode}:page:{page}"
                ),
                InlineKeyboardButton(
                    text="›", callback_data=f"msgpick:{mode}:page:{min(pages, page + 1)}"
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="全选", callback_data=f"msgpick:{mode}:all:{page}"
            ),
            InlineKeyboardButton(
                text="全不选", callback_data=f"msgpick:{mode}:none:{page}"
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"确认选择（{len(selected)}）", callback_data=f"msgpick:{mode}:done:{page}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="取消", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hour_panel(prefix: str = "msgtime") -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{hour:02d}时", callback_data=f"{prefix}:hour:{hour}")
        for hour in range(24)
    ]
    rows = [buttons[index : index + 4] for index in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton(text="取消", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def minute_panel(hour: int, prefix: str = "msgtime") -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{minute:02d}分", callback_data=f"{prefix}:minute:{hour}:{minute}"
        )
        for minute in range(0, 60, 5)
    ]
    rows = [buttons[index : index + 4] for index in range(0, len(buttons), 4)]
    rows.append(
        [InlineKeyboardButton(text="输入其他分钟", callback_data=f"{prefix}:minute_custom:{hour}")]
    )
    rows.append([InlineKeyboardButton(text="返回小时", callback_data=f"{prefix}:hours")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def offset_panel(prefix: str = "msgoffset") -> InlineKeyboardMarkup:
    values = [("不随机", 0), ("±15 分钟", 15), ("±30 分钟", 30), ("±60 分钟", 60), ("±120 分钟", 120)]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:set:{value}")]
        for label, value in values
    ]
    rows.append([InlineKeyboardButton(text="输入其他分钟", callback_data=f"{prefix}:custom")])
    rows.append([InlineKeyboardButton(text="取消", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_panel(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="确认", callback_data=action)],
            [InlineKeyboardButton(text="取消", callback_data="flow:cancel")],
        ]
    )


def schedules_panel(
    rules: list[ScheduledMessage],
    account_id: int | None,
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for rule in rules:
        state = "✓" if rule.enabled else "○"
        local = rule.next_run_at.astimezone(SINGAPORE_TZ)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{state} 规则 #{rule.rule_id} {rule.target_ref} · {local:%m-%d %H:%M}",
                    callback_data=f"msgrule:view:{rule.id}",
                )
            ]
        )
    if pages > 1:
        target = account_id if account_id is not None else "all"
        rows.append(
            [
                InlineKeyboardButton(
                    text="‹", callback_data=f"msgsched:list:{target}:{max(1, page - 1)}"
                ),
                InlineKeyboardButton(
                    text=f"{page}/{pages}", callback_data=f"msgsched:list:{target}:{page}"
                ),
                InlineKeyboardButton(
                    text="›", callback_data=f"msgsched:list:{target}:{min(pages, page + 1)}"
                ),
            ]
        )
    start_target = account_id if account_id is not None else "multi"
    rows.append(
        [InlineKeyboardButton(text="新增规则", callback_data=f"msgsched:start:{start_target}")]
    )
    rows.append([InlineKeyboardButton(text="发送历史", callback_data=f"msghistory:{account_id or 'all'}")])
    rows.append([InlineKeyboardButton(text="返回", callback_data="msgmenu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def master_schedules_panel(
    rules: list[tuple[ScheduledMessageRule, int]],
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for rule, account_count in rules:
        state = "✓" if rule.enabled else "○"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{state} 规则 #{rule.id} {rule.target_ref} · {account_count} 个账号",
                    callback_data=f"msgmaster:view:{rule.id}",
                )
            ]
        )
    if pages > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text="‹", callback_data=f"msgsched:list:all:{max(1, page - 1)}"
                ),
                InlineKeyboardButton(
                    text=f"{page}/{pages}", callback_data=f"msgsched:list:all:{page}"
                ),
                InlineKeyboardButton(
                    text="›", callback_data=f"msgsched:list:all:{min(pages, page + 1)}"
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="新增规则", callback_data="msgsched:start:multi")]
    )
    rows.append([InlineKeyboardButton(text="发送历史", callback_data="msghistory:all")])
    rows.append([InlineKeyboardButton(text="返回", callback_data="msgmenu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def master_rule_detail_panel(rule: ScheduledMessageRule) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="停用" if rule.enabled else "启用",
                    callback_data=f"msgmaster:toggle:{rule.id}",
                ),
                InlineKeyboardButton(
                    text="删除", callback_data=f"msgmaster:delete:{rule.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="修改目标", callback_data=f"msgmaster:target:{rule.id}"
                ),
                InlineKeyboardButton(
                    text="修改指令", callback_data=f"msgmaster:text:{rule.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="修改时间", callback_data=f"msgmaster:time:{rule.id}"
                ),
                InlineKeyboardButton(
                    text="修改随机范围", callback_data=f"msgmaster:offset:{rule.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="修改应用账号", callback_data=f"msgmaster:accounts:{rule.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="执行历史", callback_data=f"msghistory:master:{rule.id}"
                )
            ],
            [InlineKeyboardButton(text="返回规则列表", callback_data="msgsched:list:all")],
        ]
    )


def rule_detail_panel(rule: ScheduledMessage) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="停用" if rule.enabled else "启用",
                    callback_data=f"msgrule:toggle:{rule.id}",
                ),
                InlineKeyboardButton(text="删除", callback_data=f"msgrule:delete:{rule.id}"),
            ],
            [
                InlineKeyboardButton(text="修改目标", callback_data=f"msgrule:target:{rule.id}"),
                InlineKeyboardButton(text="修改指令", callback_data=f"msgrule:text:{rule.id}"),
            ],
            [
                InlineKeyboardButton(text="修改时间", callback_data=f"msgrule:time:{rule.id}"),
                InlineKeyboardButton(text="修改随机范围", callback_data=f"msgrule:offset:{rule.id}"),
            ],
            [InlineKeyboardButton(text="执行历史", callback_data=f"msghistory:rule:{rule.id}")],
            [InlineKeyboardButton(text="返回规则列表", callback_data=f"msgsched:list:{rule.account_id}")],
        ]
    )


def message_history_panel(jobs: list[Job]) -> InlineKeyboardMarkup:
    rows = []
    for job in jobs:
        params = job.params_json or {}
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        if params.get("accounts") and params.get("target") and (
            params.get("text") or payload.get("text")
        ):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"复用任务 #{job.id}",
                        callback_data=f"msghistory:reuse:{job.id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="返回", callback_data="msgmenu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
