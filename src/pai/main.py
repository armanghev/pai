"""Entry point: load config, open the database, start the bot and jobs."""

from __future__ import annotations

import logging
import os
import sys

import anthropic
from telegram import BotCommand, Update
from telegram.ext import Application

from pai import db, errorlog, mcp, money
from pai.agent import Agent
from pai.bot import PaiBot
from pai.canvas import CanvasClient
from pai.config import Secrets, load_config
from pai.github import GitHubClient
from pai.tools import default_tools

log = logging.getLogger("pai")


def setup_logging() -> None:
    """INFO and below to stdout, WARNING and up to stderr: Railway shows everything on
    stderr as an error, so a single stderr handler made every line look red."""
    fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    out.addFilter(lambda r: r.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)
    root = logging.getLogger()
    root.handlers[:] = [out, err]
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "apscheduler"):  # every poll and every job run otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)


COMMANDS = [
    BotCommand("help", "Everything Pai can do"),
    BotCommand("due", "What's due in the next 7 days"),
    BotCommand("missing", "Past-due work not turned in"),
    BotCommand("grades", "Current grades"),
    BotCommand("need", "What you need for a grade"),
    BotCommand("review", "This week's review"),
    BotCommand("plan", "This week's study plan"),
    BotCommand("replan", "Rebuild the plan from now to Sunday"),
    BotCommand("remind", "Pending reminders"),
    BotCommand("briefing", "Morning briefing, now"),
    BotCommand("standup", "Side-project standup, now"),
    BotCommand("focus", "Start a study timer, or see this week"),
    BotCommand("stop", "End the focus timer"),
    BotCommand("habits", "Habit streaks; /habits add Gym"),
    BotCommand("cards", "Make flashcards from notes"),
    BotCommand("quiz", "Review due flashcards"),
    BotCommand("leads", "Find businesses that need a website"),
    BotCommand("watch", "Watch a product's price"),
    BotCommand("draft", "Reference draft of an assignment"),
    BotCommand("memory", "What Pai remembers about you"),
    BotCommand("forget", "Forget a memory: /forget 3"),
    BotCommand("jobs", "Recurring jobs you set up in chat"),
    BotCommand("fix", "Diagnose what went wrong and propose a patch"),
    BotCommand("connect", "MCP servers: list, add, on/off, remove"),
    BotCommand("money", "Money status; /money off to pause"),
    BotCommand("health", "How the host machine is doing"),
    BotCommand("spend", "API spend today, this week, this month"),
    BotCommand("update", "Pull the latest code and restart"),
]


def main() -> None:
    setup_logging()

    config = load_config(os.environ.get("PAI_CONFIG", "config.toml"))
    secrets = Secrets()  # fails loudly if .env is incomplete
    errorlog.install(  # recent warnings for /fix, with these values redacted
        [
            v.get_secret_value()
            for v in (
                secrets.telegram_token,
                secrets.anthropic_api_key,
                secrets.canvas_token,
                secrets.github_token,
            )
            if v is not None
        ]
    )
    conn = db.connect(config.db_path)
    money.LABELS.update(config.money.labels)  # display names for Plaid item labels
    mcp.load_secrets(conn)  # MCP server tokens are redacted from logs and /fix too

    client = anthropic.AsyncAnthropic(api_key=secrets.anthropic_api_key.get_secret_value())
    agent = Agent(client, conn, config, default_tools())
    canvas = (
        CanvasClient(secrets.canvas_base_url, secrets.canvas_token.get_secret_value())
        if secrets.canvas_base_url and secrets.canvas_token
        else None
    )
    github = (
        GitHubClient(secrets.github_token.get_secret_value(), config.github.user)
        if secrets.github_token
        else None
    )
    plaid = money.settings(secrets)  # None: money stays off, quietly
    key = secrets.pai_token_key.get_secret_value() if secrets.pai_token_key else None
    if key:
        errorlog.LOG.add_secret(key)
    cipher = money.cipher(key)
    money.load_secrets(conn, plaid, cipher)
    bot = PaiBot(
        agent,
        secrets.telegram_user_id,
        canvas=canvas,
        github=github,
        money_client=money.Plaid(plaid) if plaid else None,
        money_settings=plaid,
        money_cipher=cipher,
    )

    async def post_init(app: Application) -> None:
        await app.bot.set_my_commands(COMMANDS)
        await bot.announce_restart(app)
        me = await app.bot.get_me()
        if not me.supports_guest_queries:
            log.info(
                "guest mode is off; turn it on in @BotFather to use @%s in other chats", me.username
            )
        log.info(
            "pai is up: tz %s, chat model %s, canvas %s, github %s, money %s",
            config.timezone,
            config.models.chat,
            "on" if canvas else "off",
            "on" if github else "off",
            "on" if plaid else "off",
        )

    app = (
        Application.builder()
        .token(secrets.telegram_token.get_secret_value())
        .post_init(post_init)
        .build()
    )
    bot.install(app)
    # All types: guest messages (@Pai in other chats) aren't in Telegram's default set.
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
    # Non-zero after /update so systemd (Restart=always) brings us back on the new code.
    sys.exit(3 if bot.restart_requested else 0)


if __name__ == "__main__":
    main()
