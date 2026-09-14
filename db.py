"""SQLite layer for the food budget bot.

All money is stored as integer cents. Never floats.
All dates are stored as 'YYYY-MM-DD' strings in the household's local timezone,
alongside a UTC timestamp, so week queries are plain BETWEEN comparisons.
"""

import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "foodbot.db")
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Asia/Singapore")
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "S$")

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS households (
  chat_id         INTEGER PRIMARY KEY,
  timezone        TEXT NOT NULL,
  currency        TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
  chat_id      INTEGER NOT NULL,
  user_id      INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  PRIMARY KEY (chat_id, user_id)
);

-- Budget history, not a single mutable number, so past weeks stay accurate.
CREATE TABLE IF NOT EXISTS budget_periods (
  chat_id        INTEGER NOT NULL,
  effective_from TEXT NOT NULL,
  amount_cents   INTEGER NOT NULL,
  PRIMARY KEY (chat_id, effective_from)
);

CREATE TABLE IF NOT EXISTS expenses (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id      INTEGER NOT NULL,
  user_id      INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  description  TEXT,
  category     TEXT NOT NULL DEFAULT 'food',
  meal_slot    TEXT,
  is_estimate  INTEGER NOT NULL DEFAULT 0,
  spent_at     TEXT NOT NULL,
  local_date   TEXT NOT NULL,
  source       TEXT NOT NULL,
  message_id   INTEGER,
  deleted_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_exp_week
  ON expenses(chat_id, local_date) WHERE deleted_at IS NULL;

-- Dedupes threshold warnings so you get told once, not on every entry.
CREATE TABLE IF NOT EXISTS alerts_sent (
  chat_id    INTEGER NOT NULL,
  week_start TEXT NOT NULL,
  threshold  INTEGER NOT NULL,
  PRIMARY KEY (chat_id, week_start, threshold)
);

-- One row per closed week, written by the Sunday-evening job. A ledger, not a
-- running total, so /savings can show a streak and re-derive the total if the
-- formula ever changes.
CREATE TABLE IF NOT EXISTS savings_ledger (
  chat_id      INTEGER NOT NULL,
  week_start   TEXT NOT NULL,
  budget_cents INTEGER NOT NULL,
  total_cents  INTEGER NOT NULL,
  saved_cents  INTEGER NOT NULL,
  PRIMARY KEY (chat_id, week_start)
);
"""


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    conn = connect()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def _query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = connect()
    with _lock:
        return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def week_start_of(d: date) -> date:
    """Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def local_today(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# households & members
# --------------------------------------------------------------------------

def all_households() -> list[sqlite3.Row]:
    return _query("SELECT * FROM households", ())


def ensure_household(chat_id: int) -> sqlite3.Row:
    row = _query("SELECT * FROM households WHERE chat_id = ?", (chat_id,))
    if row:
        return row[0]
    _exec(
        "INSERT INTO households (chat_id, timezone, currency, created_at) VALUES (?,?,?,?)",
        (chat_id, DEFAULT_TZ, DEFAULT_CURRENCY, utc_now_iso()),
    )
    return _query("SELECT * FROM households WHERE chat_id = ?", (chat_id,))[0]


def set_timezone(chat_id: int, tz_name: str) -> None:
    ZoneInfo(tz_name)  # raises if invalid
    _exec("UPDATE households SET timezone = ? WHERE chat_id = ?", (tz_name, chat_id))


def set_currency(chat_id: int, symbol: str) -> None:
    _exec("UPDATE households SET currency = ? WHERE chat_id = ?", (symbol, chat_id))


def ensure_member(chat_id: int, user_id: int, display_name: str) -> None:
    _exec(
        "INSERT INTO members (chat_id, user_id, display_name) VALUES (?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET display_name = excluded.display_name",
        (chat_id, user_id, display_name),
    )


def members_of(chat_id: int) -> list[sqlite3.Row]:
    return _query("SELECT user_id, display_name FROM members WHERE chat_id = ?", (chat_id,))


def migrate_chat(old_id: int, new_id: int) -> None:
    """Telegram changes chat_id when a group is upgraded to a supergroup."""
    for table in (
        "households", "members", "budget_periods", "expenses",
        "alerts_sent", "savings_ledger",
    ):
        _exec(f"UPDATE OR IGNORE {table} SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))
        _exec(f"DELETE FROM {table} WHERE chat_id = ?", (old_id,))


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------

def set_budget(chat_id: int, amount_cents: int, effective_from: date) -> None:
    _exec(
        "INSERT INTO budget_periods (chat_id, effective_from, amount_cents) VALUES (?,?,?) "
        "ON CONFLICT(chat_id, effective_from) DO UPDATE SET amount_cents = excluded.amount_cents",
        (chat_id, effective_from.isoformat(), amount_cents),
    )


def budget_for_week(chat_id: int, week_start: date) -> int | None:
    """The budget in force during that week: the latest one set on or before it."""
    rows = _query(
        "SELECT amount_cents FROM budget_periods "
        "WHERE chat_id = ? AND effective_from <= ? "
        "ORDER BY effective_from DESC LIMIT 1",
        (chat_id, week_start.isoformat()),
    )
    return rows[0]["amount_cents"] if rows else None


# --------------------------------------------------------------------------
# expenses
# --------------------------------------------------------------------------

def add_expense(
    chat_id: int,
    user_id: int,
    amount_cents: int,
    description: str | None,
    category: str = "food",
    meal_slot: str | None = None,
    is_estimate: bool = False,
    source: str = "message",
    message_id: int | None = None,
    local_date_override: date | None = None,
) -> int:
    hh = ensure_household(chat_id)
    d = local_date_override or local_today(hh["timezone"])
    cur = _exec(
        "INSERT INTO expenses (chat_id, user_id, amount_cents, description, category, "
        "meal_slot, is_estimate, spent_at, local_date, source, message_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            chat_id, user_id, amount_cents, description, category, meal_slot,
            1 if is_estimate else 0, utc_now_iso(), d.isoformat(), source, message_id,
        ),
    )
    return cur.lastrowid


def set_description(chat_id: int, expense_id: int, description: str, category: str | None = None) -> bool:
    if category is None:
        cur = _exec(
            "UPDATE expenses SET description = ? "
            "WHERE id = ? AND chat_id = ? AND deleted_at IS NULL",
            (description, expense_id, chat_id),
        )
    else:
        cur = _exec(
            "UPDATE expenses SET description = ?, category = ? "
            "WHERE id = ? AND chat_id = ? AND deleted_at IS NULL",
            (description, category, expense_id, chat_id),
        )
    return cur.rowcount > 0


def soft_delete(chat_id: int, expense_id: int) -> bool:
    cur = _exec(
        "UPDATE expenses SET deleted_at = ? "
        "WHERE id = ? AND chat_id = ? AND deleted_at IS NULL",
        (utc_now_iso(), expense_id, chat_id),
    )
    return cur.rowcount > 0


def last_expense_by(chat_id: int, user_id: int) -> sqlite3.Row | None:
    rows = _query(
        "SELECT * FROM expenses WHERE chat_id = ? AND user_id = ? AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (chat_id, user_id),
    )
    return rows[0] if rows else None


def expenses_between(chat_id: int, start: date, end: date) -> list[sqlite3.Row]:
    return _query(
        "SELECT e.*, COALESCE(m.display_name, 'someone') AS display_name "
        "FROM expenses e LEFT JOIN members m "
        "  ON m.chat_id = e.chat_id AND m.user_id = e.user_id "
        "WHERE e.chat_id = ? AND e.deleted_at IS NULL "
        "  AND e.local_date BETWEEN ? AND ? "
        "ORDER BY e.local_date, e.id",
        (chat_id, start.isoformat(), end.isoformat()),
    )


def slot_logged_user_ids(chat_id: int, d: date, meal_slot: str) -> set[int]:
    """Who has already logged an expense for this meal slot today."""
    rows = _query(
        "SELECT DISTINCT user_id FROM expenses "
        "WHERE chat_id = ? AND local_date = ? AND meal_slot = ? AND deleted_at IS NULL",
        (chat_id, d.isoformat(), meal_slot),
    )
    return {r["user_id"] for r in rows}


def all_expenses_ordered(chat_id: int) -> list[sqlite3.Row]:
    return _query(
        "SELECT e.*, COALESCE(m.display_name, 'someone') AS display_name "
        "FROM expenses e LEFT JOIN members m "
        "  ON m.chat_id = e.chat_id AND m.user_id = e.user_id "
        "WHERE e.chat_id = ? AND e.deleted_at IS NULL "
        "ORDER BY e.local_date, e.id",
        (chat_id,),
    )


def total_between(chat_id: int, start: date, end: date) -> int:
    rows = _query(
        "SELECT COALESCE(SUM(amount_cents), 0) AS t FROM expenses "
        "WHERE chat_id = ? AND deleted_at IS NULL AND local_date BETWEEN ? AND ?",
        (chat_id, start.isoformat(), end.isoformat()),
    )
    return rows[0]["t"]


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------

def alert_already_sent(chat_id: int, week_start: date, threshold: int) -> bool:
    return bool(_query(
        "SELECT 1 FROM alerts_sent WHERE chat_id = ? AND week_start = ? AND threshold = ?",
        (chat_id, week_start.isoformat(), threshold),
    ))


def record_alert(chat_id: int, week_start: date, threshold: int) -> None:
    _exec(
        "INSERT OR IGNORE INTO alerts_sent (chat_id, week_start, threshold) VALUES (?,?,?)",
        (chat_id, week_start.isoformat(), threshold),
    )


# --------------------------------------------------------------------------
# savings ledger
# --------------------------------------------------------------------------

def record_week_close(chat_id: int, week_start: date, budget_cents: int, total_cents: int) -> None:
    """Write once per week when it closes. INSERT OR IGNORE: a restart on the
    same Sunday must not double-count or overwrite the original figure."""
    _exec(
        "INSERT OR IGNORE INTO savings_ledger "
        "(chat_id, week_start, budget_cents, total_cents, saved_cents) VALUES (?,?,?,?,?)",
        (chat_id, week_start.isoformat(), budget_cents, total_cents, budget_cents - total_cents),
    )


def savings_summary(chat_id: int) -> dict:
    """Cumulative saved_cents (negative if net overspent) and the current
    streak of consecutive under-budget weeks, most recent first."""
    rows = _query(
        "SELECT saved_cents FROM savings_ledger WHERE chat_id = ? ORDER BY week_start DESC",
        (chat_id,),
    )
    streak = 0
    for r in rows:
        if r["saved_cents"] <= 0:
            break
        streak += 1
    return {
        "weeks_recorded": len(rows),
        "total_saved_cents": sum(r["saved_cents"] for r in rows),
        "streak": streak,
    }
