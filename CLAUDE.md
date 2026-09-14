# Food budget Telegram bot

A Telegram bot for a two-person household group chat. Members type expenses as
plain messages (`chicken rice 4.50`); the bot logs them and tracks a weekly budget.

## Stack

- Python 3.11+, `python-telegram-bot` v22 (async)
- SQLite, WAL mode, one file
- Long polling (not webhooks) — deployed on a GCP e2-micro under systemd

## Layout

| File | Purpose |
|---|---|
| `bot.py` | Handlers, commands, summary rendering, threshold alerts |
| `db.py` | Schema and every SQL query. No SQL outside this file. |
| `parsing.py` | Message → expense, and the guards that reject non-expenses |
| `SETUP.md` | Deployment guide |

## Conventions that matter

- **Money is integer cents everywhere.** Never floats. `fmt_money()` is the only
  place division happens.
- **No SQL outside `db.py`.** Add a helper function instead.
- **Budgets are a history table** (`budget_periods`), not a mutable field, so past
  weeks keep the budget that was in force at the time.
- **Deletes are soft** (`deleted_at`). Every read must filter `deleted_at IS NULL`.
- **Each expense stores both `spent_at` (UTC) and `local_date`.** Week queries use
  `local_date` so they stay a plain `BETWEEN`.
- **The parser is deliberately conservative.** When unsure, return `None` and stay
  silent. A missed expense costs one `/add`; a false positive corrupts the data and
  makes the group annoying to talk in. Do not loosen the guards in
  `parsing.NOT_MONEY` without adding test cases both ways.
- **Confirm with a 👍 reaction, not a reply.** Chat messages per expense make the
  group unusable.

## Testing

There is no test suite yet. When changing `parsing.py`, check both lists:

```python
# should log
"chicken rice 4.50", "$12 pizza", "groceries 43.20", "12", "S$8.90 kopi", "coffee 5,50"
# should be ignored
"let's meet at 7", "see you at 7:30pm", "i'll be there in 20 mins",
"5km run today", "27 Jan", "how much did you spend?"
```

Watch for regex guards that accidentally eat decimals — `\d{1,2}[:.]\d{2}` matches
`4.50` as readily as `7.30`, which silently broke every decimal amount once already.

## Phase 2 (built)

- 12:00 and 19:00 reminders with inline bucket buttons (`<$5`, `$5–10`, …), via
  PTB's `JobQueue`. Skips the prompt if every known member already logged that
  slot today. Jobs are re-registered from `db.all_households()` in `post_init`
  (and rescheduled on `/settz`) since `JobQueue` is in-memory and would
  otherwise go silent after a restart.
- Tapping a bucket logs an `is_estimate=1` expense, then asks for an optional
  description via a `ForceReply` follow-up message. The prompt must @-mention
  the tapper (`ForceReply(selective=True)` only forces the keyboard for a
  mentioned user or the sender it replies to — learned the hard way, see
  `on_meal_button`). The pending prompt lives in the in-memory
  `PENDING_DESCRIPTION` dict, same lost-on-restart tradeoff as `JobQueue`.
- Scheduled Sunday-evening weekly summary (`weekly_summary_job`), which also
  writes that week's row to `savings_ledger`.
- `/savings` — cumulative saved/overspent and the current under-budget streak,
  read from `savings_ledger` (one row per closed week, `INSERT OR IGNORE` so a
  restart on the same Sunday can't double-count).
- `/export` — all expenses as a CSV document.

## Not yet built (Phase 3)

- `/export` filters (date range, category)
- Multi-currency / FX if a household splits time across countries

## Don't

- Don't read or print `.env` — it holds the bot token.
- Don't commit `foodbot.db` or `.env`.
- Don't switch to webhooks; the deployment has no inbound ports open by design.
