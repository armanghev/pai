# pai

Pai is a small orange owl that lives in your Telegram. It's a personal assistant for a busy
student: it plans study time around work and classes, watches Canvas for grades, deadlines
and changes, keeps reminders, notes and habits, and keeps an eye on your side projects,
your money (read-only) and the machine it runs on. You talk to it in plain language; it
calls Claude with a set of tools behind the scenes.

It's built for one person. Pai answers only the Telegram user ID you configure, and every
personal detail (schedule, school, area, accounts) lives in your own config, not in the
code. Host it wherever you like: anywhere Python 3.12 or Docker runs.

- [What it does](#what-it-does)
- [Money (Plaid)](#money-plaid)
- [Setup](#setup)
- [Host it anywhere](#host-it-anywhere)
- [Costs and models](#costs-and-models)
- [Privacy and safety](#privacy-and-safety)
- [Development](#development)

## What it does

Just text Pai. Commands are shortcuts; almost everything also works in plain language
("what's due this week?", "remind me Thursday at 6 to call the bank").

### Chat and memory
- Free chat with tools: Canvas, reminders, notes, weather, web search, your projects.
  Say "think hard" for the most capable model.
- Long-term memory: tell it lasting facts ("remember I work evenings on weekdays") and it
  uses them in later chats. `/memory` lists them with delete buttons; `/forget 3` drops one.
- Replies stream into Telegram as they're written.

### Canvas (read-only)
- `/due` what's due in the next 7 days, `/missing` past-due work, `/grades` current grades,
  `/need BIO B` what you need on the rest of a class for a target grade.
- Ask about an assignment and Pai reads its full instructions: linked pages, attached files
  (PDFs, Word), rubric levels, discussion prompts and group discussions. It can read what
  you turned in and the feedback, open attachments when a question needs them, and send
  you a Canvas file in Telegram.
- `/draft essay 2` writes a labeled reference draft to study from (`/draft revise essay 2`
  builds on your submission). It never submits anything.
- Alerts every 2 hours: grades posted or changed, new assignments, due dates moved,
  points changed, assignments removed, and work going past due. Announcements and inbox
  messages every 30 minutes.

### Planning, focus and "I'm behind"
- Sunday-night study plan for the week; `/plan` shows it, `/replan` rebuilds from now.
  Plans fit your free time: study windows minus work and classes, with overrides by text
  ("I'm off Thursday").
- `/focus 25 BIO 101` starts a timer (and counts toward the matching plan block); `/stop`
  ends it. Pai learns how long each kind of work really takes you (essays, labs, quizzes,
  readings...) and sizes future plans to match.
- Running late? Tap 🐢 +30m / 🐢 +1h / ❌ Missed it on a plan, send `/replan tonight 1h`,
  or just say "running an hour late". Pai reworks only tonight: work due tonight first,
  the rest moved to later days before it's due.

### Reminders, habits, flashcards
- Reminders by text, with Done / snooze buttons. `/remind` lists them.
- Send a photo of a syllabus, flyer or whiteboard and Pai sets reminders for its dates.
- `/habits add Gym`: evening check-ins and weekly streaks.
- `/cards BIO101` with notes makes flashcards; `/quiz` reviews them with spaced repetition.

### Briefings, recaps and check-ins
- Morning briefing: weather, what's due, today's plan, reminders, Hacker News picks and
  your projects. `/briefing` sends it now.
- After work (when your work hours end): what's still due tonight, Canvas changes since
  the morning, tonight's plan and reminders.
- Evening recap, Sunday weekly review (`/review`).
- Background check-ins every ~45 minutes that message you only when something matters
  (a deadline in a few hours with no time logged, unforwarded inbox messages, errors).
  Edit what they watch by text ("also watch for...", "stop checking dinner").

### Recurring jobs
- "Every Friday at 5pm send me what's due next week" sets up a job after you tap Create.
  `/jobs` lists jobs and the check-in list, with Pause and Delete.

### Side projects and GitHub
- With a GitHub token: CI failures and new issues/PRs on repos you've pushed to lately,
  and a Sunday side-project standup (`/standup`).
- With an Obsidian vault: Telegram captures land in your inbox folder, project notes feed
  the briefing, and a daily ship check nudges about quiet projects.

### Everything else
- **Lead finder** (`/leads dentists`, weekly by default): local businesses in your area
  with weak or missing websites, each with real links and a draft note. Drafts only; Pai
  never contacts anyone. Off until you set `[leads] area`.
- **Price watch:** `/watch <product link> [target]`, daily checks, `/unwatch 3`.
- **YouTube to MP3:** send a YouTube link, get the audio with cover art.
- **Link summaries:** send any link or PDF link for a short summary.
- **MCP servers:** `/connect add name https://server/mcp [token]` adds a remote MCP server.
  Read-only tools are on; write tools stay off until you allow them. Mention the server by
  name to use it. `/connect`, `/connect off name`, `/connect remove name`.
- **`/fix`:** reads Pai's recent errors and its own source code and explains what broke,
  with a proposed patch to apply yourself. It can only read. New errors come with a
  Diagnose button.
- **@Pai in other chats:** tag the bot in any chat (Telegram guest mode) and it answers you,
  and only you, keeping private details out. Turn guest mode on for your bot in @BotFather.
- **Ops:** `/health` (host CPU, disk, memory), `/spend` (API costs), `/update` (git pull and
  restart, for git-checkout installs).

## Money (Plaid)

Optional and **read-only**. Pai uses Plaid's Transactions, Investments and Liabilities
products plus cached balances; never Transfer or Payment Initiation. It can't move money.

- **What you get:** balances and spending for checking accounts and credit cards (with the
  card's due date and minimum), and holdings for brokerage and retirement accounts with
  daily P/L, kept separate.
- **Where it shows up:** a money message after the after-work recap, a spending line in
  the Sunday review, `/money positions` for every holding, and chat questions ("how much
  did I spend this week?"). `/money` shows status; `/money off` pauses, `/money on`
  resumes, `/money sync` syncs now.
- **Numbers stay hidden:** every amount, card mask and date is behind a Telegram spoiler,
  and the first line of a money message never has numbers, so notifications don't show them.
- **Linking from your phone:** `/money link firstbank` sends a one-time Plaid link (30
  minutes, single use). Log in to "First Example Bank" in your browser; Pai finishes the
  rest and stores the access token encrypted with `PAI_TOKEN_KEY`. Do the same with
  `/money link examplebrokerage` for investments. Tokens never appear in Telegram.
- **Sandbox first:** start with `PLAID_ENV=sandbox` and Plaid's test banks (`user_good` /
  `pass_good`). For your real accounts, get Plaid Trial (Production) access, switch to the
  Production secret and `PLAID_ENV=production`, and link again.
- **What reaches Claude:** by default, nothing. Recaps, `/money` and lookups are computed
  by code and sent straight to you; the model only learns that an answer was sent. One
  opt-in: `/money ai on` lets Claude see your holdings (tickers, shares, values, weights;
  no account numbers, balances or transactions) for portfolio questions you ask in your
  own chat ("how diversified am I?"). Those replies have numbers hidden, aren't kept in
  chat history, and nothing from them is saved to memory. `/money ai off` stops it.

Details: [docs/plaid.md](docs/plaid.md).

## Setup

1. **Create the bot.** In Telegram, message @BotFather, send `/newbot`, and keep the token.
   Get your numeric user ID from @userinfobot.
2. **Install.** Python 3.12 and [uv](https://docs.astral.sh/uv/):
   ```bash
   git clone https://github.com/armanghev/pai.git && cd pai
   uv sync
   cp .env.example .env
   cp config.example.toml config.toml
   ```
3. **Secrets in `.env`** (or your host's environment variables). Required:
   `TELEGRAM_TOKEN`, `TELEGRAM_USER_ID`, `ANTHROPIC_API_KEY`. Optional, each turning on a
   feature: `CANVAS_BASE_URL` + `CANVAS_TOKEN` (e.g. `https://examplecc.instructure.com`),
   `GITHUB_TOKEN` (fine-grained, read-only), `VAULT_REPO` + `VAULT_TOKEN` +
   `VAULT_GIT_NAME` + `VAULT_GIT_EMAIL` (a git-synced vault on a server), and
   `PLAID_CLIENT_ID` + `PLAID_SECRET` + `PLAID_ENV` + `PAI_TOKEN_KEY` for money.
4. **Everything else in `config.toml`.** Every setting is commented in
   `config.example.toml`. A sample of the personal parts:
   ```toml
   timezone = "America/Chicago"

   [profile]
   about = "A nursing student at Example Community College who works weekday shifts."
   school = "community college"

   [[schedule.work]]
   days = ["mon", "tue", "wed", "thu", "fri"]
   start = "09:00"
   end = "17:00"              # the after-work recap goes out when this ends

   [[schedule.classes]]
   name = "BIO 101"
   days = ["tue", "thu"]
   start = "18:00"
   end = "19:15"

   [weather]
   city = "Springfield"

   [leads]
   area = "Springfield, IL"
   neighborhoods = ["Downtown", "Westside"]

   [money]
   labels = { firstbank = "First Example Bank", examplebrokerage = "Example Brokerage" }
   ```
5. **Run it:** `scripts/run-local.sh`, then message your bot. Features without their
   credentials or settings stay off quietly.

## Host it anywhere

Pai is one long-running Python process (Telegram long polling, so no public URL or open
port) plus a SQLite file. Anything that keeps it running works; these are examples:

- **Your own machine or a home server:** `scripts/run-local.sh`, or on Debian/Ubuntu,
  `deploy/install.sh` sets it up as a systemd service (`deploy/pai.service`).
- **Docker, a VPS or a PaaS** (Railway, Fly.io, Render, ...): the `Dockerfile` runs
  `deploy/railway-start.sh`, which writes `PAI_CONFIG_TOML` to a config file and keeps
  state in `/data` (`PAI_DATA` to change it). Mount a persistent volume there.

Run only one copy per bot token: two copies polling the same bot conflict.

## Costs and models

Pai routes each job to the cheapest model that does it well (set in `[models]`):

| Route | Default model | Used for |
| --- | --- | --- |
| chat | Claude Haiku | everyday chat, reminders, lookups, check-ins, recurring jobs |
| planner | Claude Sonnet | study plans, drafts, research, lead finder, `/fix` |
| deep | Claude Opus | when you say "think hard" |

- The system prompt and tool list are cached, so most turns are cheap.
- `[budget] daily_usd` (default $1) is a hard daily guard: past it, Pai stops calling
  Claude until midnight. Background check-ins stop at 90% of it, recurring jobs and
  `/fix` have per-run caps, and many features (recaps, alerts, money, "I'm behind") use
  no model at all.
- `/spend` shows today, this week and this month.

## Privacy and safety

- Pai answers only the Telegram user ID in `TELEGRAM_USER_ID`; everyone else is ignored.
- Canvas access is read-only: Pai never submits, posts or replies on your behalf.
- Money is read-only, numbers are hidden behind spoilers, and Plaid tokens are encrypted
  at rest. The weekly database backup Pai sends you has tokens removed.
- Secrets stay in environment variables, are redacted from logs and from `/fix`, and are
  never sent to the model.
- MCP write tools stay off until you allow each one. `/fix` can read Pai's code but never
  changes it.
- Lead finder, drafts and price watch only research and draft; Pai never contacts anyone,
  buys anything or signs up for anything.

## Development

```bash
uv run pytest            # offline; Canvas, Plaid and Claude are stubbed
uv run pytest -m live    # opt in: one real Claude call
uv run ruff check && uv run ruff format --check
```

`scripts/canvas_preview.py` serves a local, read-only page showing exactly what Pai
sees from Canvas.

MIT licensed.
