"""Read-only money from Plaid: bank balances and transactions, credit card due dates, and
daily investment snapshots for P/L.

Products used: Transactions, Balance (via the free cached /accounts/get), Investments and
Liabilities. Never Transfer or Payment Initiation. Credentials come from the environment;
unset or malformed means money is off. Every number shown to the owner sits behind a
Telegram spoiler, and money never reaches the model, long-term memory, the heartbeat or
/fix. Recaps and answers are computed here, with no model calls.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pai import db, errorlog
from pai.spend import utc_iso

log = logging.getLogger(__name__)

PAUSED_KEY = "money_paused"
AI_KEY = "money_ai"  # holdings may go to the model for portfolio questions (opt-in)
LINK_SECONDS = 30 * 60  # how long a phone link stays usable
LABEL_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
KEEP_DAYS = 400  # ~13 months of transactions
RETIREMENT = {
    "ira",
    "roth",
    "roth 401k",
    "401k",
    "403b",
    "457b",
    "sep ira",
    "simple ira",
    "keogh",
    "pension",
    "retirement",
    "401a",
    "rollover",
}
RELINK = {
    "ITEM_LOGIN_REQUIRED",
    "PENDING_EXPIRATION",
    "ITEM_LOCKED",
    "ACCESS_NOT_GRANTED",
    "INVALID_CREDENTIALS",
    "USER_SETUP_REQUIRED",
    "MFA_NOT_SUPPORTED",
    "NO_ACCOUNTS",
    "ITEM_NOT_SUPPORTED",
    "INVALID_ACCESS_TOKEN",
}
NOT_OFFERED = {
    "PRODUCTS_NOT_SUPPORTED",
    "NO_INVESTMENT_ACCOUNTS",
    "NO_LIABILITY_ACCOUNTS",
    "PRODUCT_NOT_READY",
    "NO_INVESTMENT_AUTH_ACCOUNTS",
    "INVALID_PRODUCT",
}
NOT_SPENDING = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "INCOME", "BANK_FEES_REFUND"}
KIND_NAMES = {
    "checking": "checking",
    "savings": "savings",
    "credit": "credit card",
    "brokerage": "brokerage",
    "retirement": "IRA (retirement)",
    "other": "account",
}
LABELS: dict[str, str] = {}  # display names per item label, from [money] labels in config


@dataclass
class Settings:
    client_id: str
    secret: str
    env: str
    tokens: dict[str, str]


class PlaidError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def settings(secrets) -> Settings | None:
    """Plaid settings from the environment, or None (money off). The client ID and secret
    turn money on; PLAID_ACCESS_TOKENS is optional now that /money link can add items.
    Malformed tokens turn money off with one warning. Secret values get redacted."""
    if not (secrets.plaid_client_id and secrets.plaid_secret):
        return None
    tokens: dict[str, str] = {}
    raw = secrets.plaid_access_tokens.get_secret_value() if secrets.plaid_access_tokens else ""
    if raw.strip():
        try:
            tokens = json.loads(raw)
            if not isinstance(tokens, dict) or not all(isinstance(v, str) for v in tokens.values()):
                raise ValueError
        except ValueError:
            log.warning("PLAID_ACCESS_TOKENS isn't a JSON object of label to token; money is off")
            return None
    secret = secrets.plaid_secret.get_secret_value()
    for value in (secret, raw, *tokens.values()):
        errorlog.LOG.add_secret(value)
    return Settings(secrets.plaid_client_id, secret, secrets.plaid_env.lower(), tokens)


# ---- tokens Pai links itself, encrypted at rest ---------------------------------


def cipher(key: str | None):
    """A Fernet cipher from PAI_TOKEN_KEY (any string; hashed to a key), or None."""
    if not key or len(key) < 16:
        return None
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def save_token(conn: sqlite3.Connection, f, label: str, token: str) -> None:
    errorlog.LOG.add_secret(token)
    sealed = f.encrypt(token.encode()).decode()
    conn.execute(
        "INSERT INTO money_items (label, access_token) VALUES (?, ?)"
        " ON CONFLICT (label) DO UPDATE SET access_token = excluded.access_token,"
        " cursor = NULL, last_error = NULL",
        (label, sealed),
    )


def tokens(conn: sqlite3.Connection, s: Settings, f) -> dict[str, str]:
    """Every item's access token: linked ones from the DB, then env ones (env wins)."""
    out: dict[str, str] = {}
    if f is not None:
        from cryptography.fernet import InvalidToken

        for r in conn.execute(
            "SELECT label, access_token FROM money_items WHERE access_token IS NOT NULL"
        ):
            try:
                out[r["label"]] = f.decrypt(r["access_token"].encode()).decode()
            except InvalidToken:
                log.warning(
                    "money: stored token for %s doesn't decrypt (PAI_TOKEN_KEY changed?)",
                    r["label"],
                )
    return {**out, **s.tokens}


