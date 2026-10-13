import json
import logging
import re
from datetime import date, timedelta
from types import SimpleNamespace as N

import pytest
from pydantic import SecretStr

from pai import errorlog, money
from pai.tools.money import money_answer, period_bounds

TODAY = date(2026, 10, 12)
BANK, BROKER = "access-sandbox-mybank-1111", "access-sandbox-broker-2222"


def visible(text: str) -> str:
    return re.sub(r"<tg-spoiler>.*?</tg-spoiler>", "", text)


def secrets(tokens):
    return N(
        plaid_client_id="cid",
        plaid_secret=SecretStr("sandbox-secret-xyz"),
        plaid_env="sandbox",
        plaid_access_tokens=SecretStr(tokens) if tokens else None,
    )


@pytest.fixture(autouse=True)
def labels(monkeypatch):
    monkeypatch.setattr(money, "LABELS", {"mybank": "Mybank", "mybroker": "Mybroker"})


@pytest.fixture
def log(monkeypatch):
    fresh = errorlog.ErrorLog()
    monkeypatch.setattr(errorlog, "LOG", fresh)
    return fresh


def test_settings_off_malformed_and_redacted(log, caplog):
    no_client = secrets(None)
    no_client.plaid_client_id = None
    assert money.settings(no_client) is None
    assert money.settings(secrets(None)).tokens == {}  # keys alone: /money link adds items
    with caplog.at_level(logging.WARNING):
        assert money.settings(secrets('["not", "an object"]')) is None
    assert "money is off" in caplog.text and BANK not in caplog.text
    s = money.settings(secrets(json.dumps({"mybank": BANK, "mybroker": BROKER})))
    assert s.tokens == {"mybank": BANK, "mybroker": BROKER} and s.env == "sandbox"
    assert (
        log.redact(f"x {BANK} {BROKER} sandbox-secret-xyz") == "x [redacted] [redacted] [redacted]"
    )


def acct(aid, type_, subtype, current, mask, available=None, limit=None):
    return {
        "account_id": aid,
        "type": type_,
        "subtype": subtype,
        "mask": mask,
        "balances": {"current": current, "available": available, "limit": limit},
    }


def txn(tid, aid, day, amount, merchant, cat):
    return {
        "transaction_id": tid,
        "account_id": aid,
        "date": day,
        "amount": amount,
        "merchant_name": merchant,
        "personal_finance_category": {"primary": cat},
    }


class FakePlaid:
    def __init__(self):
        self.value = {"rh-brk": 5000.0, "rh-ira": 2000.0}
        self.calls = []
        self.fail = {}

    def _check(self, what, token):
        self.calls.append((what, token))
        if (what, token) in self.fail:
            raise money.PlaidError(self.fail[(what, token)])

    async def accounts(self, token):
        self._check("accounts", token)
        if token == BANK:
            return [
                acct("mybank-chk", "depository", "checking", 1500.0, "1234", available=1450.0),
                acct("mybank-cc", "credit", "credit card", 430.0, "9876", limit=3000.0),
            ]
        return [
            acct("rh-brk", "investment", "brokerage", self.value["rh-brk"], "5555"),
            acct("rh-ira", "investment", "roth", self.value["rh-ira"], "7777"),
        ]

    async def transactions(self, token, cursor):
        self._check("transactions", token)
        if cursor is None:
            return {
                "added": [
                    txn("t1", "mybank-chk", TODAY, 12.40, "Chipotle", "FOOD_AND_DRINK"),
                    txn("t2", "mybank-cc", TODAY, 30.00, "Target", "GENERAL_MERCHANDISE"),
                    txn("t3", "mybank-chk", TODAY, 200.0, "Card payment", "LOAN_PAYMENTS"),
                    txn("t4", "mybank-chk", TODAY, -900.0, "Paycheck", "INCOME"),
                    txn(
                        "old", "mybank-chk", TODAY - timedelta(days=500), 5.0, "x", "FOOD_AND_DRINK"
                    ),
                ],
                "modified": [],
                "removed": [],
                "next_cursor": "c1",
            }
        return {
            "added": [],
            "modified": [txn("t2", "mybank-cc", TODAY, 35.0, "Target", "GENERAL_MERCHANDISE")],
            "removed": [{"transaction_id": "t1"}],
            "next_cursor": "c2",
        }

    async def liabilities(self, token):
        self._check("liabilities", token)
        return {
            "liabilities": {
                "credit": [
                    {
                        "account_id": "mybank-cc",
                        "minimum_payment_amount": 35.0,
                        "next_payment_due_date": TODAY + timedelta(days=4),
                    }
                ]
            }
        }

    async def holdings(self, token):
        self._check("holdings", token)
        return {
            "accounts": [{"account_id": "rh-brk"}, {"account_id": "rh-ira"}],
            "securities": [
                {
                    "security_id": "s1",
                    "ticker_symbol": "VTI",
                    "name": "Vanguard Total",
                    "type": "etf",
                },
                {
                    "security_id": "s2",
                    "ticker_symbol": "BTC",
                    "name": "Bitcoin",
                    "type": "cryptocurrency",
                },
            ],
            "holdings": [
                {
                    "account_id": "rh-brk",
                    "security_id": "s1",
                    "quantity": 10,
                    "institution_price": self.value["rh-brk"] / 10,
                    "institution_value": self.value["rh-brk"],
                    "cost_basis": 4000,
                },
                {
                    "account_id": "rh-ira",
                    "security_id": "s1",
                    "quantity": 4,
                    "institution_price": self.value["rh-ira"] / 4,
                    "institution_value": self.value["rh-ira"],
                    "cost_basis": 1800,
                },
            ],
        }


