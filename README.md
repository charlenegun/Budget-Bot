# Budget Bot

A Telegram bot that tracks a shared food budget for a two-person household.
Type an expense as a plain message — `chicken rice 4.50` — and it's logged
against the week's budget. No commands to memorize for the common case.

```
you:  chicken rice 4.50
bot:  👍

you:  /week
bot:  Week of Mon 8 Sep – Sun 14 Sep
      Spent: S$62.30 / S$200.00 (31%)
      ...
```

## Features

- **Free-text logging.** The parser recognizes an amount in a normal-looking
  message and ignores everything that isn't one — times, distances, durations,
  dates, questions — so the chat stays usable for actual conversation.
- **Weekly budget tracking** with 80% / 100% threshold alerts.
- **Lunch/dinner reminders** (12:00 / 19:00 local time) with one-tap amount
  buttons, skipped once everyone's already logged that meal.
- **Sunday wrap-up** and a running savings streak (`/savings`).
- **CSV export** (`/export`) of the full expense history.
- Multi-timezone and multi-currency per group (`/settz`, `/setcurrency`).

Full command list and daily-use notes are in [SETUP.md](SETUP.md#daily-use).

## Stack

Python 3.11+, [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v22 (async),
SQLite (WAL mode). Long polling, not webhooks — runs on a free-tier GCP
e2-micro under systemd with no inbound ports open.

| File | Purpose |
|---|---|
| [bot.py](bot.py) | Handlers, commands, summary rendering, threshold alerts |
| [db.py](db.py) | Schema and every SQL query |
| [parsing.py](parsing.py) | Message → expense, and the guards that reject non-expenses |
| [backup.sh](backup.sh) | Nightly SQLite backup via `.backup` (WAL-safe) |
| [foodbot.service](foodbot.service) | systemd unit for deployment |

## Setup

See [SETUP.md](SETUP.md) for the full walkthrough — creating the bot with
@BotFather, running it locally, and deploying it to a free GCP VM (about 30
minutes end to end).

Quick local run:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in TELEGRAM_TOKEN
python bot.py
```

## Design notes

- **Money is integer cents everywhere** — never floats. `fmt_money()` is the
  only place division happens.
- **Budgets are a history table**, not a mutable field, so past weeks keep
  the budget that was in force at the time.
- **Deletes are soft** (`deleted_at`); nothing is ever actually removed.
- **The parser is deliberately conservative.** A missed expense costs one
  `/add`; a false positive silently corrupts the data. When in doubt, it
  stays silent.

More detail in [CLAUDE.md](CLAUDE.md).

## Status

Built for one household's actual use. Phase 3 (export filters, multi-currency
FX) isn't built yet — see [CLAUDE.md](CLAUDE.md#not-yet-built-phase-3).

## License

[MIT](LICENSE)