def load_secrets(conn: sqlite3.Connection, s: Settings | None, f) -> None:
    """At startup: redact every token Pai holds from logs and /fix."""
    if s is not None:
        for t in tokens(conn, s, f).values():
            errorlog.LOG.add_secret(t)


def products_for(label: str) -> tuple[list[str], list[str]]:
    """(required, optional) Plaid products for a new link: brokerages need investments only;
    banks need transactions, and liabilities/investments if the institution has them."""
    if re.search(r"robinhood|fidelity|schwab|vanguard|etrade|webull|broker|ira", label):
        return ["investments"], []
    return ["transactions"], ["liabilities", "investments"]


class Plaid:
    """Thin async wrapper over plaid-python (which is synchronous). Returns plain dicts."""

    def __init__(self, s: Settings):
        import plaid
        from plaid.api import plaid_api

        host = plaid.Environment.Production if s.env == "production" else plaid.Environment.Sandbox
        config = plaid.Configuration(
            host=host, api_key={"clientId": s.client_id, "secret": s.secret}
        )
        self.api = plaid_api.PlaidApi(plaid.ApiClient(config))

    async def _call(self, method: str, request) -> dict:
        import plaid

        try:
            r = await asyncio.to_thread(getattr(self.api, method), request)
        except plaid.ApiException as e:
            try:
                code = json.loads(e.body or "{}").get("error_code") or "UNKNOWN"
            except ValueError:
                code = f"HTTP_{e.status}"
            raise PlaidError(code) from None
        return r.to_dict()

    async def accounts(self, token: str) -> list[dict]:
        from plaid.model.accounts_get_request import AccountsGetRequest

        return (await self._call("accounts_get", AccountsGetRequest(access_token=token)))[
            "accounts"
        ]

    async def transactions(self, token: str, cursor: str | None) -> dict:
        """All changes since `cursor`: {added, modified, removed, next_cursor}."""
        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        out = {"added": [], "modified": [], "removed": [], "next_cursor": cursor}
        while True:
            kw = {"cursor": out["next_cursor"]} if out["next_cursor"] else {}
            r = await self._call(
                "transactions_sync", TransactionsSyncRequest(access_token=token, **kw)
            )
            for k in ("added", "modified", "removed"):
                out[k] += r[k]
            out["next_cursor"] = r["next_cursor"]
            if not r["has_more"]:
                return out

    async def holdings(self, token: str) -> dict:
        from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest

        return await self._call(
            "investments_holdings_get", InvestmentsHoldingsGetRequest(access_token=token)
        )

    async def link(self, label: str) -> dict:
        """A one-time Hosted Link for a new item: {link_token, hosted_link_url, expiration}."""
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_hosted_link import LinkTokenCreateHostedLink
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products

        required, optional = products_for(label)
        extra = {"optional_products": [Products(p) for p in optional]} if optional else {}
        req = LinkTokenCreateRequest(
            client_name="Pai",
            language="en",
            country_codes=[CountryCode("US")],
            user=LinkTokenCreateRequestUser(client_user_id="pai-owner"),
            products=[Products(p) for p in required],
            hosted_link=LinkTokenCreateHostedLink(url_lifetime_seconds=LINK_SECONDS),
            **extra,
        )
        return await self._call("link_token_create", req)

    async def link_result(self, link_token: str) -> dict:
        """{public_token} once the owner finished, {exited} if they backed out, else {}."""
        from plaid.model.link_token_get_request import LinkTokenGetRequest

        r = await self._call("link_token_get", LinkTokenGetRequest(link_token=link_token))
        exited = False
        for session in r.get("link_sessions") or []:
            for item in ((session.get("results") or {}).get("item_add_results")) or []:
                if item.get("public_token"):
                    return {"public_token": item["public_token"]}
            exited = exited or bool(session.get("exit") or session.get("on_exit"))
        return {"exited": True} if exited else {}

    async def exchange(self, public_token: str) -> str:
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

        r = await self._call(
            "item_public_token_exchange", ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        return r["access_token"]

    async def liabilities(self, token: str) -> dict:
        from plaid.model.liabilities_get_request import LiabilitiesGetRequest

        return await self._call("liabilities_get", LiabilitiesGetRequest(access_token=token))


def kind_of(account: dict) -> str:
    t, sub = str(account.get("type") or ""), str(account.get("subtype") or "").lower()
    if t == "depository":
        return "checking" if sub == "checking" else "savings" if sub == "savings" else "other"
    if t == "credit":
        return "credit"
    if t == "investment":
        return "retirement" if sub in RETIREMENT or "ira" in sub.split() else "brokerage"
    return "other"


def paused(conn: sqlite3.Connection) -> bool:
    return db.kv_get(conn, PAUSED_KEY) == "1"


def set_paused(conn: sqlite3.Connection, on: bool) -> None:
    db.kv_set(conn, PAUSED_KEY, "1" if on else "0")


def ai_on(conn: sqlite3.Connection) -> bool:
    return db.kv_get(conn, AI_KEY) == "1"


def set_ai(conn: sqlite3.Connection, on: bool) -> None:
    db.kv_set(conn, AI_KEY, "1" if on else "0")


def portfolio_for_model(conn: sqlite3.Connection, today: date) -> str:
    """Holdings as plain text for the model: account type, ticker, name, type, shares,
    value, weight and today's change. No account numbers, masks, balances or bank data."""
    rows = positions(conn, today)
    if not rows:
        return "No holdings synced yet."
    kinds = {a["account_id"]: a for a in accounts(conn)}
    total = sum(h["value"] for h in rows) or 1.0
    lines = [f"Holdings as of {rows[0]['day']} (total {usd(total)}):"]
    for h in rows:
        a = kinds.get(h["account_id"])
        where = f"{display(a['item'])} {a['kind']}" if a else "investment account"
        what = h["ticker"] or "?"
        if h["name"]:
            what += f" ({h['name']})"
        change = f", {usd(h['value'] - h['prev_value'], True)} today" if h["prev_value"] else ""
        qty = f"{shares(h['quantity'])} sh, " if h["quantity"] is not None else ""
        lines.append(
            f"- {where}: {what}, {h['security_type'] or 'unknown type'}, {qty}"
            f"{usd(h['value'])}, {h['value'] / total * 100:.1f}% of holdings{change}"
        )
    return "\n".join(lines)


# A number, but never the digits of an HTML entity like &#39; (that broke the message).
NUMBER = re.compile(r"(?<![&#\w])[-+−]?\$?\d[\d,]*(?:\.\d+)?%?")


def spoil_numbers(text: str) -> str:
    """A model reply as Telegram HTML with every number behind a spoiler; *bold* kept."""
    out = html.escape(text, quote=False)  # apostrophes stay apostrophes
    out = re.sub(r"\*\*(.+?)\*\*|\*(.+?)\*", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    return NUMBER.sub(lambda m: f"<tg-spoiler>{m.group(0)}</tg-spoiler>", out)


def _day(v) -> str:
    return v.isoformat() if isinstance(v, date) else str(v)[:10]


async def refresh(
    conn: sqlite3.Connection, client, tokens: dict[str, str], today: date
) -> list[str]:
    """Sync every item. Returns alerts (an item that needs re-linking), at most one per item
    per day. An item with no investments or liabilities just skips that part."""
    alerts = []
    now = utc_iso(datetime.now().astimezone())
    for label, token in tokens.items():
        conn.execute("INSERT OR IGNORE INTO money_items (label) VALUES (?)", (label,))
        item = conn.execute("SELECT * FROM money_items WHERE label = ?", (label,)).fetchone()
        try:
            accounts = await client.accounts(token)
            kinds = {}
            for a in accounts:
                kinds[a["account_id"]] = kind_of(a)
                b = a.get("balances") or {}
                conn.execute(
                    "INSERT INTO money_accounts (account_id, item, kind, mask, balance, available,"
                    " credit_limit, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (account_id) DO UPDATE SET kind = excluded.kind,"
                    " mask = excluded.mask, balance = excluded.balance,"
                    " available = excluded.available, credit_limit = excluded.credit_limit,"
                    " updated_at = excluded.updated_at",
                    (
                        a["account_id"],
                        label,
                        kinds[a["account_id"]],
                        a.get("mask"),
                        b.get("current"),
                        b.get("available"),
                        b.get("limit"),
                        now,
                    ),
                )
            if any(k in ("checking", "savings", "credit", "other") for k in kinds.values()):
                await _sync_transactions(conn, client, label, token, item["cursor"])
            if "credit" in kinds.values():
                await _optional(label, "liabilities", _liabilities(conn, client, token))
            if any(k in ("brokerage", "retirement") for k in kinds.values()):
                await _optional(label, "investments", _holdings(conn, client, token, today))
            conn.execute(
                "UPDATE money_items SET last_error = NULL, synced_at = ? WHERE label = ?",
                (now, label),
            )
        except PlaidError as e:
            log.warning("plaid %s failed: %s", label, e.code)
            conn.execute("UPDATE money_items SET last_error = ? WHERE label = ?", (e.code, label))
            if e.code in RELINK and item["error_alerted"] != today.isoformat():
                conn.execute(
                    "UPDATE money_items SET error_alerted = ? WHERE label = ?",
                    (today.isoformat(), label),
                )
                alerts.append(
                    f"🔌 {display(label)} needs re-linking ({e.code}): `/money link {label}`"
                )
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    conn.execute("DELETE FROM money_transactions WHERE day < ?", (cutoff,))
    return alerts


async def _optional(label: str, what: str, job) -> None:
    try:
        await job
    except PlaidError as e:
        if e.code not in NOT_OFFERED:
            raise
        log.info("plaid %s: no %s (%s); showing the rest", label, what, e.code)


async def _sync_transactions(conn, client, label: str, token: str, cursor: str | None) -> None:
    r = await client.transactions(token, cursor)
    for t in r["added"] + r["modified"]:
        pfc = t.get("personal_finance_category") or {}
        conn.execute(
            "INSERT INTO money_transactions (id, account_id, day, amount, merchant, category,"
            " pending) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET"
            " day = excluded.day, amount = excluded.amount, merchant = excluded.merchant,"
            " category = excluded.category, pending = excluded.pending",
            (
                t["transaction_id"],
                t["account_id"],
                _day(t["date"]),
                t["amount"],
                t.get("merchant_name") or t.get("name"),
                pfc.get("primary"),
                int(bool(t.get("pending"))),
            ),
        )
    conn.executemany(
        "DELETE FROM money_transactions WHERE id = ?",
        [(t["transaction_id"],) for t in r["removed"]],
    )
    conn.execute("UPDATE money_items SET cursor = ? WHERE label = ?", (r["next_cursor"], label))


async def _liabilities(conn, client, token: str) -> None:
    r = await client.liabilities(token)
    for c in (r.get("liabilities") or {}).get("credit") or []:
        conn.execute(
            "UPDATE money_accounts SET due_date = ?, minimum_due = ? WHERE account_id = ?",
            (
                _day(c["next_payment_due_date"]) if c.get("next_payment_due_date") else None,
                c.get("minimum_payment_amount"),
                c["account_id"],
            ),
        )


async def _holdings(conn, client, token: str, today: date) -> None:
    r = await client.holdings(token)
    securities = {s["security_id"]: s for s in r.get("securities") or []}
    ids = [a["account_id"] for a in r.get("accounts") or []]
    if ids:  # today's snapshot is replaced, not added to
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM holdings_snapshots WHERE day = ? AND account_id IN ({marks})",
            (today.isoformat(), *ids),
        )
    for h in r.get("holdings") or []:
        conn.execute(
            "INSERT OR REPLACE INTO holdings_snapshots (day, account_id, security_id, ticker,"
            " quantity, price, value, cost_basis, name, security_type)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                today.isoformat(),
                h["account_id"],
                h["security_id"],
                securities.get(h["security_id"], {}).get("ticker_symbol"),
                h.get("quantity"),
                h.get("institution_price"),
                h.get("institution_value") or 0,
                h.get("cost_basis"),
                securities.get(h["security_id"], {}).get("name"),
                securities.get(h["security_id"], {}).get("type"),
            ),
        )