TOKENS = {"mybank": BANK, "mybroker": BROKER}


async def test_refresh_two_items_and_recap(conn):
    p = FakePlaid()
    assert await money.refresh(conn, p, TOKENS, TODAY - timedelta(days=1)) == []
    p.value = {"rh-brk": 5042.10, "rh-ira": 1990.0}
    await money.refresh(conn, p, TOKENS, TODAY)
    kinds = {a["account_id"]: a["kind"] for a in money.accounts(conn)}
    assert kinds == {
        "mybank-chk": "checking",
        "mybank-cc": "credit",
        "rh-brk": "brokerage",
        "rh-ira": "retirement",
    }
    # Second sync used the cursor: t1 removed, t2 updated; the 500-day-old one pruned.
    ids = {r[0] for r in conn.execute("SELECT id FROM money_transactions")}
    assert ids == {"t2", "t3", "t4"}
    assert money.spending(conn, TODAY, TODAY) == (35.0, 1)  # no card payment, no paycheck
    pl = money.daily_pl(conn, TODAY)
    assert pl["rh-brk"] == (5042.10, pytest.approx(42.10))
    assert ("holdings", BANK) not in p.calls and ("transactions", BROKER) not in p.calls

    text = money.recap(conn, TODAY)
    lines = text.splitlines()
    assert lines[0] == "💵 Money recap inside"
    assert not re.search(r"\d", visible(text))  # every number is behind a spoiler
    assert "Mybank credit card" in text and "Mybroker IRA (retirement)" in text
    assert "Mybroker brokerage" in text and "Investments together" in text
    assert "<tg-spoiler>+$42.10 (+0.84%) today</tg-spoiler>" in text
    assert "minimum <tg-spoiler>$35.00</tg-spoiler> due <tg-spoiler>Fri 10/16</tg-spoiler>" in text


async def test_mybroker_without_investments_shows_bank_only(conn, caplog):
    p = FakePlaid()
    p.fail[("holdings", BROKER)] = "NO_INVESTMENT_ACCOUNTS"
    with caplog.at_level(logging.INFO):
        assert await money.refresh(conn, p, TOKENS, TODAY) == []
    assert "plaid mybroker: no investments (NO_INVESTMENT_ACCOUNTS)" in caplog.text
    assert money.daily_pl(conn, TODAY) == {}
    assert "Mybank checking" in money.recap(conn, TODAY)


