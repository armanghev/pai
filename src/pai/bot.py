"""Telegram handlers: auth gate, slash commands and free text."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import sqlite3
import tempfile
import time as clock
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from pai import canvas as canvas_mod
from pai import (
    db,
    errorlog,
    estimates,
    fixer,
    flashcards,
    focus,
    habits,
    heartbeat,
    links,
    mcp,
    memory,
    money,
    photo,
    prices,
    routines,
    slip,
    spend,
    youtube,
)
from pai.agent import Agent, BudgetExceeded
from pai.canvas import CanvasClient
from pai.github import GitHubClient
from pai.jobs import Jobs
from pai.keyboards import SLIP_BUTTONS, buttons
from pai.planner import format_plan, latest_plan, make_plan, rest_of_week
from pai.tools import ToolContext
from pai.tools.canvas import get_canvas_items, get_grades
from pai.tools.drafts import draft_assignment
from pai.tools.grades import grade_needed
from pai.tools.prices import watch_price
from pai.tools.reminders import add_reminder, list_reminders
from pai.tools.system import format_health, health

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
RESTART_KEY = "restart_pending"
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # bots can't fetch files bigger than this
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
HELP = (
    'Hoo! I\'m Pai. Just text me (say "think hard" for the big brain), or use:\n'
    "/due       what's due in the next 7 days\n"
    "/missing   past-due work you haven't turned in\n"
    "/grades    current grades\n"
    "/need      what you need for a grade (/need comm B)\n"
    "/review    this week's review, now (also Sundays at 7pm)\n"
    "/plan      this week's study plan\n"
    "/replan    rebuild the plan from now to Sunday (/replan tonight 1h if you're behind)\n"
    "/remind    pending reminders\n"
    "/briefing  the morning briefing, right now\n"
    "/standup   side-project standup, right now\n"
    "/focus     start a study timer (/focus 25 CS/IT 135) or see this week\n"
    "/stop      end the running focus timer\n"
    "/habits    habit streaks (/habits add Gym, /habits remove Gym)\n"
    "/cards     make flashcards from notes (/cards CS135 + notes)\n"
    "/quiz      review flashcards that are due\n"
    "/leads     find businesses that need a website (/leads dentists)\n"
    "/watch     price watch (/watch <product link> [target]); /unwatch to stop\n"
    "/draft     reference draft of an assignment (/draft essay 2, /draft revise essay 2)\n"
    "/memory    what I remember about you; /forget 3 to drop one\n"
    "/jobs      check-in list and recurring jobs you set up in chat\n"
    "/fix       why something broke, with a proposed patch (/fix why did /leads fail)\n"
    "/connect   MCP servers (/connect add name https://url [token], /connect off name)\n"
    "/money     money status; /money positions, /money link mybank, /money off, /money sync\n"
    "/health    how the host machine is doing\n"
    "/spend     what I've cost today, this week and this month\n"
    "/update    pull the latest code and restart\n"
    "Send a YouTube link and I'll send back the MP3 with its cover art.\n"
    "Send any other link (or a PDF link) and I'll summarize it.\n"
    "Send a photo of a syllabus, flyer or whiteboard and I'll set reminders for its dates."
)
DEEP_TRIGGERS = ("think hard", "think carefully", "think deeply")
GUEST_COST_LIMIT = 0.05
GUEST_NOTE = (
    "[Guest mode: the owner @mentioned you in a chat other people can see. Answer in a few "
    "sentences. Don't bring up anything private (grades, assignments, schedule, reminders, "
    "notes, projects, money, what you remember about them) unless this message asks for it.]"
)


def plain(html_text: str) -> str:
    """Telegram HTML as plain text for the fallback. Spoilered parts stay hidden, since
    they're money amounts."""
    text = re.sub(r"<tg-spoiler>.*?</tg-spoiler>", "▒▒", html_text, flags=re.S)
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text))


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on paragraph, then line, then hard boundaries to fit Telegram's limit."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


class Draft:
    """Streams a reply into a Telegram message draft as it's written: plain text, at most
    every INTERVAL seconds. The final message is still sent normally (drafts are only a
    preview). If Telegram refuses a draft, streaming stops quietly for this reply."""

    INTERVAL = 0.7

    def __init__(self, bot, chat_id: int, draft_id: int):
        self.bot, self.chat_id, self.draft_id = bot, chat_id, draft_id
        self.enabled = True
        self._last = 0.0

    async def show(self, text: str, force: bool = False) -> None:
        if not self.enabled:
            return
        now = clock.monotonic()
        if not force and now - self._last < self.INTERVAL:
            return
        self._last = now
        try:
            await self.bot.send_message_draft(
                self.chat_id, self.draft_id, text=text[:TELEGRAM_LIMIT]
            )
        except TelegramError as e:
            self.enabled = False
            log.info("draft streaming off for this reply: %s", e)

    async def __call__(self, text: str) -> None:
        await self.show(text)