# ---- numbers -----------------------------------------------------------------


def accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    order = (
        "CASE kind WHEN 'checking' THEN 0 WHEN 'savings' THEN 1 WHEN 'credit' THEN 2"
        " WHEN 'brokerage' THEN 3 WHEN 'retirement' THEN 4 ELSE 5 END"
    )
    return conn.execute(f"SELECT * FROM money_accounts ORDER BY {order}, item").fetchall()


def spending(conn: sqlite3.Connection, start: date, end: date) -> tuple[float, int]:
    """(total out, purchases) for [start, end], leaving out transfers, card payments, income."""
    marks = ",".join("?" * len(NOT_SPENDING))
    row = conn.execute(
        "SELECT COALESCE(SUM(t.amount), 0), COUNT(*) FROM money_transactions t"
        " JOIN money_accounts a ON a.account_id = t.account_id"
        f" WHERE t.day BETWEEN ? AND ? AND t.amount > 0 AND a.kind IN ('checking', 'credit')"
        f" AND COALESCE(t.category, '') NOT IN ({marks})",
        (start.isoformat(), end.isoformat(), *NOT_SPENDING),
    ).fetchone()
    return float(row[0]), int(row[1])


def top_categories(conn, start: date, end: date, n: int = 3) -> list[tuple[str, float]]:
    marks = ",".join("?" * len(NOT_SPENDING))
    rows = conn.execute(
        "SELECT COALESCE(t.category, 'OTHER') c, SUM(t.amount) s FROM money_transactions t"
        " JOIN money_accounts a ON a.account_id = t.account_id"
        " WHERE t.day BETWEEN ? AND ? AND t.amount > 0 AND a.kind IN ('checking', 'credit')"
        f" AND COALESCE(t.category, '') NOT IN ({marks}) GROUP BY c ORDER BY s DESC LIMIT ?",
        (start.isoformat(), end.isoformat(), *NOT_SPENDING, n),
    ).fetchall()
    return [(r["c"].replace("_", " ").lower(), float(r["s"])) for r in rows]