async def test_relink_alert_once_per_item_per_day(conn):
    p = FakePlaid()
    p.fail[("accounts", BANK)] = "ITEM_LOGIN_REQUIRED"
    alerts = await money.refresh(conn, p, TOKENS, TODAY)
    assert alerts == ["🔌 Mybank needs re-linking (ITEM_LOGIN_REQUIRED): `/money link mybank`"]
    assert await money.refresh(conn, p, TOKENS, TODAY) == []
    assert len(await money.refresh(conn, p, TOKENS, TODAY + timedelta(days=1))) == 1
    assert BANK not in alerts[0]


async def test_chat_answers_are_spoilered_and_model_sees_no_numbers(conn, ctx):
    await money.refresh(conn, FakePlaid(), TOKENS, TODAY)
    sent = []

    async def send_html(text):
        sent.append(text)

    ctx.send_html, ctx.money_on = send_html, False
    assert "isn't connected" in await money_answer({"topic": "spending"}, ctx)
    ctx.money_on = True
    for topic in ("spending", "balances", "credit", "investments", "overview"):
        reply = await money_answer({"topic": topic, "period": "this_week"}, ctx)
        assert not re.search(r"\d", reply)
    assert len(sent) == 5 and all(not re.search(r"\d", visible(t)) for t in sent)
    assert "Mybank checking" in sent[1] and "credit card" in sent[2] and "IRA" in sent[3]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_period_bounds():
    assert period_bounds("last_week", TODAY) == (date(2026, 10, 5), date(2026, 10, 11))
    assert period_bounds("last_month", TODAY) == (date(2026, 9, 1), date(2026, 9, 30))
    assert period_bounds("this_month", TODAY) == (date(2026, 10, 1), TODAY)


async def test_plaid_wrapper_maps_api_errors(monkeypatch):
    import plaid

    client = money.Plaid(money.Settings("cid", "sec", "sandbox", {}))

    err = plaid.ApiException(status=400, reason="x")
    err.body = json.dumps({"error_code": "ITEM_LOGIN_REQUIRED"})

    def fails(request):
        raise err

    monkeypatch.setattr(client.api, "accounts_get", fails)
    with pytest.raises(money.PlaidError) as e:
        await client.accounts(BANK)
    assert e.value.code == "ITEM_LOGIN_REQUIRED"
    pages = iter(
        [
            N(
                to_dict=lambda: {
                    "added": [1],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "a",
                    "has_more": True,
                }
            ),
            N(
                to_dict=lambda: {
                    "added": [2],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "b",
                    "has_more": False,
                }
            ),
        ]
    )
    monkeypatch.setattr(client.api, "transactions_sync", lambda req: next(pages))
    r = await client.transactions(BANK, None)
    assert r["added"] == [1, 2] and r["next_cursor"] == "b"


KEY = "a-long-random-pai-token-key-for-tests"


async def test_link_flow_saves_encrypted_token_and_env_wins(conn, log):
    f = money.cipher(KEY)
    assert money.cipher("short") is None
    money.save_token(conn, f, "mybank", BANK)
    stored = conn.execute("SELECT access_token FROM money_items WHERE label = 'mybank'").fetchone()[
        0
    ]
    assert BANK not in stored and f.decrypt(stored.encode()).decode() == BANK
    assert log.redact(BANK) == "[redacted]"
    s = money.Settings("cid", "sec", "sandbox", {"mybroker": BROKER})
    assert money.tokens(conn, s, f) == {"mybank": BANK, "mybroker": BROKER}
    env_wins = money.Settings("cid", "sec", "sandbox", {"mybank": "access-env-mybank-0000"})
    assert money.tokens(conn, env_wins, f)["mybank"] == "access-env-mybank-0000"
    assert money.tokens(conn, s, money.cipher("another-long-key-entirely-x")) == {
        "mybroker": BROKER
    }


def test_products_for_labels():
    assert money.products_for("mybroker") == (["investments"], [])
    assert money.products_for("mybank") == (["transactions"], ["liabilities", "investments"])


