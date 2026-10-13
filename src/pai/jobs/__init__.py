"""Scheduled jobs, all on python-telegram-bot's JobQueue (one process, one systemd unit)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import tempfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

from pai import (
    db,
    errorlog,
    estimates,
    flashcards,
    focus,
    habits,
    heartbeat,
    hn,
    leads,
    money,
    prices,
    prompts,
    routines,
    spend,
    vault,
)
from pai.agent import BudgetExceeded
from pai.config import DAYS
from pai.jobs import briefing, feeds, grade_watch, monitor, recap, review
from pai.keyboards import SLIP_BUTTONS, buttons
from pai.planner import latest_plan, make_plan, next_week
from pai.sites import check
from pai.spend import utc_iso
from pai.tools.reminders import pending
from pai.tools.system import health
from pai.weather import forecast, one_line

log = logging.getLogger(__name__)
GH_KEY = "last_github_check"
ERROR_DAY_KEY = "error_alerts_today"  # "YYYY-MM-DD:count"
MAX_ERROR_ALERTS = 3


def _ptb_day(day: str) -> int:
    """PTB numbers days 0=Sunday .. 6=Saturday."""
    return (DAYS.index(day) + 1) % 7


def _at(hhmm: str, tz) -> time:
    return time.fromisoformat(hhmm).replace(tzinfo=tz)


def snooze_buttons(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"r:{rid}:done"),
                InlineKeyboardButton("⏰ 1 hour", callback_data=f"r:{rid}:1h"),
                InlineKeyboardButton("🌅 Tomorrow 9am", callback_data=f"r:{rid}:tom"),
            ]
        ]
    )


def scrub_secrets(dest: sqlite3.Connection) -> None:
    """The weekly backup goes to Telegram, so it carries no tokens: blank them and VACUUM
    so no old copy survives in free pages."""
    dest.execute("UPDATE money_items SET access_token = NULL")
    dest.execute("UPDATE mcp_servers SET token = NULL")
    dest.execute("DELETE FROM money_links")
    dest.commit()
    dest.execute("VACUUM")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Jobs:
    def __init__(self, bot):
        self.bot = bot  # PaiBot
        self._error_seen = 0  # highest error record id already alerted on

    @property
    def conn(self) -> sqlite3.Connection:
        return self.bot.agent.conn

    @property
    def config(self):
        return self.bot.agent.config

    def _wrap(self, name, fn):
        async def run(context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                await fn(context.application, context)
            except BudgetExceeded:
                log.warning("job %s skipped: daily budget hit", name)
            except Exception as e:
                log.exception("job %s failed", name)
                await self.bot.send(context.application, f"Job *{name}* broke: {type(e).__name__}")

        run.__name__ = name
        return run

    def quiet_now(self) -> bool:
        j = self.config.jobs
        now = f"{datetime.now(self.config.tz):%H:%M}"
        if j.quiet_start <= j.quiet_end:
            return j.quiet_start <= now < j.quiet_end
        return now >= j.quiet_start or now < j.quiet_end  # wraps past midnight

    async def notify(self, app: Application, text: str) -> None:
        """Alerts from scheduled jobs. During quiet hours they arrive without a sound."""
        await self.bot.send(app, text, silent=self.quiet_now())

    # ---- reminders -----------------------------------------------------------

    def schedule_reminder(self, app: Application, rid: int, when: datetime) -> None:
        delay = max((when - datetime.now(UTC)).total_seconds(), 1)
        app.job_queue.run_once(
            self._wrap("reminder", self._fire_reminder), delay, data=rid, name=f"reminder:{rid}"
        )

    def unschedule_reminder(self, app: Application, rid: int) -> None:
        for job in app.job_queue.get_jobs_by_name(f"reminder:{rid}"):
            job.schedule_removal()

    async def _fire_reminder(self, app: Application, context) -> None:
        rid = context.job.data
        row = self.conn.execute(
            "SELECT text, due_at FROM reminders WHERE id = ? AND status = 'pending'", (rid,)
        ).fetchone()
        if row is None:
            return
        late = datetime.now(UTC) - _ts(row["due_at"]) > timedelta(minutes=5)
        note = " _(late: I was offline at the time)_" if late else ""
        await self.bot.send(app, f"⏰ {row['text']}{note}", markup=snooze_buttons(rid))
        self.conn.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (rid,))

    def reload_reminders(self, app: Application) -> int:
        rows = pending(self.conn)
        for r in rows:
            self.schedule_reminder(app, r["id"], _ts(r["due_at"]))
        return len(rows)

    # ---- school --------------------------------------------------------------

    async def grade_watch(self, app: Application, context=None) -> None:
        canvas = self.bot.canvas
        if canvas is None:
            return
        local = datetime.now(self.config.tz)
        j = self.config.jobs
        if not (j.grade_watch_start <= f"{local:%H:%M}" <= j.grade_watch_end):
            return
        items = await canvas.all_assignments(fresh=True)
        for alert in grade_watch.diff(self.conn, items, datetime.now(UTC), self.config.tz):
            await self.notify(app, alert)
        estimates.collect(self.conn, items, datetime.now(UTC))

    async def canvas_feeds(self, app: Application, context=None) -> None:
        """New course announcements and unread Canvas inbox messages."""
        canvas = self.bot.canvas
        if canvas is None:
            return
        courses, _ = await canvas.snapshot()
        since = datetime.now(UTC) - timedelta(days=14)
        posts, inbox = await asyncio.gather(
            canvas.announcements(courses, since), canvas.unread_messages(), return_exceptions=True
        )
        if isinstance(posts, Exception):
            log.warning("announcements failed: %s", posts)
        else:
            for a in sorted(
                feeds.unseen(self.conn, "seen_announcements", posts),
                key=lambda a: a["posted_at"] or since,
            ):
                await self.notify(app, feeds.announcement_text(a))
        if isinstance(inbox, Exception):
            log.warning("inbox failed: %s", inbox)
        else:
            for m in feeds.unseen(self.conn, "seen_messages", inbox):
                await self.notify(app, feeds.message_text(m))

    async def weekly_review(self, app: Application, context=None) -> None:
        cfg, now = self.config, datetime.now(UTC)
        courses, items = await self.bot.canvas.snapshot(fresh=True) if self.bot.canvas else ([], [])
        logged = sum(focus.minutes_between(self.conn, *focus.week_bounds(now, cfg.tz)).values())
        report = habits.week_report(self.conn, now.astimezone(cfg.tz).date())
        text = review.build(
            now,
            cfg.tz,
            courses,
            items,
            latest_plan(self.conn, now.astimezone(cfg.tz).date()),
            logged,
            report if habits.active(self.conn) else None,
            estimates.summary(estimates.ratios(self.conn)),
        )
        await self.notify(app, text)
        if self.bot.money_on():
            local = now.astimezone(cfg.tz).date()
            start = local - timedelta(days=local.weekday())
            if line := money.week_line(self.conn, start, local):
                await self.bot.send(app, line, silent=self.quiet_now(), html=True)

    async def price_check(self, app: Application, context=None) -> None:
        for row in prices.active(self.conn):
            try:
                price = await prices.fetch(row["url"])
            except Exception as e:
                log.warning("price check failed for %s: %s", row["url"], e)
                continue
            if price is None:
                continue
            alert = prices.check(self.conn, row, price)
            if alert:
                await self.notify(app, alert)

    async def weekly_plan(self, app: Application, context=None) -> None:
        now = datetime.now(UTC)
        start, end = next_week(now, self.config.tz)
        await self.notify(app, await make_plan(self.bot.tool_context(app), start, end, now))

    # ---- daily life ----------------------------------------------------------

    async def morning_briefing(self, app: Application, context=None) -> None:
        cfg, now = self.config, datetime.now(UTC)
        weather_task = (
            forecast(self.conn, cfg.weather.city, cfg.timezone, cfg.weather.units)
            if cfg.weather.city
            else asyncio.sleep(0)
        )
        canvas_task = self.bot.canvas.all_assignments() if self.bot.canvas else asyncio.sleep(0)
        hn_task = (
            hn.digest(cfg.digest.topics, cfg.digest.count)
            if cfg.digest.enabled
            else asyncio.sleep(0)
        )
        weather, items, stories = await asyncio.gather(
            weather_task, canvas_task, hn_task, return_exceptions=True
        )
        for name, value in (("weather", weather), ("canvas", items), ("hn", stories)):
            if isinstance(value, Exception):
                log.warning("briefing %s failed: %s", name, value)
        text = briefing.build(
            now,
            cfg.tz,
            None if isinstance(weather, Exception) or weather is None else one_line(weather),
            None if isinstance(items, Exception) or items is None else items,
            latest_plan(self.conn, now.astimezone(cfg.tz).date()),
            [(_ts(r["due_at"]), r["text"]) for r in pending(self.conn)],
            None if isinstance(stories, Exception) else stories,
        )
        due_cards = flashcards.due_count(self.conn, now.astimezone(cfg.tz).date())
        if due_cards:
            text += f"\n\n🗂 {due_cards} flashcard(s) due. /quiz"
        if cfg.vault.path:
            if cfg.vault.git_sync:
                await vault.pull(cfg.vault.path)
            try:
                lines = vault.briefing_lines(
                    vault.load_projects(cfg.vault.path), now.astimezone(cfg.tz).date()
                )
            except OSError as e:
                log.warning("briefing vault failed: %s", e)
                lines = ["_(Couldn't read the vault this morning.)_"]
            if lines:
                text += "\n\n*Projects*\n" + "\n".join(lines)
        await self.notify(app, text)

    async def shift_over(self, app: Application, context=None) -> None:
        """Weekday after-work recap: due tonight, Canvas changes since the morning briefing,
        tonight's plan and reminders, with the "behind?" buttons when there's a plan."""
        cfg, now = self.config, datetime.now(UTC)
        items = None
        if self.bot.canvas:
            try:
                items = await self.bot.canvas.all_assignments()
            except Exception as e:
                log.warning("shift recap canvas failed: %s", e)
        local = now.astimezone(cfg.tz)
        morning = datetime.combine(local.date(), time.fromisoformat(cfg.jobs.briefing_time), cfg.tz)
        plan = latest_plan(self.conn, local.date())
        text = briefing.shift_over(
            now,
            cfg.tz,
            items,
            plan,
            [(_ts(r["due_at"]), r["text"]) for r in pending(self.conn)],
            grade_watch.changes_since(self.conn, morning),
        )
        has_tonight = plan and any(b.day == local.date() for b in plan.blocks)
        await self.bot.send(
            app,
            text,
            silent=self.quiet_now(),
            markup=buttons(SLIP_BUTTONS) if has_tonight else None,
        )
        if self.bot.money_on() and (cash := money.recap(self.conn, local.date())):
            await self.bot.send(app, cash, silent=self.quiet_now(), html=True)

    async def evening_recap(self, app: Application, context=None) -> None:
        cfg, now = self.config, datetime.now(UTC)
        items = None
        if self.bot.canvas:
            try:
                items = await self.bot.canvas.all_assignments()
            except Exception as e:
                log.warning("recap canvas failed: %s", e)
        logged = sum(focus.minutes_between(self.conn, *focus.day_bounds(now, cfg.tz)).values())
        text = recap.build(
            now,
            cfg.tz,
            items,
            latest_plan(self.conn, now.astimezone(cfg.tz).date()),
            logged,
            [(_ts(r["due_at"]), r["text"]) for r in pending(self.conn)],
        )
        await self.notify(app, text)

    async def lead_finder(
        self, app: Application, context=None, category: str | None = None
    ) -> None:
        if not self.config.leads.area:
            await self.bot.send(app, "Set `area` under `[leads]` in config.toml to find leads.")
            return
        today = datetime.now(self.config.tz).date()
        messages = await leads.find(self.bot.agent, self.conn, self.config, today, category)
        for text in messages:  # a header, then one message per lead
            await self.bot.send(app, text, silent=self.quiet_now(), html=True)

    # ---- money (read-only Plaid) --------------------------------------------

    def money_tokens(self) -> dict[str, str]:
        b = self.bot
        return money.tokens(self.conn, b.money_settings, b.money_cipher) if b.money_settings else {}

    async def money_sync(self, app: Application, context=None) -> None:
        tokens = self.money_tokens()
        if not self.bot.money_on() or not tokens:
            return
        today = datetime.now(self.config.tz).date()
        alerts = await money.refresh(self.conn, self.bot.money_client, tokens, today)
        for a in alerts:
            await self.notify(app, a)

    def watch_link(self, app: Application, link_token: str) -> None:
        """Poll a phone link every 30 s until it's finished, backed out of, or expired."""
        app.job_queue.run_repeating(
            self._wrap("money_link", self._poll_link),
            interval=30,
            first=30,
            data=link_token,
            name=f"money_link:{link_token[-12:]}",
        )

    async def _poll_link(self, app: Application, context) -> None:
        token, conn = context.job.data, self.conn
        row = conn.execute("SELECT * FROM money_links WHERE link_token = ?", (token,)).fetchone()
        if row is None:
            context.job.schedule_removal()
            return
        name = money.display(row["label"])
        if _ts(row["expires_at"]) < datetime.now(UTC):
            conn.execute("DELETE FROM money_links WHERE link_token = ?", (token,))
            context.job.schedule_removal()
            await self.bot.send(
                app, f"The {name} link expired. `/money link {row['label']}` for a new one."
            )
            return
        result = await self.bot.money_client.link_result(token)
        if not result:
            return
        conn.execute("DELETE FROM money_links WHERE link_token = ?", (token,))
        context.job.schedule_removal()
        if "exited" in result:
            await self.bot.send(app, f"Linking {name} was cancelled. Nothing was saved.")
            return
        access = await self.bot.money_client.exchange(result["public_token"])
        money.save_token(conn, self.bot.money_cipher, row["label"], access)
        await self.money_sync(app)
        n = conn.execute(
            "SELECT COUNT(*) FROM money_accounts WHERE item = ?", (row["label"],)
        ).fetchone()[0]
        await self.bot.send(
            app, f"✅ {name} linked ({n} accounts, read-only). It'll be in today's 4:30 recap."
        )

    def reload_links(self, app: Application) -> int:
        rows = self.conn.execute("SELECT link_token FROM money_links").fetchall()
        for r in rows:
            self.watch_link(app, r["link_token"])
        return len(rows)

    # ---- habits --------------------------------------------------------------

    async def habit_checkin(self, app: Application, context=None) -> None:
        rows = habits.active(self.conn)
        if not rows:
            return
        today = datetime.now(self.config.tz).date().isoformat()
        keyboard = [
            [
                InlineKeyboardButton(f"✅ {h['name']}", callback_data=f"h:{h['id']}:{today}:1"),
                InlineKeyboardButton("❌", callback_data=f"h:{h['id']}:{today}:0"),
            ]
            for h in rows
        ]
        await app.bot.send_message(
            self.bot.owner_id,
            "Habit check-in: how did today go?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def habit_report(self, app: Application, context=None) -> None:
        if habits.active(self.conn):
            await self.bot.send(
                app, habits.week_report(self.conn, datetime.now(self.config.tz).date())
            )

    # ---- focus ---------------------------------------------------------------

    def schedule_focus_end(self, app: Application, sid: int, when: datetime) -> None:
        delay = max((when - datetime.now(UTC)).total_seconds(), 1)
        app.job_queue.run_once(
            self._wrap("focus_end", self._focus_end), delay, data=sid, name=f"focus:{sid}"
        )

    def unschedule_focus_end(self, app: Application, sid: int) -> None:
        for job in app.job_queue.get_jobs_by_name(f"focus:{sid}"):
            job.schedule_removal()

    async def _focus_end(self, app: Application, context) -> None:
        sid = context.job.data
        minutes = focus.finish(self.conn, sid, datetime.now(UTC))
        if minutes:
            await self.bot.send(app, f"⏰ Focus done: {minutes} min logged. Take a break. 🦉")

    def reload_focus(self, app: Application) -> None:
        row = focus.running(self.conn)
        if row:
            end = _ts(row["started_at"]) + timedelta(minutes=row["planned_minutes"])
            self.schedule_focus_end(app, row["id"], end)

    # ---- recurring jobs from chat -------------------------------------------

    def schedule_routine(self, app: Application, rid: int) -> None:
        row = routines.get(self.conn, rid)
        if row is None or row["status"] != "active":
            return
        self.unschedule_routine(app, rid)
        app.job_queue.run_daily(
            self._wrap("routine", self._run_routine),
            _at(row["at"], self.config.tz),
            days=tuple(_ptb_day(d) for d in routines.days_of(row)),
            data=rid,
            name=f"routine:{rid}",
        )

    def unschedule_routine(self, app: Application, rid: int) -> None:
        for job in app.job_queue.get_jobs_by_name(f"routine:{rid}"):
            job.schedule_removal()

    async def _run_routine(self, app: Application, context) -> None:
        row = routines.get(self.conn, context.job.data)
        if row is None or row["status"] != "active":
            return
        reply = await self.bot.agent.run(
            routines.run_prompt(row),
            self.bot.tool_context(app),
            job="routine",
            history=False,
            max_cost=routines.RUN_COST_LIMIT,
        )
        self.conn.execute(
            "UPDATE routines SET last_run_at = ? WHERE id = ?",
            (utc_iso(datetime.now(UTC)), row["id"]),
        )
        await self.notify(app, f"🔁 *{row['title']}*\n{reply.text}")

    def reload_routines(self, app: Application) -> int:
        rows = [r for r in routines.listed(self.conn) if r["status"] == "active"]
        for r in rows:
            self.schedule_routine(app, r["id"])
        return len(rows)

    # ---- monitoring ----------------------------------------------------------

    async def uptime(self, app: Application, context=None) -> None:
        sites = self.config.monitor.sites
        timeout = self.config.monitor.timeout_seconds
        results = await asyncio.gather(*(check(s.url, timeout) for s in sites))
        for site, result in zip(sites, results, strict=True):
            alert = monitor.site_transition(self.conn, site.url, result)
            if alert:
                await self.notify(app, alert)

    async def github_alerts(self, app: Application, context=None) -> None:
        gh = self.bot.github
        if gh is None:
            return
        now = datetime.now(UTC)
        last = db.kv_get(self.conn, GH_KEY)
        db.kv_set(self.conn, GH_KEY, utc_iso(now))
        if last is None:
            return  # first run: baseline only
        since = _ts(last)
        cfg = self.config.github
        repos = await gh.repos(cfg.repos, cfg.active_days, now)
        names = [r["full_name"] for r in repos]
        fails, issues = await asyncio.gather(
            asyncio.gather(*(gh.failed_runs(n, since) for n in names)),
            asyncio.gather(*(gh.new_issues(n, since) for n in names)),
        )
        for f in (x for group in fails for x in group):
            await self.bot.send(
                app, f"❌ CI failed: *{f['repo']}* {f['name']} on `{f['branch']}`\n{f['url']}"
            )
        for i in (x for group in issues for x in group):
            await self.bot.send(
                app, f"📬 New {i['kind']} on *{i['repo']}* by {i['by']}: {i['title']}\n{i['url']}"
            )

    async def standup(self, app: Application, context=None) -> None:
        gh = self.bot.github
        if gh is None:
            return
        from pai.tools.github import github_activity

        data = await github_activity({"days": 7}, self.bot.tool_context(app))
        prompt = prompts.render("standup.md", self.config)
        now = datetime.now(self.config.tz)
        text = await self.bot.agent.complete(
            "planner", prompt, f"Today is {now:%A %Y-%m-%d}.\n\n{json.dumps(data)}", job="standup"
        )
        await self.notify(app, f"*Side-project standup*\n\n{text}")

    async def vault_drift(self, app: Application, context=None) -> None:
        """Nudges when an active project goes quiet or paused projects take the week."""
        v = self.config.vault
        if v.path is None:
            return
        if v.git_sync:
            await vault.pull(v.path)
        projects = vault.load_projects(v.path)
        now = datetime.now(UTC)
        week, recent = await asyncio.gather(
            vault.commit_counts(projects, now - timedelta(days=7), self.bot.github),
            vault.commit_counts(
                [p for p in projects if p.status == vault.ACTIVE],
                now - timedelta(days=v.quiet_days),
                self.bot.github,
            ),
        )
        alerts = vault.drift_alerts(projects, week, recent, v.quiet_days)
        if alerts:
            await self.notify(app, "*Ship check* 🦉\n" + "\n".join(alerts))

    async def error_alerts(self, app: Application, context=None) -> None:
        """A short ping with a Diagnose button for new ERRORs, at most 3 a day."""
        # The buffer and its ids live in memory, so "already alerted" does too.
        fresh = [r for r in errorlog.LOG.recent(25, "ERROR") if r.id > self._error_seen]
        if not fresh:
            return
        self._error_seen = fresh[-1].id
        today = f"{datetime.now(self.config.tz):%Y-%m-%d}"
        day, _, count = (db.kv_get(self.conn, ERROR_DAY_KEY, "") or "").partition(":")
        count = int(count) if day == today and count else 0
        if count >= MAX_ERROR_ALERTS:
            return
        db.kv_set(self.conn, ERROR_DAY_KEY, f"{today}:{count + 1}")
        r = fresh[-1]
        more = f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else ""
        button = InlineKeyboardButton("🩺 Diagnose", callback_data=f"x:{r.id}")
        await self.bot.send(
            app,
            f"⚠️ Something broke{more}: `{r.logger}`: {r.message[:300]}",
            silent=self.quiet_now(),
            markup=InlineKeyboardMarkup([[button]]),
        )

    async def heartbeat(self, app: Application, context=None) -> None:
        """Background check-in on the owner's checklist; messages only when something
        matters. Skipped in quiet hours and when today's spend is near the cap."""
        cfg, now, conn = self.config, datetime.now(UTC), self.conn
        if self.quiet_now():
            return
        if spend.spent_today(conn, now, cfg.tz) >= cfg.budget.daily_usd * heartbeat.SPEND_HEADROOM:
            return
        rows = heartbeat.items(conn)
        kinds = {r["kind"] for r in rows if r["kind"]}
        custom = [r["text"] for r in rows if r["kind"] is None]
        canvas = self.bot.canvas
        assignments = unread = None
        if canvas and (kinds - {"errors", "break_after_shift"} or custom):
            try:
                assignments = await canvas.all_assignments()
                if "inbox_unforwarded" in kinds:
                    unread = await canvas.unread_messages()
            except Exception as e:
                log.warning("heartbeat canvas failed: %s", e)
        worked = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT assignment_id FROM focus_sessions WHERE assignment_id IS NOT NULL"
            )
        }
        local = now.astimezone(cfg.tz)
        facts = heartbeat.facts(
            conn,
            kinds,
            now,
            cfg.tz,
            assignments,
            worked,
            unread,
            heartbeat.forwarded_ids(db.kv_get(conn, "seen_messages")),
            [r for r in errorlog.LOG.recent(25, "ERROR") if r.id > self._error_seen],
            cfg.shift_end(DAYS[local.weekday()]) or "00:00",
            cfg.shift_end(DAYS[local.weekday()]) is not None,
        )
        fresh = heartbeat.fresh(conn, facts, now)
        if not fresh and not custom:
            return  # nothing new to weigh: no model call
        context = []
        if custom:
            soon = [
                a
                for a in assignments or []
                if a.due_at and now < a.due_at <= now + timedelta(hours=24) and a.needs_submission
            ]
            context += [
                f"due {a.due_at.astimezone(cfg.tz):%a %H:%M}: {a.course} {a.title}" for a in soon
            ]
            plan = latest_plan(conn, local.date())
            context += [
                f"plan {b.start:%H:%M}-{b.end:%H:%M}: {b.course} {b.task}"
                for b in (plan.blocks if plan else [])
                if b.day == local.date()
            ]
            context += [
                f"reminder {_ts(r['due_at']).astimezone(cfg.tz):%H:%M}: {r['text']}"
                for r in pending(conn)
                if _ts(r["due_at"]) <= now + timedelta(hours=6)
            ]
        beat = await self.bot.agent.structured(
            "chat",
            heartbeat.SYSTEM,
            heartbeat.request(fresh, custom, context, now, cfg.tz),
            heartbeat.Beat,
            job="heartbeat",
        )
        if beat.ok or not beat.message.strip():
            heartbeat.mark(conn, [f.key for f in fresh], now, heartbeat.QUIET_TTL)
            return
        heartbeat.mark(conn, [f.key for f in fresh], now)
        await self.bot.send(app, f"💓 {beat.message.strip()}")

    async def health_watch(self, app: Application, context=None) -> None:
        m = self.config.monitor
        h = health(self.config.db_path.parent)
        for alert in monitor.health_alerts(self.conn, h, m.temp_alert_c, m.disk_alert_pct):
            await self.notify(app, alert)

    async def backup(self, app: Application, context=None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"pai-{datetime.now(self.config.tz):%Y-%m-%d}.db"
            dest = sqlite3.connect(path)
            self.conn.backup(dest)
            scrub_secrets(dest)
            dest.close()
            with path.open("rb") as f:
                await app.bot.send_document(
                    self.bot.owner_id, f, filename=path.name, caption="Weekly database backup 🦉"
                )

    # ---- wiring --------------------------------------------------------------

    def install(self, app: Application) -> None:
        q, cfg, tz = app.job_queue, self.config, self.config.tz
        j = cfg.jobs
        w = self._wrap
        q.run_repeating(
            w("grade_watch", self.grade_watch), interval=j.grade_watch_hours * 3600, first=60
        )
        q.run_daily(
            w("weekly_plan", self.weekly_plan),
            _at(j.weekly_plan_time, tz),
            days=(_ptb_day(j.weekly_plan_day),),
        )
        q.run_daily(w("morning_briefing", self.morning_briefing), _at(j.briefing_time, tz))
        if cfg.monitor.sites:
            q.run_repeating(
                w("uptime", self.uptime), interval=cfg.monitor.interval_minutes * 60, first=30
            )
        if self.bot.github:
            q.run_repeating(
                w("github_alerts", self.github_alerts),
                interval=cfg.github.poll_minutes * 60,
                first=90,
            )
            q.run_daily(
                w("standup", self.standup), _at(j.standup_time, tz), days=(_ptb_day(j.standup_day),)
            )
        if cfg.vault.path:
            q.run_daily(w("vault_drift", self.vault_drift), _at(j.drift_time, tz))
        q.run_repeating(w("health_watch", self.health_watch), interval=3600, first=120)
        q.run_repeating(w("error_alerts", self.error_alerts), interval=300, first=300)
        q.run_repeating(
            w("heartbeat", self.heartbeat), interval=j.heartbeat_minutes * 60, first=600
        )
        q.run_daily(w("backup", self.backup), _at("21:00", tz), days=(_ptb_day("sun"),))
        q.run_daily(w("evening_recap", self.evening_recap), _at(j.recap_time, tz))
        by_time: dict[str, list[str]] = {}  # after-work recap, at each day's work end
        for day in DAYS:
            if end := cfg.shift_end(day):
                by_time.setdefault(end, []).append(day)
        for end, days in by_time.items():
            q.run_daily(
                w("shift_over", self.shift_over),
                _at(end, tz),
                days=tuple(_ptb_day(d) for d in days),
            )
        q.run_daily(
            w("weekly_review", self.weekly_review),
            _at(j.review_time, tz),
            days=(_ptb_day(j.review_day),),
        )
        q.run_daily(w("price_check", self.price_check), _at(j.price_time, tz))
        if self.bot.money_client is not None:  # before the 16:30 recap; markets are closed
            q.run_daily(w("money_sync", self.money_sync), _at("16:15", tz))
            self.reload_links(app)
        if self.bot.canvas:
            q.run_repeating(
                w("canvas_feeds", self.canvas_feeds), interval=j.feeds_minutes * 60, first=45
            )
        q.run_daily(w("habit_checkin", self.habit_checkin), _at(j.habits_time, tz))
        q.run_daily(
            w("habit_report", self.habit_report),
            _at(j.habits_time, tz),
            days=(_ptb_day(j.habits_report_day),),
        )
        if cfg.leads.enabled and cfg.leads.area:
            q.run_daily(
                w("lead_finder", self.lead_finder),
                _at(j.leads_time, tz),
                days=(_ptb_day(j.leads_day),),
            )
        self.reload_focus(app)
        log.info(
            "scheduled jobs installed; %d reminders and %d recurring jobs reloaded",
            self.reload_reminders(app),
            self.reload_routines(app),
        )
