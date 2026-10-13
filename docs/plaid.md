# Money (Plaid): setup

Pai reads money data from Plaid, **read-only**: balances and transactions from bank accounts
and credit cards, a card's due date and minimum (Liabilities), and holdings for daily P/L
from brokerage and retirement accounts (kept separate). It never uses Transfer or Payment
Initiation, and it can't move money.

## Environment variables (e.g. on Railway)

| Variable | Value |
|---|---|
| `PLAID_CLIENT_ID` | From the Plaid dashboard (Developers → Keys) |
| `PLAID_SECRET` | The **Sandbox** secret first; the Production (Trial) secret later |
| `PLAID_ENV` | `sandbox`, later `production` |
| `PAI_TOKEN_KEY` | Any long random string (32+ characters). It encrypts the access tokens Pai stores when you link from your phone. Keep it: if it changes, the stored links stop working and you'd link again. |
| `PLAID_ACCESS_TOKENS` | Optional. JSON of label to access token, only if you linked on a computer instead: `{"mybank": "access-..."}`. Wins over a phone link with the same label. |

Without `PLAID_CLIENT_ID` and `PLAID_SECRET`, money stays off and nothing else changes. Secrets
and tokens are never logged, are redacted from `/fix`, and are wiped from the weekly backup
Pai sends you.

## Linking from your phone (Plaid Hosted Link)

1. Set the variables above on Railway and redeploy.
2. In Telegram: `/money link mybank` (any short name). Pai replies with a **🔗 Link Mybank** button.
3. Tap it: it opens Plaid in Safari. Pick your bank and log in. In Sandbox, use
   `user_good` / `pass_good` on any test bank.
4. Pai checks every 30 seconds and confirms "✅ Mybank linked (N accounts, read-only)". The link
   works once and for 30 minutes; if it expires or you back out, nothing is saved.
5. Do the same for a brokerage, e.g. `/money link mybroker` (labels with broker names or
   "broker"/"ira" ask for Investments only).

Pai swaps the result for an access token itself, encrypts it with `PAI_TOKEN_KEY` and keeps it
in its database. The token never appears in Telegram.

Not yet checked against a live Plaid account: Hosted Link in Sandbox, and whether your Trial
plan includes Hosted Link. If `/money link` replies with a Plaid error code, that's the answer;
the computer route below still works.

## Going from Sandbox to your real accounts (Trial)

1. In the Plaid dashboard, request Trial (Production) access for Transactions, Investments
   and Liabilities.
2. On Railway, set `PLAID_SECRET` to the **Production** secret and `PLAID_ENV=production`,
   then redeploy.
3. `/money link <name>` again for each account (Sandbox links don't carry over).
   Not every brokerage supports Plaid Investments: if one returns no investments, Pai
   shows the bank side only and logs one line.

## Linking on a computer instead (optional)

Clone Plaid's quickstart (`git clone https://github.com/plaid/quickstart`), run it with your
keys and `PLAID_PRODUCTS=transactions,investments,liabilities`, link the account, and put
the access token in `PLAID_ACCESS_TOKENS` on Railway. Never paste tokens into Telegram.

## Using it

- 16:15 daily: sync (balances, new transactions, card due date, holdings snapshot).
- 16:30 weekdays: the recap is followed by "💵 Money recap inside"; every amount, mask and
  date is behind a spoiler, so notifications show no numbers.
- Sunday review: a spoilered spending line.
- Chat: "how much did I spend this week?" — Pai sends the answer itself, behind spoilers.
- `/money` shows status (no amounts); `/money off` pauses without deleting, `/money on`
  resumes, `/money sync` syncs now.
- If an item needs re-linking (e.g. `ITEM_LOGIN_REQUIRED`), Pai says so once a day; re-run
  `/money link <label>` to link it again.

## Portfolio questions with Claude (opt-in)

By default the model never sees money: lookups ("what do I hold?", "how much did I spend?")
are answered by code and sent straight to you. You opted in to one exception, holdings
only, for analysis you ask for in your own chat with Pai ("am I too heavy in tech?"):

- A `portfolio_snapshot` tool gives Claude your positions: account type (brokerage or
  retirement), ticker, name, asset type, shares, value, % weight and today's change. No
  account numbers, card masks, tokens, balances or bank transactions.
- It works only in your direct chat, never in check-ins, recurring jobs, @mention replies or
  `/fix`.
- The reply comes back with every number behind a spoiler. That turn is kept in chat
  history only as "answered a portfolio question; details not kept", and nothing from it
  can be saved to long-term memory.
- `/money ai off` turns it off (the tool then says so); `/money ai on` turns it back on.