async def test_plaid_link_calls(monkeypatch):
    client = money.Plaid(money.Settings("cid", "sec", "sandbox", {}))
    made = []

    def link_token_create(req):
        made.append(req)
        return N(
            to_dict=lambda: {
                "link_token": "link-sandbox-1",
                "hosted_link_url": "https://h/1",
                "expiration": "x",
            }
        )

    monkeypatch.setattr(client.api, "link_token_create", link_token_create)
    r = await client.link("mybroker")
    assert r["hosted_link_url"] == "https://h/1"
    req = made[0].to_dict()
    assert [str(p) for p in req["products"]] == ["investments"]
    assert "transfer" not in json.dumps(req, default=str)
    assert req["hosted_link"]["url_lifetime_seconds"] == money.LINK_SECONDS

    sessions = iter(
        [
            {"link_sessions": []},
            {"link_sessions": [{"exit": {"status": "requires_credentials"}}]},
            {"link_sessions": [{"results": {"item_add_results": [{"public_token": "public-1"}]}}]},
        ]
    )

    def link_token_get(req):
        page = next(sessions)
        return N(to_dict=lambda: page)

    monkeypatch.setattr(client.api, "link_token_get", link_token_get)
    assert await client.link_result("link-sandbox-1") == {}
    assert await client.link_result("link-sandbox-1") == {"exited": True}
    assert await client.link_result("link-sandbox-1") == {"public_token": "public-1"}
    monkeypatch.setattr(
        client.api,
        "item_public_token_exchange",
        lambda req: N(to_dict=lambda: {"access_token": BANK}),
    )
    assert await client.exchange("public-1") == BANK


async def test_poll_link_finishes_and_backup_has_no_tokens(conn, config, log, tmp_path):
    import sqlite3

    from pai.jobs import Jobs, scrub_secrets

    f = money.cipher(KEY)
    sent = []

    class Client(FakePlaid):
        async def link_result(self, token):
            return {"public_token": "public-1"}

        async def exchange(self, public_token):
            return BANK

    async def send(app, text, **k):
        sent.append(text)

    bot = N(
        agent=N(conn=conn, config=config),
        money_client=Client(),
        money_cipher=f,
        money_settings=money.Settings("cid", "sec", "sandbox", {}),
        send=send,
        money_on=lambda: True,
    )
    jobs = Jobs(bot)
    conn.execute(
        "INSERT INTO money_links (link_token, label, expires_at) VALUES (?, ?, ?)",
        ("link-1", "mybank", "2999-01-01T00:00:00.000Z"),
    )
    removed = []
    job = N(data="link-1", schedule_removal=lambda: removed.append(1))
    await jobs._poll_link(None, N(job=job))
    assert removed == [1]
    assert sent[-1] == "✅ Mybank linked (2 accounts, read-only). It'll be in today's 4:30 recap."
    assert all(BANK not in t for t in sent)
    assert conn.execute("SELECT COUNT(*) FROM money_links").fetchone()[0] == 0

    path = tmp_path / "backup.db"
    dest = sqlite3.connect(path)
    conn.backup(dest)
    scrub_secrets(dest)
    dest.close()
    raw = path.read_bytes()
    stored = conn.execute("SELECT access_token FROM money_items").fetchone()[0]
    assert stored.encode() not in raw and BANK.encode() not in raw


async def test_expired_link_says_so(conn, config, log):
    from pai.jobs import Jobs

    sent = []

    async def send(app, text, **k):
        sent.append(text)

    bot = N(
        agent=N(conn=conn, config=config),
        money_client=FakePlaid(),
        send=send,
        money_cipher=money.cipher(KEY),
        money_settings=None,
        money_on=lambda: True,
    )
    conn.execute(
        "INSERT INTO money_links (link_token, label, expires_at) VALUES (?, ?, ?)",
        ("link-2", "mybroker", "2000-01-01T00:00:00.000Z"),
    )
    await Jobs(bot)._poll_link(None, N(job=N(data="link-2", schedule_removal=lambda: None)))
    assert sent == ["The Mybroker link expired. `/money link mybroker` for a new one."]