class PaiBot:
    def __init__(
        self,
        agent: Agent,
        owner_id: int,
        canvas: CanvasClient | None = None,
        github: GitHubClient | None = None,
        money_client=None,
        money_settings=None,
        money_cipher=None,
    ):
        self.agent = agent
        self.owner_id = owner_id
        self.canvas = canvas
        self.github = github
        self.money_client = money_client  # pai.money.Plaid, or None when money is off
        self.money_settings = money_settings
        self.money_cipher = money_cipher  # encrypts tokens from /money link; None without a key
        self.jobs = Jobs(self)
        self._ignored: set[int] = set()
        self._chat_lock = asyncio.Lock()  # one exchange at a time keeps history ordered
        self._yt_lock = asyncio.Lock()  # one download at a time on a small host
        self.restart_requested = False
        self._fixer: Agent | None = None

    # ---- plumbing ------------------------------------------------------------

    async def send(
        self, app: Application, text: str, silent: bool = False, markup=None, html: bool = False
    ) -> None:
        """Send to the owner. `silent` skips the notification sound; `markup` (buttons)
        goes on the last chunk; `html` for text already escaped as Telegram HTML."""
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            extra = {
                "link_preview_options": NO_PREVIEW,
                "disable_notification": silent,
                "reply_markup": markup if i == len(chunks) - 1 else None,
            }
            try:
                mode = ParseMode.HTML if html else ParseMode.MARKDOWN
                await app.bot.send_message(self.owner_id, chunk, parse_mode=mode, **extra)
            except BadRequest:  # unbalanced Markdown or HTML; send it plain
                log.warning("telegram rejected formatting; sending plain text")
                await app.bot.send_message(self.owner_id, plain(chunk) if html else chunk, **extra)

    def tool_context(self, app: Application) -> ToolContext:
        return ToolContext(
            conn=self.agent.conn,
            config=self.agent.config,
            send=lambda text: self.send(app, text),
            agent=self.agent,
            canvas=self.canvas,
            github=self.github,
            schedule_reminder=lambda rid, when: self.jobs.schedule_reminder(app, rid, when),
            unschedule_reminder=lambda rid: self.jobs.unschedule_reminder(app, rid),
            send_file=lambda name, f, caption: self.send_file(app, name, f, caption),
            send_buttons=lambda text, rows: self.send(app, text, markup=buttons(rows)),
            send_html=lambda text: self.send(app, text, html=True),
            money_on=self.money_on(),
        )

    def money_on(self) -> bool:
        return self.money_client is not None and not money.paused(self.agent.conn)

    async def gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Runs before every other handler. Strangers are dropped silently, logged once."""
        user = update.effective_user
        if user is not None and user.id == self.owner_id:
            return
        uid = user.id if user else 0
        if uid not in self._ignored:
            self._ignored.add(uid)
            log.warning("ignoring updates from non-owner %s (@%s)", uid, user and user.username)
        raise ApplicationHandlerStop

    # ---- commands ------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.send(context.application, HELP)

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        data_dir = self.agent.config.db_path.parent
        await self.send(context.application, format_health(health(data_dir)))

    async def spend(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cfg = self.agent.config
        text = spend.summary(self.agent.conn, datetime.now(UTC), cfg.tz)
        text += f"\nDaily limit: ${cfg.budget.daily_usd:.2f}"
        await self.send(context.application, text)

    async def update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """git pull + uv sync, then exit so systemd restarts us on the new code."""
        app = context.application
        await self.send(app, "Pulling the latest code…")
        proc = await asyncio.create_subprocess_shell(
            "git pull --ff-only && uv sync --frozen",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        tail = out.decode(errors="replace").strip()[-1500:]
        if proc.returncode != 0:
            await self.send(app, f"Update failed:\n```\n{tail}\n```")
            return
        await self.send(app, f"```\n{tail}\n```\nRestarting. Back in a few seconds.")
        log.info("update applied; exiting for systemd restart")
        db.kv_set(self.agent.conn, RESTART_KEY, "1")
        self.restart_requested = True
        app.stop_running()

    async def _tool_command(self, update, context, fn, args=None, render=None) -> None:
        app = context.application
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        result = await fn(args or {}, self.tool_context(app))
        await self.send(app, render(result) if render else result)

    async def due(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def render(r):
            if not r["items"]:
                return "Nothing due in the next 7 days. 🦉"
            return "\n".join(
                f"• {i['due']} *{i['course']}* [{i['title']}]({i['url']})"
                + (f" ({i['points']:g} pts)" if i["points"] else "")
                for i in r["items"]
            )

        args = {"days": 7, "include_overdue": False}
        await self._tool_command(update, context, get_canvas_items, args, render)

    async def missing_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app = context.application
        if self.canvas is None:
            await self.send(app, "Canvas isn't set up yet (CANVAS_TOKEN in .env).")
            return
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        now, tz = datetime.now(UTC), self.agent.config.tz
        all_items = await self.canvas.all_assignments()
        items = canvas_mod.missing(all_items, now)
        locked = canvas_mod.locked_missed(all_items, now)
        if not items and not locked:
            await self.send(app, "Nothing missing. 🦉")
            return

        def line(a) -> str:
            late = (now - a.due_at).days
            ago = "today" if late == 0 else f"{late} day{'s' if late != 1 else ''} ago"
            pts = f", {a.points:g} pts" if a.points else ""
            due = f"{a.due_at.astimezone(tz):%b} {a.due_at.astimezone(tz).day}"
            return f"• *{a.course}* [{a.title}]({a.html_url}) (due {due}, {ago}{pts})"

        lines = []
        if items:
            lines += [f"*Missing* ({len(items)}), still open:", *map(line, items)]
        else:
            lines.append("Nothing missing that you can still turn in. 🦉")
        if locked:
            lines += [
                "",
                f"🔒 *Locked* ({len(locked)}), can't be submitted anymore:",
                *map(line, locked),
            ]
        await self.send(app, "\n".join(lines))

    async def grades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def render(r):
            lines = [
                f"• *{c['course']}*: {c['grade'] or '–'}"
                + (f" ({c['score']:g}%)" if c["score"] is not None else "")
                for c in r["courses"]
            ]
            if r["recently_graded"]:
                lines.append("\n*Recently graded*")
                lines += [
                    f"• {g['course']} {g['title']}: {g['score']:g}"
                    + (f"/{g['points']:g}" if g["points"] else "")
                    for g in r["recently_graded"][:10]
                ]
            return "\n".join(lines)

        await self._tool_command(update, context, get_grades, None, render)

    async def plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cfg = self.agent.config
        today = datetime.now(cfg.tz).date()
        plan = latest_plan(self.agent.conn, today)
        if plan is None:
            await self.send(context.application, "No plan for this week yet. Send /replan.")
            return
        await self.send(
            context.application,
            format_plan(plan, "This week's plan") + "\n\n_Behind tonight? Tap below._",
            markup=buttons(SLIP_BUTTONS),
        )

    async def replan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app, args = context.application, [a.lower() for a in context.args or []]
        if args and args[0] == "tonight":
            delay, missed = 0, "missed" in args
            for a in args[1:]:
                if a.rstrip("hm").isdigit():
                    delay = int(a.rstrip("hm")) * (60 if a.endswith("h") else 1)
            await self._slip(app, delay, missed)
            return
        await self.send(app, "Replanning from now through Sunday…")
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        now = datetime.now(UTC)
        start, end = rest_of_week(now, self.agent.config.tz)
        text = await make_plan(self.tool_context(app), start, end, now)
        await self.send(app, text, markup=buttons(SLIP_BUTTONS))

    async def _slip(self, app: Application, delay: int, missed: bool) -> None:
        text = await slip.replan_tonight(self.tool_context(app), datetime.now(UTC), delay, missed)
        await self.send(app, text)

    async def slip_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        what = q.data.split(":")[1]
        await q.answer("Reworking tonight…")
        missed = what == "missed"
        await self._slip(context.application, 0 if missed else int(what), missed)

    async def remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def render(r):
            if not r["pending"]:
                return (
                    "No pending reminders. Text me something like "
                    '"remind me Thursday at 6 to call the bank".'
                )
            return "\n".join(f"• #{p['id']} {p['when']}: {p['text']}" for p in r["pending"])

        await self._tool_command(update, context, list_reminders, None, render)

    async def briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.jobs.morning_briefing(context.application)

    async def standup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.github is None:
            await self.send(context.application, "GitHub isn't set up yet (GITHUB_TOKEN in .env).")
            return
        await context.application.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        await self.jobs.standup(context.application)

    async def habits_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app, conn = context.application, self.agent.conn
        args = context.args or []
        if args and args[0].lower() in ("add", "remove", "rm") and len(args) > 1:
            name = " ".join(args[1:])
            if args[0].lower() == "add":
                added = habits.add(conn, name)
                await self.send(app, f"Tracking *{added}*. I'll check in every evening.")
            elif habits.remove(conn, name):
                await self.send(app, f"Stopped tracking *{name}*.")
            else:
                await self.send(app, f"No active habit called {name}.")
            return
        rows = habits.active(conn)
        if not rows:
            await self.send(app, "No habits yet. Add one: `/habits add Gym`")
            return
        await self.send(app, habits.week_report(conn, datetime.now(self.agent.config.tz).date()))

    async def habit_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        try:
            _, hid, day, done = q.data.split(":")
            habits.record(self.agent.conn, int(hid), date.fromisoformat(day), done == "1")
        except ValueError:
            await q.answer("That button is out of date.")
            return
        today = datetime.now(self.agent.config.tz).date()
        streak = habits.streak(self.agent.conn, int(hid), today)
        await q.answer(f"🔥 {streak}-day streak" if done == "1" and streak > 1 else "Logged")
        # Re-draw that habit's row so the message shows what was picked (tap again to change).
        rows = []
        for row in q.message.reply_markup.inline_keyboard:
            if row[0].callback_data.split(":")[1] == hid:
                name = row[0].text.split(" ", 1)[1]
                yes = InlineKeyboardButton(
                    f"{'✅' if done == '1' else '▫️'} {name}", callback_data=f"h:{hid}:{day}:1"
                )
                no = InlineKeyboardButton(
                    "❌" if done == "0" else "▫️", callback_data=f"h:{hid}:{day}:0"
                )
                row = [yes, no]
            rows.append(row)
        try:
            await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        except BadRequest:  # unchanged markup
            pass

    async def reminder_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Done / snooze 1 hour / tomorrow 9am on a reminder that just fired."""
        q, conn, tz = update.callback_query, self.agent.conn, self.agent.config.tz
        _, rid, action = q.data.split(":")
        rid = int(rid)
        row = conn.execute("SELECT text FROM reminders WHERE id = ?", (rid,)).fetchone()
        if row is None:
            await q.answer("That reminder is gone.")
            return
        now = datetime.now(UTC)
        if action == "done":
            await q.answer("Nice 👍")
            await q.edit_message_text(f"✅ {row['text']}")
            return
        if action == "1h":
            when = now + timedelta(hours=1)
        else:
            tomorrow = now.astimezone(tz).date() + timedelta(days=1)
            when = datetime.combine(tomorrow, time(9, 0), tz)
        conn.execute(
            "UPDATE reminders SET due_at = ?, status = 'pending' WHERE id = ?",
            (spend.utc_iso(when), rid),
        )
        self.jobs.unschedule_reminder(context.application, rid)
        self.jobs.schedule_reminder(context.application, rid, when)
        label = f"{when.astimezone(tz):%a %H:%M}"
        await q.answer(f"Snoozed until {label}")
        await q.edit_message_text(
            f"⏰ {row['text']}\n_Snoozed until {label}_", parse_mode=ParseMode.MARKDOWN
        )

    async def focus_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app, conn, cfg = context.application, self.agent.conn, self.agent.config
        args = context.args or []
        if not args:
            row = focus.running(conn)
            text = focus.week_summary(conn, None, cfg.tz)
            if row:
                text = (
                    f"A {row['planned_minutes']}-min session is running. /stop to end it.\n\n"
                    + text
                )
            else:
                text += "\n\nStart one with `/focus 25 CS/IT 135`"
            await self.send(app, text)
            return
        try:
            minutes = int(args[0])
        except ValueError:
            await self.send(app, "Usage: `/focus 25 what you're working on`")
            return
        label = " ".join(args[1:])
        now = datetime.now(UTC)
        plan = latest_plan(conn, now.astimezone(cfg.tz).date())
        tied = estimates.tie(plan, label, now, cfg.tz)
        try:
            sid = focus.start(conn, minutes, label, now, tied)
        except (ValueError, RuntimeError) as e:
            await self.send(app, str(e).capitalize() + ".")
            return
        self.jobs.schedule_focus_end(app, sid, now + timedelta(minutes=minutes))
        local_end = (now + timedelta(minutes=minutes)).astimezone(cfg.tz)
        what = f" on {label}" if label else ""
        block = next((b for b in plan.blocks if b.assignment_id == tied), None) if tied else None
        toward = f"\n_Counting toward {block.course}: {block.task}._" if block else ""
        await self.send(
            app, f"🎯 Focus{what} until {local_end:%H:%M}. I'll ping you when it's done.{toward}"
        )

    async def stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app, conn = context.application, self.agent.conn
        row = focus.running(conn)
        if row is None:
            await self.send(app, "No focus session running.")
            return
        self.jobs.unschedule_focus_end(app, row["id"])
        minutes = focus.finish(conn, row["id"], datetime.now(UTC), stopped=True)
        await self.send(app, f"Stopped. {minutes} min logged.")

    # ---- reading, photos, flashcards ------------------------------------------

    async def summarize(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
        app = context.application
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        try:
            text = await links.summarize(self.agent, url)
        except (httpx.HTTPError, ValueError) as e:
            await self.send(app, f"Couldn't summarize that: {e}")
            return
        await self.send(app, text)

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Syllabus, flyer or whiteboard photo -> reminders for every dated item."""
        app, msg, cfg = context.application, update.message, self.agent.config
        if msg.photo:
            tg_file, media_type = await msg.photo[-1].get_file(), "image/jpeg"
        else:
            doc = msg.document
            if doc.file_size and doc.file_size > TELEGRAM_DOWNLOAD_LIMIT:
                await msg.reply_text("Telegram only lets bots download files up to 20 MB.")
                return
            media_type = doc.mime_type or "image/jpeg"
            if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                await msg.reply_text("Send it as a photo (JPEG or PNG) and I'll read it.")
                return
            tg_file = await doc.get_file()
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        image = bytes(await tg_file.download_as_bytearray())
        now = datetime.now(UTC)
        extracted = await self.agent.structured(
            "chat",
            photo.SYSTEM,
            photo.request(image, media_type, msg.caption or "", now, cfg.tz),
            photo.Extracted,
            job="photo_tasks",
        )
        if not extracted.items:
            await self.send(app, f"I didn't find any dates in that. {extracted.summary}")
            return
        ctx = self.tool_context(app)
        lines, skipped = [], 0
        for item in sorted(extracted.items, key=lambda i: (i.date, i.time or time(0))):
            when = photo.remind_at(item, cfg.tz, now)
            if when is None:
                skipped += 1
                continue
            text = photo.reminder_text(item)
            result = await add_reminder({"text": text, "when": when.isoformat()}, ctx)
            rid = result.split("#")[1].split(" ")[0]
            lines.append(f"• #{rid} {when.astimezone(cfg.tz):%a %m/%d %H:%M}: {text}")
        out = [f"📸 {extracted.summary}"]
        if lines:
            out += ["", f"Set {len(lines)} reminder(s):", *lines]
            out.append('\nSay "cancel reminder 12" to drop any of them.')
        if skipped:
            out.append(f"Skipped {skipped} item(s) that are already past.")
        await self.send(app, "\n".join(out))

    async def cards_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/cards DECK with notes on the next lines (or as a reply to your notes)."""
        app, msg, conn = context.application, update.message, self.agent.conn
        today = datetime.now(self.agent.config.tz).date()
        first, _, notes = msg.text.partition("\n")
        deck = " ".join(first.split()[1:])
        if not notes.strip() and msg.reply_to_message:
            notes = msg.reply_to_message.text or msg.reply_to_message.caption or ""
        if not deck or not notes.strip():
            rows = flashcards.decks(conn, today)
            listing = "\n".join(
                f"• *{r['deck']}*: {r['total']} cards, {r['due']} due" for r in rows
            )
            await self.send(
                app,
                (listing + "\n\n" if listing else "")
                + "Make a deck: `/cards CS135` with your notes on the next lines, or reply "
                "`/cards CS135` to a message with your notes. Then /quiz.",
            )
            return
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        try:
            n = await flashcards.make(self.agent, conn, deck, notes, today)
        except ValueError as e:
            await self.send(app, str(e).capitalize() + ".")
            return
        await self.send(app, f"🗂 Made {n} cards in *{deck}*. Start with /quiz {deck}")

    async def quiz_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        deck = " ".join(context.args or []) or None
        await self._show_card(context.application, deck)

    async def _show_card(self, app: Application, deck: str | None, skip: int | None = None) -> None:
        conn = self.agent.conn
        today = datetime.now(self.agent.config.tz).date()
        card = flashcards.next_due(conn, today, deck, skip)
        if card is None:
            nxt = flashcards.next_due_date(conn, deck)
            more = f" Next review: {nxt}." if nxt else " Make a deck with /cards."
            await self.send(app, "🎉 All caught up." + more)
            return
        scope = deck or ""
        button = InlineKeyboardButton("Show answer", callback_data=f"f:show:{card['id']}:{scope}")
        await app.bot.send_message(
            self.owner_id,
            f"🗂 {card['deck']}\n\n{card['front']}",
            reply_markup=InlineKeyboardMarkup([[button]]),
        )

    async def card_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q, conn = update.callback_query, self.agent.conn
        parts = q.data.split(":", 4)
        action, card_id = parts[1], int(parts[2])
        card = conn.execute("SELECT * FROM flashcards WHERE id = ?", (card_id,)).fetchone()
        if card is None:
            await q.answer("That card is gone.")
            return
        if action == "show":
            scope = parts[3] if len(parts) > 3 else ""
            buttons = [
                InlineKeyboardButton(label, callback_data=f"f:rate:{card_id}:{key}:{scope}")
                for key, label in (
                    ("again", "Again"),
                    ("hard", "Hard"),
                    ("good", "Good"),
                    ("easy", "Easy"),
                )
            ]
            await q.answer()
            await q.edit_message_text(
                f"🗂 {card['deck']}\n\n{card['front']}\n\n➡️ {card['back']}",
                reply_markup=InlineKeyboardMarkup([buttons]),
            )
            return
        rating, scope = parts[3], (parts[4] if len(parts) > 4 else "")
        today = datetime.now(self.agent.config.tz).date()
        days = flashcards.rate(conn, card_id, rating, today)
        await q.answer("Again soon" if days == 0 else f"Next in {days} day(s)")
        await q.edit_message_reply_markup(None)
        await self._show_card(context.application, scope or None, skip=card_id)

    async def send_file(self, app: Application, name: str, fileobj, caption: str) -> None:
        await app.bot.send_document(self.owner_id, fileobj, filename=name, caption=caption)

    async def draft_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/draft essay 2: a reference draft of a Canvas assignment, sent as a file."""
        app, msg = context.application, update.message
        first, _, pasted = msg.text.partition("\n")  # instructions may follow on new lines
        query = " ".join(first.split()[1:])
        if not pasted.strip() and msg.reply_to_message:
            pasted = msg.reply_to_message.text or ""
        if not query:
            await self.send(app, "Which assignment? For example: `/draft essay 2`")
            return
        await self.send(app, "📝 Writing a reference draft…")
        await app.bot.send_chat_action(self.owner_id, ChatAction.UPLOAD_DOCUMENT)
        revise = query.lower().startswith("revise ")
        query = query[len("revise ") :] if revise else query
        args = {"assignment": query, "instructions": pasted.strip(), "use_submission": revise}
        result = await draft_assignment(args, self.tool_context(app))
        if not result.startswith("Sent"):
            await self.send(app, result)

    async def need_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/need comm B: what you need on the rest of a class's posted work."""
        app, args = context.application, context.args or []
        if len(args) < 2:
            await self.send(app, "Usage: `/need comm B` or `/need cs135 90`")
            return
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        result = await grade_needed(
            {"course": " ".join(args[:-1]), "target": args[-1]}, self.tool_context(app)
        )
        await self.send(app, result + "\n_Estimate from the work posted on Canvas so far._")

    async def watch_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/watch URL [target]: daily price check. /watch alone lists what's watched."""
        app, args, conn = context.application, context.args or [], self.agent.conn
        if not args:
            rows = prices.active(conn)
            if not rows:
                await self.send(
                    app, "Not watching anything. `/watch <product link> [target price]`"
                )
                return
            lines = ["*Watching*"]
            for r in rows:
                goal = f", target ${r['target']:,.2f}" if r["target"] else ""
                now = f"${r['last_price']:,.2f}" if r["last_price"] is not None else "?"
                lines.append(f"• #{r['id']} [{r['title'] or 'item'}]({r['url']}): {now}{goal}")
            lines.append("\n`/unwatch 3` to stop one.")
            await self.send(app, "\n".join(lines))
            return
        target = None
        if len(args) > 1:
            try:
                target = float(args[1].lstrip("$").replace(",", ""))
            except ValueError:
                await self.send(app, "The target should be a number, like `/watch <link> 199`")
                return
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        try:
            text = await watch_price({"url": args[0], "target": target}, self.tool_context(app))
        except httpx.HTTPError as e:
            text = f"Couldn't open that page: {type(e).__name__}"
        await self.send(app, text)

    async def unwatch_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app, args = context.application, context.args or []
        if not args or not args[0].lstrip("#").isdigit():
            await self.send(app, "Usage: `/unwatch 3` (ids are in /watch)")
            return
        ok = prices.remove(self.agent.conn, int(args[0].lstrip("#")))
        await self.send(app, "Stopped watching it." if ok else "No watch with that id.")

    async def review_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await context.application.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        await self.jobs.weekly_review(context.application)

    async def leads_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/leads [category]: research leads now. Drafts only; Pai never contacts anyone."""
        app = context.application
        category = " ".join(context.args or []) or None
        await self.send(app, "🔎 Looking for businesses with weak or missing websites…")
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        await self.jobs.lead_finder(app, category=category)

    async def memory_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/memory: everything remembered, numbered, with a Forget button each."""
        app, rows = context.application, memory.all_(self.agent.conn)
        if not rows:
            await self.send(
                app, 'Nothing remembered yet. Tell me things like "remember I work Tuesdays".'
            )
            return
        lines = ["*What I remember*", *(f"{r['id']}. {r['fact']}" for r in rows)]
        lines.append("\nTap to forget one, or `/forget 3`.")
        buttons = [InlineKeyboardButton(f"🗑 {r['id']}", callback_data=f"m:{r['id']}") for r in rows]
        rows_of = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        await self.send(app, "\n".join(lines), markup=InlineKeyboardMarkup(rows_of))

    async def forget_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app, args = context.application, context.args or []
        if not args or not args[0].lstrip("#").isdigit():
            await self.send(app, "Usage: `/forget 3` (numbers are in /memory)")
            return
        gone = memory.forget(self.agent.conn, int(args[0].lstrip("#")))
        await self.send(app, f"Forgot: {gone}" if gone else "No memory with that number.")

    async def memory_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        gone = memory.forget(self.agent.conn, int(q.data.split(":")[1]))
        await q.answer(f"Forgot: {gone}"[:200] if gone else "Already gone.")

    async def jobs_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/jobs: recurring jobs from chat, each with Pause/Resume and Delete."""
        app, rows = context.application, routines.listed(self.agent.conn)
        beat = heartbeat.items(self.agent.conn, enabled_only=False)
        mins = self.agent.config.jobs.heartbeat_minutes
        lines = [f"💓 *Check-ins* every {mins} min (quiet 23:00–07:00), watching for:"]
        lines += [f"• {b['text']}{'' if b['enabled'] else ' (off)'}" for b in beat]
        lines.append('_Change it by texting, e.g. "also watch for…" or "stop checking dinner"._')
        await self.send(app, "\n".join(lines))
        if not rows:
            await self.send(
                app,
                "No recurring jobs yet. Ask me something like "
                '"every Friday at 5pm send me what\'s due next week".',
            )
            return
        for r in rows:
            text, markup = self._routine_view(r)
            await self.send(app, text, markup=markup)

    def _routine_view(self, r) -> tuple[str, InlineKeyboardMarkup]:
        when = routines.describe(routines.days_of(r), r["at"])
        paused = r["status"] == "paused"
        text = f"🔁 *{r['title']}*, {when}{' (paused)' if paused else ''}\n_{r['prompt']}_"
        toggle = (
            ("▶️ Resume", f"j:resume:{r['id']}") if paused else ("⏸ Pause", f"j:pause:{r['id']}")
        )
        return text, buttons([[toggle, ("🗑 Delete", f"j:del:{r['id']}")]])

    async def routine_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q, conn, app = update.callback_query, self.agent.conn, context.application
        _, action, rid = q.data.split(":")
        rid = int(rid)
        row = routines.get(conn, rid)
        if row is None:
            await q.answer("That job is gone.")
            return
        if action == "no" or action == "del":
            self.jobs.unschedule_routine(app, rid)
            routines.delete(conn, rid)
            await q.answer("Deleted" if action == "del" else "Cancelled")
            await q.edit_message_text(
                f"✖️ {row['title']}: {'deleted' if action == 'del' else 'not created'}."
            )
            return
        if action == "ok" and row["status"] == "pending":
            if len(routines.listed(conn)) >= routines.MAX_ROUTINES:
                await q.answer(f"Already {routines.MAX_ROUTINES} jobs; delete one in /jobs.")
                return
        routines.set_status(conn, rid, "paused" if action == "pause" else "active")
        if action == "pause":
            self.jobs.unschedule_routine(app, rid)
        else:
            self.jobs.schedule_routine(app, rid)
        await q.answer({"ok": "Created", "pause": "Paused", "resume": "Resumed"}.get(action, "OK"))
        text, markup = self._routine_view(routines.get(conn, rid))
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

    def fixer_agent(self) -> Agent:
        """A separate agent with read-only diagnosis tools; never in the chat tool list."""
        if self._fixer is None:
            a = self.agent
            self._fixer = Agent(a.client, a.conn, a.config, fixer.TOOLS, fixer.system_prompt())
        return self._fixer

    async def diagnose(self, app: Application, question: str) -> None:
        await self.send(app, "🩺 Looking at the logs and code…")
        await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
        route = "deep" if any(t in question.lower() for t in DEEP_TRIGGERS) else "planner"
        reply = await self.fixer_agent().run(
            question,
            self.tool_context(app),
            route=route,
            job="fix",
            history=False,
            max_cost=fixer.RUN_COST_LIMIT,
        )
        await self.send(app, errorlog.LOG.redact(reply.text))

    async def fix_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/fix [question]: diagnose from recent errors and source; propose a patch."""
        question = " ".join(context.args or []) or (
            "What's the most recent error or warning, why did it happen, and how would you fix it?"
        )
        await self.diagnose(context.application, question)

    async def diagnose_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        rid = int(q.data.split(":")[1])
        record = errorlog.LOG.get(rid)
        await q.answer()
        if record is None:
            await self.send(context.application, "That error is no longer in memory (restart?).")
            return
        await self.diagnose(
            context.application,
            f"Diagnose error #{rid} ({record.logger}: {record.message}). Why did it happen and "
            "how would you fix it?",
        )

    async def money_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/money [on|off|sync]: read-only Plaid status. Never shows amounts here."""
        app, conn, args = context.application, self.agent.conn, context.args or []
        if self.money_client is None:
            await self.send(app, "Money isn't connected. See docs/plaid.md for the setup.")
            return
        sub = args[0].lower() if args else ""
        if sub == "ai" and len(args) > 1 and args[1].lower() in ("on", "off"):
            on = args[1].lower() == "on"
            money.set_ai(conn, on)
            await self.send(
                app,
                "Claude can now see your holdings for portfolio questions you ask here."
                if on
                else "Claude can't see your holdings anymore; lookups still work.",
            )
            return
        if sub in ("positions", "holdings"):
            today = datetime.now(self.agent.config.tz).date()
            await self.send(app, money.holdings_text(conn, today), html=True)
            return
        if sub == "link":
            await self._money_link(app, args[1].lower() if len(args) > 1 else "")
            return
        if sub in ("on", "off"):
            money.set_paused(conn, sub == "off")
            await self.send(app, "💵 Money paused." if sub == "off" else "💵 Money back on.")
            return
        if sub == "sync":
            await self.jobs.money_sync(app)
        items = conn.execute("SELECT * FROM money_items ORDER BY label").fetchall()
        n = {r["item"]: 0 for r in money.accounts(conn)}
        for a in money.accounts(conn):
            n[a["item"]] += 1
        lines = [f"💵 Money is {'paused' if money.paused(conn) else 'on'} (read-only)."]
        for i in items:
            state = f"needs re-linking ({i['last_error']})" if i["last_error"] else "ok"
            lines.append(f"• {money.display(i['label'])}: {n.get(i['label'], 0)} accounts, {state}")
        await self.send(app, "\n".join(lines))

    async def _money_link(self, app: Application, label: str) -> None:
        """Send a one-time Plaid link to open on the phone; a job finishes it when done."""
        if self.money_cipher is None:
            await self.send(
                app, "Set PAI_TOKEN_KEY on Railway first (any long random string), then redeploy."
            )
            return
        if not money.LABEL_RE.match(label):
            await self.send(
                app, "Usage: `/money link mybank` (a short name for the account you're linking)"
            )
            return
        try:
            link = await self.money_client.link(label)
        except money.PlaidError as e:
            await self.send(app, f"Plaid wouldn't make a link: {e.code}")
            return
        expires = datetime.now(UTC) + timedelta(seconds=money.LINK_SECONDS)
        self.agent.conn.execute(
            "INSERT INTO money_links (link_token, label, expires_at) VALUES (?, ?, ?)",
            (link["link_token"], label, spend.utc_iso(expires)),
        )
        button = InlineKeyboardButton(
            f"🔗 Link {money.display(label)}", url=link["hosted_link_url"]
        )
        await app.bot.send_message(
            self.owner_id,
            f"Tap to link {money.display(label)} in Plaid and log in to it there. The link works "
            "once and for 30 minutes; I'll confirm here when it's done. Read-only: Pai can't "
            "move money.",
            reply_markup=InlineKeyboardMarkup([[button]]),
        )
        self.jobs.watch_link(app, link["link_token"])

    async def connect_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/connect [list] | add NAME URL [TOKEN] | on NAME | off NAME | remove NAME."""
        app, conn, args = context.application, self.agent.conn, context.args or []
        sub = args[0].lower() if args else "list"
        if sub == "add":
            if len(args) > 3:  # the message holds a token: don't leave it in the chat
                try:
                    await update.message.delete()
                except TelegramError:
                    log.info("couldn't delete the /connect message")
            if len(args) < 3:
                await self.send(app, "Usage: `/connect add name https://server/mcp [token]`")
                return
            name, url, token = args[1], args[2], (args[3] if len(args) > 3 else None)
            try:
                tools = await mcp.list_tools(url, token)
                mcp.add(conn, name, url, token, tools)
            except (ValueError, httpx.HTTPError, KeyError, sqlite3.IntegrityError) as e:
                await self.send(app, f"Couldn't add *{name}*: {type(e).__name__}: {e}"[:300])
                return
            ro = sum(t["read_only"] for t in tools)
            await self.send(
                app,
                f"🔌 Connected *{name.lower()}*: {len(tools)} tools, {ro} read-only ones on. "
                "Write tools stay off until you turn them on below. Mention "
                f"*{name.lower()}* in a message to use it.",
            )
            await self._show_server(app, name.lower())
            return
        if sub in ("on", "off", "remove") and len(args) > 1:
            on = {"on": True, "off": False, "remove": None}[sub]
            ok = mcp.set_server(conn, args[1], on)
            done = {"on": "on", "off": "off", "remove": "removed"}[sub]
            await self.send(app, f"*{args[1]}* {done}." if ok else f"No server called {args[1]}.")
            return
        rows = mcp.servers(conn)
        if not rows:
            await self.send(
                app, "No MCP servers yet. `/connect add name https://server/mcp [token]`"
            )
            return
        for r in rows:
            await self._show_server(app, r["name"])

    async def _show_server(self, app: Application, name: str) -> None:
        s = next((r for r in mcp.servers(self.agent.conn) if r["name"] == name), None)
        if s is None:
            return
        tools = mcp.tools_of(self.agent.conn, s["id"])
        host = urlparse(s["url"]).netloc
        lines = [f"🔌 *{s['name']}* ({host}){'' if s['enabled'] else ', off'}"]
        lines += [
            f"{'✅' if t['enabled'] else '▫️'} {t['name']}{'' if t['read_only'] else ' (writes)'}"
            for t in tools
        ]
        rows = [
            [
                (
                    f"{'Turn off' if t['enabled'] else 'Allow'} {t['name']}"[:60],
                    f"c:{t['id']}:{0 if t['enabled'] else 1}",
                )
            ]
            for t in tools
            if not t["read_only"]
        ]
        await self.send(app, "\n".join(lines), markup=buttons(rows) if rows else None)

    async def connect_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        _, tool_id, on = q.data.split(":")
        row = mcp.set_tool(self.agent.conn, int(tool_id), on == "1")
        if row is None:
            await q.answer("That tool is gone.")
            return
        await q.answer(f"{row['name']} {'allowed' if on == '1' else 'off'}")
        server = self.agent.conn.execute(
            "SELECT name FROM mcp_servers WHERE id = ?", (row["server_id"],)
        ).fetchone()
        await self._show_server(context.application, server["name"])

    async def announce_restart(self, app: Application) -> None:
        """After /update, say we're back and on which commit."""
        if db.kv_get(self.agent.conn, RESTART_KEY) != "1":
            return
        db.kv_set(self.agent.conn, RESTART_KEY, "0")
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "-1", "--format=%h %s", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        commit = out.decode(errors="replace").strip()
        await self.send(
            app, "✅ Update done, I'm back up." + (f"\nNow on `{commit}`" if commit else "")
        )

    # ---- free text -----------------------------------------------------------

    async def youtube(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
        """YouTube link -> MP3 with cover art. Plain download, no Claude call."""
        app = context.application
        vid = youtube.video_id(url)
        cached = db.kv_get(self.agent.conn, f"yt:{vid}") if vid else None
        if cached:  # sent before: Telegram re-sends its stored copy instantly
            try:
                await app.bot.send_audio(self.owner_id, cached)
                return
            except BadRequest:
                log.info("cached file for %s no longer valid; downloading again", vid)

        status = await update.message.reply_text("🎧 Downloading…")
        loop = asyncio.get_running_loop()

        def on_stage(stage: str) -> None:  # called from the download thread
            asyncio.run_coroutine_threadsafe(status.edit_text("🎧 Converting to MP3…"), loop)

        async with self._yt_lock:
            with tempfile.TemporaryDirectory(dir=self._tmp_dir()) as tmp:
                try:
                    track = await asyncio.to_thread(youtube.download_mp3, url, Path(tmp), on_stage)
                except youtube.TooLong as e:
                    await status.edit_text(f"Too long for Telegram: {e}.")
                    return
                except Exception as e:
                    log.error("youtube download failed for %s: %s", url, e)
                    msg = str(e).removeprefix("ERROR: ").split("\n")[0][:200]
                    await status.edit_text(f"Couldn't get that one: {msg}")
                    return
                await status.edit_text("🎧 Sending…")
                await app.bot.send_chat_action(self.owner_id, ChatAction.UPLOAD_DOCUMENT)
                with track.path.open("rb") as audio:
                    thumb = track.thumb.open("rb") if track.thumb else None
                    try:
                        sent = await app.bot.send_audio(
                            self.owner_id,
                            audio,
                            title=track.title,
                            performer=track.artist or None,
                            duration=track.duration or None,
                            filename=youtube.safe_filename(track.title),
                            thumbnail=thumb,
                            read_timeout=120,
                            write_timeout=300,
                        )
                    finally:
                        if thumb:
                            thumb.close()
        if vid and sent.audio:
            db.kv_set(self.agent.conn, f"yt:{vid}", sent.audio.file_id)
        await status.delete()

    def _tmp_dir(self) -> Path:
        path = self.agent.config.db_path.parent / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        app = context.application
        url = youtube.find_url(update.message.text)
        if url:
            await self.youtube(update, context, url)
            return
        link = links.wants_summary(update.message.text)
        if link:
            await self.summarize(update, context, link)
            return
        async with self._chat_lock:
            await app.bot.send_chat_action(self.owner_id, ChatAction.TYPING)
            text = update.message.text
            route = "deep" if any(t in text.lower() for t in DEEP_TRIGGERS) else "chat"
            draft = Draft(app.bot, self.owner_id, update.message.message_id)
            await draft.show("", force=True)  # Telegram's "Thinking…" placeholder
            picked = mcp.relevant(self.agent.conn, text)
            ctx = self.tool_context(app)
            ctx.portfolio_ok = True  # the owner's own chat: the only place holdings may go
            reply = await self.agent.run(
                text,
                ctx,
                route=route,
                on_text=draft,
                mcp=mcp.request_parts(self.agent.conn, picked) if picked else None,
            )
        if reply.private:  # built on his holdings: numbers hidden, like other money replies
            await self.send(app, "💵 " + money.spoil_numbers(reply.text), html=True)
        else:
            await self.send(app, reply.text)

    async def guest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """@Pai in a chat it isn't a member of (Telegram guest mode). The gate already drops
        everyone but the owner; this checks again, since the reply is visible to others."""
        msg = update.guest_message
        if msg is None or msg.from_user is None or msg.from_user.id != self.owner_id:
            return
        app = context.application
        me = (app.bot.username or "").lower()
        text = " ".join(w for w in (msg.text or "").split() if w.lower() != f"@{me}").strip()
        if not text:
            text = "Say hi in one line."
        reply = await self.agent.run(
            f"{GUEST_NOTE}\n\n{text}",
            self.tool_context(app),
            job="guest",
            history=False,
            memories=False,
            max_cost=GUEST_COST_LIMIT,
        )
        body = split_message(reply.text)[0]
        for mode in (ParseMode.MARKDOWN, None):
            result = InlineQueryResultArticle(
                id=uuid.uuid4().hex,
                title="Pai",
                input_message_content=InputTextMessageContent(
                    body, parse_mode=mode, link_preview_options=NO_PREVIEW
                ),
            )
            try:
                await app.bot.answer_guest_query(msg.guest_query_id, result)
                return
            except BadRequest:  # unbalanced Markdown: try once more as plain text
                if mode is None:
                    raise

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(context.error, BudgetExceeded):
            await self.send(context.application, str(context.error))
            return
        if isinstance(context.error, NetworkError) and update is None:
            # A dropped connection while polling Telegram; it retries on its own.
            log.warning("telegram network hiccup: %s", context.error)
            return
        log.error("handler error", exc_info=context.error)
        try:
            await self.send(context.application, f"Something broke: {type(context.error).__name__}")
        except Exception:
            log.exception("could not report error to owner")

    # ---- wiring --------------------------------------------------------------

    def install(self, app: Application) -> None:
        app.add_handler(TypeHandler(Update, self.gate), group=-1)
        app.add_handler(CommandHandler(["start", "help"], self.start))
        app.add_handler(CommandHandler("due", self.due))
        app.add_handler(CommandHandler("missing", self.missing_cmd))
        app.add_handler(CommandHandler("grades", self.grades))
        app.add_handler(CommandHandler("plan", self.plan))
        app.add_handler(CommandHandler("replan", self.replan))
        app.add_handler(CommandHandler("remind", self.remind))
        app.add_handler(CommandHandler("briefing", self.briefing))
        app.add_handler(CommandHandler("standup", self.standup))
        app.add_handler(CommandHandler("habits", self.habits_cmd))
        app.add_handler(CommandHandler("focus", self.focus_cmd))
        app.add_handler(CommandHandler("stop", self.stop_cmd))
        app.add_handler(CallbackQueryHandler(self.habit_button, pattern=r"^h:"))
        app.add_handler(CallbackQueryHandler(self.card_button, pattern=r"^f:"))
        app.add_handler(CallbackQueryHandler(self.reminder_button, pattern=r"^r:"))
        app.add_handler(CommandHandler("cards", self.cards_cmd))
        app.add_handler(CommandHandler("quiz", self.quiz_cmd))
        app.add_handler(CommandHandler("leads", self.leads_cmd))
        app.add_handler(CommandHandler("need", self.need_cmd))
        app.add_handler(CommandHandler("watch", self.watch_cmd))
        app.add_handler(CommandHandler("unwatch", self.unwatch_cmd))
        app.add_handler(CommandHandler("review", self.review_cmd))
        app.add_handler(CommandHandler("draft", self.draft_cmd))
        app.add_handler(CommandHandler("memory", self.memory_cmd))
        app.add_handler(CommandHandler("forget", self.forget_cmd))
        app.add_handler(CallbackQueryHandler(self.memory_button, pattern=r"^m:"))
        app.add_handler(CommandHandler("jobs", self.jobs_cmd))
        app.add_handler(CallbackQueryHandler(self.routine_button, pattern=r"^j:"))
        app.add_handler(CommandHandler("fix", self.fix_cmd))
        app.add_handler(CallbackQueryHandler(self.slip_button, pattern=r"^s:"))
        app.add_handler(CommandHandler("connect", self.connect_cmd))
        app.add_handler(CommandHandler("money", self.money_cmd))
        app.add_handler(CallbackQueryHandler(self.connect_button, pattern=r"^c:"))
        app.add_handler(CallbackQueryHandler(self.diagnose_button, pattern=r"^x:"))
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & (filters.PHOTO | filters.Document.IMAGE), self.photo
            )
        )
        app.add_handler(CommandHandler("health", self.health))
        app.add_handler(CommandHandler("spend", self.spend))
        app.add_handler(CommandHandler("update", self.update))
        app.add_handler(MessageHandler(filters.UpdateType.GUEST_MESSAGE, self.guest))
        new_text = filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND  # not edits
        app.add_handler(MessageHandler(new_text, self.text))
        app.add_error_handler(self.on_error)
        self.jobs.install(app)