def values_on(conn: sqlite3.Connection, day: date) -> dict[str, float]:
    rows = conn.execute(
        "SELECT account_id, SUM(value) v FROM holdings_snapshots WHERE day = ? GROUP BY account_id",
        (day.isoformat(),),
    ).fetchall()
    return {r["account_id"]: float(r["v"]) for r in rows}


def daily_pl(conn: sqlite3.Connection, today: date) -> dict[str, tuple[float, float | None]]:
    """account_id -> (value today, change since the previous snapshot day). Today's value is
    the latest holdings snapshot; a live-quote source could replace it later (hook here)."""
    now = values_on(conn, today)
    prev = conn.execute(
        "SELECT MAX(day) FROM holdings_snapshots WHERE day < ?", (today.isoformat(),)
    ).fetchone()[0]
    before = values_on(conn, date.fromisoformat(prev)) if prev else {}
    return {aid: (v, v - before[aid] if aid in before else None) for aid, v in now.items()}


def positions(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    """Each holding in the latest snapshot (up to today) with its change since the snapshot
    before it, largest first within each account."""
    day = conn.execute(
        "SELECT MAX(day) FROM holdings_snapshots WHERE day <= ?", (today.isoformat(),)
    ).fetchone()[0]
    if day is None:
        return []
    prev = conn.execute("SELECT MAX(day) FROM holdings_snapshots WHERE day < ?", (day,)).fetchone()[
        0
    ]
    return conn.execute(
        "SELECT h.*, p.value AS prev_value FROM holdings_snapshots h"
        " LEFT JOIN holdings_snapshots p ON p.day = ? AND p.account_id = h.account_id"
        " AND p.security_id = h.security_id WHERE h.day = ? ORDER BY h.account_id, h.value DESC",
        (prev, day),
    ).fetchall()


# ---- rendering (Telegram HTML; every number behind a spoiler) -------------------


def display(label: str) -> str:
    return LABELS.get(label.lower(), re.sub(r"\d", "", label).strip().capitalize() or "Account")


def hidden(text: str) -> str:
    return f"<tg-spoiler>{html.escape(text)}</tg-spoiler>"


def usd(v: float, sign: bool = False) -> str:
    s = f"${abs(v):,.2f}"
    return (("+" if v >= 0 else "−") + s) if sign else ("−" + s if v < 0 else s)


def name_of(a: sqlite3.Row) -> str:
    base = f"{display(a['item'])} {KIND_NAMES.get(a['kind'], 'account')}"
    return html.escape(base) + (f" {hidden('••' + a['mask'])}" if a["mask"] else "")


INVESTED = ("brokerage", "retirement")
TOPICS = {
    "balances": ("checking", "savings", "other"),
    "credit": ("credit",),
    "investments": INVESTED,
}


def _line(a: sqlite3.Row, pl: dict) -> str:
    if a["kind"] in INVESTED:
        v, change = pl.get(a["account_id"], (a["balance"] or 0.0, None))
        ch = ""
        if change is not None and v - change:
            ch = f" {hidden(f'{usd(change, True)} ({change / (v - change) * 100:+.2f}%) today')}"
        return f"{name_of(a)}: {hidden(usd(v))}{ch}"
    if a["kind"] == "credit":
        line = f"{name_of(a)}: {hidden(usd(a['balance'] or 0))} owed"
        if a["minimum_due"] is not None and a["due_date"]:
            due = date.fromisoformat(a["due_date"])
            line += f", minimum {hidden(usd(a['minimum_due']))} due {hidden(f'{due:%a %m/%d}')}"
        return line
    cash = a["available"] if a["available"] is not None else a["balance"] or 0.0
    return f"{name_of(a)}: {hidden(usd(cash))}"


def _invested_total(rows, pl: dict) -> str | None:
    invested = [a for a in rows if a["kind"] in INVESTED]
    if len(invested) < 2:
        return None
    total = sum(pl.get(a["account_id"], (a["balance"] or 0.0, None))[0] for a in invested)
    change = sum(pl.get(a["account_id"], (0.0, None))[1] or 0.0 for a in invested)
    return f"Investments together: {hidden(usd(total))} {hidden(usd(change, True) + ' today')}"


TYPE_NAMES = {
    "etf": "ETF",
    "cryptocurrency": "crypto",
    "mutual fund": "fund",
    "cash": "cash",
    "equity": "stock",
    "fixed income": "bond",
    "derivative": "option",
}
MAX_POSITIONS = 25  # per account


def shares(q: float) -> str:
    """Up to 6 decimals (crypto and fractional shares), no trailing zeros."""
    return f"{q:,.6f}".rstrip("0").rstrip(".")


def _label(text: str | None) -> str:
    """A ticker or name, shown plainly unless it has digits (then it's hidden too)."""
    text = (text or "?").strip()
    return hidden(text) if re.search(r"\d", text) else html.escape(text)


def holdings_text(conn: sqlite3.Connection, today: date) -> str:
    """Every position per investment account: ticker, type, shares, value and today's
    change, all numbers behind spoilers."""
    rows = positions(conn, today)
    if not rows:
        return "📈 No positions synced yet. Try /money sync."
    names = {a["account_id"]: a for a in accounts(conn)}
    out = ["📈 Your positions, inside"]
    shown: dict[str, int] = {}
    for h in rows:
        aid = h["account_id"]
        if aid not in shown:
            shown[aid] = 0
            acct = names.get(aid)
            out.append(f"\n<b>{name_of(acct) if acct else 'Investments'}</b>")
        shown[aid] += 1
        if shown[aid] > MAX_POSITIONS:
            continue
        kind = TYPE_NAMES.get((h["security_type"] or "").lower(), "")
        what = _label(h["ticker"] or h["name"]) + (f" ({kind})" if kind else "")
        parts = []
        if h["quantity"] is not None and kind != "cash":
            parts.append(f"{shares(h['quantity'])} sh")
        parts.append(usd(h["value"]))
        if h["prev_value"]:
            parts.append(f"{usd(h['value'] - h['prev_value'], True)} today")
        out.append(f"• {what} {hidden(' · '.join(parts))}")
    extra = [f"{n - MAX_POSITIONS} more not shown" for n in shown.values() if n > MAX_POSITIONS]
    if extra:
        out.append(hidden(", ".join(extra)))
    return "\n".join(out)


def recap(conn: sqlite3.Connection, today: date) -> str | None:
    """The 4:30 money message, or None when nothing has synced yet."""
    rows = accounts(conn)
    if not rows:
        return None
    spent, n = spending(conn, today, today)
    pl = daily_pl(conn, today)
    out = [
        "💵 Money recap inside",
        f"Spent today: {hidden(usd(spent))} {hidden(f'({n} purchases)')}",
    ]
    out += [_line(a, pl) for a in rows]
    total = _invested_total(rows, pl)
    return "\n".join(out + ([total] if total else []))


def _spent(conn, start: date, end: date, title: str) -> list[str]:
    spent, n = spending(conn, start, end)
    tops = top_categories(conn, start, end)
    lines = [title, hidden(f"{usd(spent)} across {n} purchases")]
    if tops:
        lines.append("Most on: " + hidden(", ".join(f"{c} {usd(v)}" for c, v in tops)))
    return lines


def week_line(conn: sqlite3.Connection, start: date, end: date) -> str | None:
    if not accounts(conn):
        return None
    return "\n".join(_spent(conn, start, end, "💵 This week's spending inside"))


def answer(conn: sqlite3.Connection, topic: str, start: date, end: date, today: date) -> str:
    """A spoilered HTML answer for chat, computed here; no model sees the numbers."""
    rows = accounts(conn)
    if not rows:
        return "💵 No money data synced yet."
    if topic == "spending":
        return "\n".join(_spent(conn, start, end, "💵 Your spending, inside"))
    if topic == "holdings":
        return holdings_text(conn, today)
    if topic not in TOPICS:
        return recap(conn, today) or "💵 No money data synced yet."
    pl = daily_pl(conn, today)
    picked = [_line(a, pl) for a in rows if a["kind"] in TOPICS[topic]]
    if topic == "investments" and (total := _invested_total(rows, pl)):
        picked.append(total)
    return (
        "\n".join(["💵 Here it is, inside", *picked]) if picked else "💵 Nothing to show for that."
    )