async def test_positions_show_each_holding_behind_spoilers(conn):
    p = FakePlaid()
    await money.refresh(conn, p, TOKENS, TODAY - timedelta(days=1))
    p.value = {"rh-brk": 5042.10, "rh-ira": 1990.0}
    base = p.holdings

    async def holdings(token):
        r = await base(token)
        r["holdings"].append(
            {
                "account_id": "rh-brk",
                "security_id": "s2",
                "quantity": 0.012345,
                "institution_value": 800.0,
            }
        )
        return r

    p.holdings = holdings
    await money.refresh(conn, p, TOKENS, TODAY)
    text = money.holdings_text(conn, TODAY)
    assert text.startswith("📈 Your positions, inside")
    assert not re.search(r"\d", visible(text))
    assert "• VTI (ETF) <tg-spoiler>10 sh · $5,042.10 · +$42.10 today</tg-spoiler>" in text
    assert "• BTC (crypto) <tg-spoiler>0.012345 sh · $800.00</tg-spoiler>" in text
    assert "<b>Mybroker brokerage" in text and "<b>Mybroker IRA (retirement)" in text
    assert money.answer(conn, "holdings", TODAY, TODAY, TODAY) == text
    assert money.holdings_text(conn, TODAY - timedelta(days=5)).startswith("📈 No positions")


async def test_portfolio_snapshot_only_in_own_chat_and_never_kept(conn, config, ctx):
    from conftest import FakeClient, response, text_block, tool_block

    from pai.agent import PRIVATE_NOTE, Agent
    from pai.tools import default_tools
    from pai.tools.memory import remember
    from pai.tools.portfolio import portfolio_snapshot

    p = FakePlaid()
    await money.refresh(conn, p, TOKENS, TODAY - timedelta(days=1))
    await money.refresh(conn, p, TOKENS, TODAY)
    assert not money.ai_on(conn)  # opt-in: off until /money ai on
    money.set_ai(conn, True)

    ctx.money_on = True
    assert "Not available here" in await portfolio_snapshot({}, ctx)  # jobs, guests, /fix
    ctx.portfolio_ok = True
    money.set_ai(conn, False)
    assert "/money ai on" in await portfolio_snapshot({}, ctx)
    money.set_ai(conn, True)
    out = await portfolio_snapshot({}, ctx)
    assert ctx.private_seen
    assert "Mybroker brokerage: VTI (Vanguard Total), etf, 10 sh, $5,000.00" in out
    assert "% of holdings" in out
    assert "5555" not in out and "7777" not in out and BROKER not in out  # no masks, no tokens
    assert "Mybank" not in out and "checking" not in out  # holdings only
    assert (await remember({"fact": "Holds VTI"}, ctx)).startswith("Not saved")

    client = FakeClient(
        response([tool_block("portfolio_snapshot")], stop_reason="tool_use"),
        response([text_block("You're about 71% in VTI, so *fairly concentrated*.")]),
    )
    agent = Agent(client, conn, config, default_tools(), system_prompt="x")
    fresh = ctx.__class__(conn=conn, config=config, send=ctx.send, money_on=True, portfolio_ok=True)
    reply = await agent.run("am I too heavy in one fund?", fresh)
    assert reply.private
    rows = conn.execute("SELECT role, text FROM messages ORDER BY id").fetchall()
    assert rows[-1]["text"] == PRIVATE_NOTE and "71" not in rows[-1]["text"]
    shown = money.spoil_numbers(reply.text)
    assert not re.search(r"\d", visible(shown))
    assert "<tg-spoiler>71%</tg-spoiler>" in shown and "<b>fairly concentrated</b>" in shown


def test_spoiled_replies_keep_punctuation_and_valid_html():
    text = (
        'BND is about 13%, and I can\'t say "buy" for AT&T or <tickers> like VTI; $1,200.50 total.'
    )
    out = money.spoil_numbers(text)
    assert "can't" in out and '"buy"' in out and "&#" not in out
    assert "AT&amp;T" in out and "&lt;tickers&gt;" in out
    assert "<tg-spoiler>13%</tg-spoiler>" in out and "<tg-spoiler>$1,200.50</tg-spoiler>" in out
    assert not re.search(r"\d", visible(out))


def test_html_fallback_keeps_spoilered_amounts_hidden():
    from pai.bot import plain

    shown = plain("💵 <b>VTI</b> <tg-spoiler>$5,042.10</tg-spoiler> &amp; can't")
    assert shown == "💵 VTI ▒▒ & can't"
