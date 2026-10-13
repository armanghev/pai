"""Inline keyboards shared by the bot and scheduled jobs."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# "I'm behind" on tonight's plan: start 30 min / 1 h late, or missed the block.
SLIP_BUTTONS = [[("🐢 +30m", "s:30"), ("🐢 +1h", "s:60"), ("❌ Missed it", "s:missed")]]


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )
