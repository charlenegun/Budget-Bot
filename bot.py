"""Food budget bot -- Phase 1 & 2.

Logs expenses typed into a group chat, tracks them against a weekly budget,
warns when you're burning through it, nudges at meal times if nobody's
logged, sends a Sunday wrap-up, and tracks cumulative savings.
"""

import csv
import io
import logging
import os
from collections import defaultdict
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from parsing import categorize, fmt_money, parse_explicit, parse_message

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("foodbot")

ALERT_THRESHOLDS = (80, 100)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MEAL_SLOTS = {"lunch": time(12, 0), "dinner": time(19, 0)}
WEEKLY_SUMMARY_TIME = time(20, 0)
WEEKLY_SUMMARY_DAYS = (6,)  # PTB weekday numbers: 0=Mon .. 6=Sun

# Bucket amounts are in absolute cents, not scaled per currency: a quick
# estimate button, not a precise entry. is_estimate=True marks them as such.
MEAL_BUCKETS = [
    (250, "under {sym}5"),
    (750, "{sym}5–10"),
    (1500, "{sym}10–20"),
    (2500, "{sym}20+"),
]

# message_id of the "what was it?" prompt -> expense id awaiting a description.
# In-memory like JobQueue: an unanswered prompt is simply lost on restart.
PENDING_DESCRIPTION: dict[int, int] = {}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def household_context(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Register the chat and sender, return (household_row, chat_id, user_id)."""
    chat = update.effective_chat
    user = update.effective_user
    hh = db.ensure_household(chat.id)
    if user:
        name = user.first_name or user.username or str(user.id)
        db.ensure_member(chat.id, user.id, name)
    if ctx.job_queue and not ctx.job_queue.get_jobs_by_name(f"weekly-{chat.id}"):
        schedule_household_jobs(ctx.job_queue, chat.id, hh["timezone"])
    return hh, chat.id, (user.id if user else 0)


def schedule_household_jobs(job_queue, chat_id: int, tz_name: str) -> None:
    """(Re)register a household's reminder and weekly-summary jobs under its
    timezone. JobQueue is in-memory, so this runs both at startup (post_init,
    for every known household) and whenever a timezone changes."""
    tz = ZoneInfo(tz_name)
    specs = [
        (f"lunch-{chat_id}", remind_meal, MEAL_SLOTS["lunch"], {"slot": "lunch"}, None),
        (f"dinner-{chat_id}", remind_meal, MEAL_SLOTS["dinner"], {"slot": "dinner"}, None),
        (f"weekly-{chat_id}", weekly_summary_job, WEEKLY_SUMMARY_TIME, {}, WEEKLY_SUMMARY_DAYS),
    ]
    for name, callback, t, data, days in specs:
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
        kwargs = dict(time=t.replace(tzinfo=tz), chat_id=chat_id, name=name, data=data)
        if days is not None:
            kwargs["days"] = days
        job_queue.run_daily(callback, **kwargs)


async def react_ok(update: Update, emoji: str = "👍") -> None:
    """Confirm silently. Falls back to nothing if reactions aren't permitted."""
    try:
        await update.effective_message.set_reaction(emoji)
    except (BadRequest, Forbidden) as exc:
        log.debug("reaction failed: %s", exc)


def week_bounds(d: date) -> tuple[date, date]:
    start = db.week_start_of(d)
    return start, start + timedelta(days=6)


def build_summary(chat_id: int, hh, start: date, end: date, title: str) -> str:
    rows = db.expenses_between(chat_id, start, end)
    sym = hh["currency"]
    total = sum(r["amount_cents"] for r in rows)
    budget = db.budget_for_week(chat_id, start)

    lines = [f"*{title}*", f"_{start:%d %b} – {end:%d %b}_", ""]

    if not rows:
        lines.append("Nothing logged yet.")
        if budget:
            lines.append(f"Budget: {fmt_money(budget, sym)}")
        return "\n".join(lines)

    lines.append(f"Total: *{fmt_money(total, sym)}*  ({len(rows)} entries)")

    if budget:
        remaining = budget - total
        pct = round(total / budget * 100)
        bar = "▮" * min(10, pct // 10) + "▯" * max(0, 10 - pct // 10)
        lines.append(f"Budget: {fmt_money(budget, sym)}  ({pct}%)")
        lines.append(f"`{bar}`")
        if remaining >= 0:
            lines.append(f"Left: *{fmt_money(remaining, sym)}*")
            today = db.local_today(hh["timezone"])
            if start <= today <= end:
                days_left = (end - today).days + 1
                lines.append(f"≈ {fmt_money(remaining // days_left, sym)}/day for {days_left} more days")
        else:
            lines.append(f"Over by *{fmt_money(-remaining, sym)}*")

    # per person
    per_person: dict[str, int] = defaultdict(int)
    for r in rows:
        per_person[r["display_name"]] += r["amount_cents"]
    if len(per_person) > 1:
        lines.append("\n*By person*")
        for name, amt in sorted(per_person.items(), key=lambda x: -x[1]):
            lines.append(f"• {name}: {fmt_money(amt, sym)}")

    # per category
    per_cat: dict[str, int] = defaultdict(int)
    for r in rows:
        per_cat[r["category"]] += r["amount_cents"]
    if len(per_cat) > 1:
        lines.append("\n*By category*")
        for cat, amt in sorted(per_cat.items(), key=lambda x: -x[1]):
            lines.append(f"• {cat}: {fmt_money(amt, sym)}")

    # per day
    per_day: dict[str, int] = defaultdict(int)
    for r in rows:
        per_day[r["local_date"]] += r["amount_cents"]
    lines.append("\n*By day*")
    for i in range(7):
        d = start + timedelta(days=i)
        amt = per_day.get(d.isoformat(), 0)
        if amt:
            lines.append(f"• {DAY_NAMES[i]}: {fmt_money(amt, sym)}")

    biggest = max(rows, key=lambda r: r["amount_cents"])
    lines.append(
        f"\nBiggest: {fmt_money(biggest['amount_cents'], sym)}"
        f" — {biggest['description'] or 'unlabelled'}"
    )

    # previous week comparison
    prev_start = start - timedelta(days=7)
    prev_total = db.total_between(chat_id, prev_start, prev_start + timedelta(days=6))
    if prev_total:
        delta = total - prev_total
        arrow = "▲" if delta > 0 else "▼"
        lines.append(f"vs last week: {arrow} {fmt_money(abs(delta), sym)}")

    return "\n".join(lines)


async def check_thresholds(update: Update, chat_id: int, hh) -> None:
    """Warn once per threshold per week."""
    today = db.local_today(hh["timezone"])
    start, end = week_bounds(today)
    budget = db.budget_for_week(chat_id, start)
    if not budget:
        return
    total = db.total_between(chat_id, start, end)
    pct = total / budget * 100
    sym = hh["currency"]

    for threshold in ALERT_THRESHOLDS:
        if pct >= threshold and not db.alert_already_sent(chat_id, start, threshold):
            db.record_alert(chat_id, start, threshold)
            days_left = (end - today).days + 1
            if threshold < 100:
                per_day = (budget - total) // max(days_left, 1)
                msg = (
                    f"⚠️ {fmt_money(total, sym)} of {fmt_money(budget, sym)} used "
                    f"with {days_left} day(s) left.\n"
                    f"About {fmt_money(per_day, sym)}/day to stay under."
                )
            else:
                over = total - budget
                msg = (
                    f"🔴 Budget hit with {days_left} day(s) to go.\n"
                    f"{fmt_money(total, sym)} spent, {fmt_money(over, sym)} over."
                )
            await update.effective_chat.send_message(msg)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    await update.effective_message.reply_text(
        "Food budget bot is live.\n\n"
        "Just type what you ate and what it cost, for example:\n"
        "  chicken rice 4.50\n"
        "  $12 pizza\n"
        "  groceries 43.20\n\n"
        "I'll react 👍 when I've logged it.\n\n"
        f"Timezone: {hh['timezone']}  ·  Currency: {hh['currency']}\n"
        "Set a weekly budget with /setbudget 200, then /week any time.\n"
        "Full list: /help"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "*Logging*\n"
        "Type naturally: `laksa 6.50`, `$14 sushi`, `23.40`\n"
        "/add 12.50 chicken rice — force a log if I missed it\n\n"
        "*Viewing*\n"
        "/week — this week's summary\n"
        "/today — today's entries\n"
        "/budget — quick status\n"
        "/last — your last 5 entries\n"
        "/savings — cumulative savings & streak\n\n"
        "*Editing*\n"
        "/undo — remove your most recent entry\n"
        "/delete <id> — remove a specific entry\n\n"
        "*Settings*\n"
        "/setbudget 200 — weekly budget\n"
        "/settz Asia/Singapore\n"
        "/setcurrency S$\n\n"
        "*Other*\n"
        "/export — all expenses as a CSV file\n\n"
        "I'll also nudge you at lunch and dinner with quick-pick buttons if "
        "nobody's logged that meal yet, and send a wrap-up every Sunday evening.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_setbudget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    if not ctx.args:
        budget = db.budget_for_week(chat_id, db.week_start_of(db.local_today(hh["timezone"])))
        current = fmt_money(budget, hh["currency"]) if budget else "not set"
        await update.effective_message.reply_text(
            f"Current weekly budget: {current}\nSet with: /setbudget 200"
        )
        return
    parsed = parse_explicit(ctx.args)
    if not parsed:
        await update.effective_message.reply_text("Try: /setbudget 200")
        return
    today = db.local_today(hh["timezone"])
    db.set_budget(chat_id, parsed.amount_cents, db.week_start_of(today))
    await update.effective_message.reply_text(
        f"Weekly budget set to {fmt_money(parsed.amount_cents, hh['currency'])}, "
        "starting this week."
    )


async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    today = db.local_today(hh["timezone"])
    start, end = week_bounds(today)
    total = db.total_between(chat_id, start, end)
    budget = db.budget_for_week(chat_id, start)
    sym = hh["currency"]
    if not budget:
        await update.effective_message.reply_text(
            f"Spent this week: {fmt_money(total, sym)}\nNo budget set — /setbudget 200"
        )
        return
    remaining = budget - total
    days_left = (end - today).days + 1
    if remaining >= 0:
        pace = fmt_money(remaining // max(days_left, 1), sym)
        await update.effective_message.reply_text(
            f"{fmt_money(total, sym)} / {fmt_money(budget, sym)}\n"
            f"{fmt_money(remaining, sym)} left · {days_left} day(s) · ≈{pace}/day"
        )
    else:
        await update.effective_message.reply_text(
            f"{fmt_money(total, sym)} / {fmt_money(budget, sym)}\n"
            f"Over by {fmt_money(-remaining, sym)}."
        )


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    today = db.local_today(hh["timezone"])
    offset = 0
    if ctx.args and ctx.args[0].lstrip("-").isdigit():
        offset = int(ctx.args[0])
    start, end = week_bounds(today + timedelta(days=7 * offset))
    title = "This week" if offset == 0 else f"Week of {start:%d %b}"
    await update.effective_message.reply_text(
        build_summary(chat_id, hh, start, end, title), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    today = db.local_today(hh["timezone"])
    rows = db.expenses_between(chat_id, today, today)
    sym = hh["currency"]
    if not rows:
        await update.effective_message.reply_text("Nothing logged today yet.")
        return
    total = sum(r["amount_cents"] for r in rows)
    lines = [f"*Today* — {fmt_money(total, sym)}", ""]
    for r in rows:
        lines.append(
            f"`#{r['id']}` {fmt_money(r['amount_cents'], sym)} · "
            f"{r['description'] or 'unlabelled'} · {r['display_name']}"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, user_id = household_context(update, ctx)
    today = db.local_today(hh["timezone"])
    rows = db.expenses_between(chat_id, today - timedelta(days=30), today)
    mine = [r for r in rows if r["user_id"] == user_id][-5:]
    if not mine:
        await update.effective_message.reply_text("You haven't logged anything recently.")
        return
    sym = hh["currency"]
    lines = ["*Your last entries*", ""]
    for r in reversed(mine):
        lines.append(
            f"`#{r['id']}` {r['local_date']} · {fmt_money(r['amount_cents'], sym)} · "
            f"{r['description'] or 'unlabelled'}"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, user_id = household_context(update, ctx)
    parsed = parse_explicit(ctx.args)
    if not parsed:
        await update.effective_message.reply_text("Try: /add 12.50 chicken rice")
        return
    db.add_expense(
        chat_id, user_id, parsed.amount_cents, parsed.description,
        category=parsed.category, source="command",
        message_id=update.effective_message.message_id,
    )
    await react_ok(update)
    await check_thresholds(update, chat_id, hh)


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, user_id = household_context(update, ctx)
    row = db.last_expense_by(chat_id, user_id)
    if not row:
        await update.effective_message.reply_text("Nothing of yours to undo.")
        return
    db.soft_delete(chat_id, row["id"])
    await update.effective_message.reply_text(
        f"Removed {fmt_money(row['amount_cents'], hh['currency'])} "
        f"({row['description'] or 'unlabelled'})."
    )


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.effective_message.reply_text("Try: /delete 42  (ids show in /today)")
        return
    expense_id = int(ctx.args[0].lstrip("#"))
    ok = db.soft_delete(chat_id, expense_id)
    await update.effective_message.reply_text(
        f"Deleted #{expense_id}." if ok else f"No live entry #{expense_id} here."
    )


async def cmd_settz(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    if not ctx.args:
        await update.effective_message.reply_text(
            f"Timezone is {hh['timezone']}.\nChange with: /settz Asia/Singapore"
        )
        return
    try:
        db.set_timezone(chat_id, ctx.args[0])
    except Exception:
        await update.effective_message.reply_text(
            "Unknown timezone. Use an IANA name like Asia/Singapore or Europe/London."
        )
        return
    if ctx.job_queue:
        schedule_household_jobs(ctx.job_queue, chat_id, ctx.args[0])
    await update.effective_message.reply_text(f"Timezone set to {ctx.args[0]}.")


async def cmd_savings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    summary = db.savings_summary(chat_id)
    if summary["weeks_recorded"] == 0:
        await update.effective_message.reply_text(
            "No completed weeks yet — this fills in after each week's Sunday wrap-up."
        )
        return
    sym = hh["currency"]
    saved = summary["total_saved_cents"]
    verb = "saved" if saved >= 0 else "overspent"
    lines = ["*Savings*", f"Cumulative {verb}: *{fmt_money(abs(saved), sym)}*"]
    if summary["streak"] > 0:
        lines.append(f"🔥 {summary['streak']} week streak under budget")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _, chat_id, _ = household_context(update, ctx)
    rows = db.all_expenses_ordered(chat_id)
    if not rows:
        await update.effective_message.reply_text("Nothing to export yet.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "local_date", "spent_at_utc", "person", "amount",
         "description", "category", "meal_slot", "is_estimate"]
    )
    for r in rows:
        writer.writerow([
            r["id"], r["local_date"], r["spent_at"], r["display_name"],
            f"{r['amount_cents'] / 100:.2f}", r["description"] or "",
            r["category"], r["meal_slot"] or "", "yes" if r["is_estimate"] else "",
        ])
    await update.effective_chat.send_document(
        document=InputFile(io.BytesIO(buf.getvalue().encode("utf-8")), filename="expenses.csv"),
    )


async def cmd_setcurrency(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hh, chat_id, _ = household_context(update, ctx)
    if not ctx.args:
        await update.effective_message.reply_text(
            f"Currency symbol is {hh['currency']}.\nChange with: /setcurrency S$"
        )
        return
    db.set_currency(chat_id, ctx.args[0][:4])
    await update.effective_message.reply_text(f"Currency set to {ctx.args[0][:4]}.")


# --------------------------------------------------------------------------
# reminders & scheduled jobs
# --------------------------------------------------------------------------

def meal_keyboard(slot: str, sym: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label.format(sym=sym), callback_data=f"meal:{slot}:{cents}")
        for cents, label in MEAL_BUCKETS
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("Skip", callback_data=f"meal:{slot}:skip")])
    return InlineKeyboardMarkup(rows)


async def remind_meal(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = ctx.job.chat_id
    slot = ctx.job.data["slot"]
    hh = db.ensure_household(chat_id)
    members = db.members_of(chat_id)
    if not members:
        return  # nobody has ever messaged, nobody to attribute a log to

    today = db.local_today(hh["timezone"])
    logged = db.slot_logged_user_ids(chat_id, today, slot)
    if all(m["user_id"] in logged for m in members):
        return  # everyone already logged this slot today

    await ctx.bot.send_message(
        chat_id=chat_id,
        text=f"🍽️ Log {slot}?",
        reply_markup=meal_keyboard(slot, hh["currency"]),
    )


async def on_meal_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, slot, choice = query.data.split(":")

    if choice == "skip":
        await query.edit_message_text(f"Skipped {slot}.")
        return

    chat_id = query.message.chat_id
    user = query.from_user
    name = user.first_name or user.username or str(user.id)
    db.ensure_member(chat_id, user.id, name)
    hh = db.ensure_household(chat_id)
    cents = int(choice)

    expense_id = db.add_expense(
        chat_id, user.id, cents, description=None, category="food",
        meal_slot=slot, is_estimate=True, source="reminder",
        message_id=query.message.message_id,
    )
    await query.edit_message_text(f"Logged {fmt_money(cents, hh['currency'])} for {slot} ✅")
    await check_thresholds(update, chat_id, hh)

    prompt = await ctx.bot.send_message(
        chat_id=chat_id,
        # selective=True only forces the reply keyboard for a user who is
        # either @-mentioned here or the sender of a message we're replying
        # to — neither was true before, so it silently forced nothing.
        text=f"{user.mention_html()} — what was it? Reply to add a description (optional).",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True),
    )
    PENDING_DESCRIPTION[prompt.message_id] = expense_id


async def weekly_summary_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = ctx.job.chat_id
    hh = db.ensure_household(chat_id)
    today = db.local_today(hh["timezone"])
    start, end = week_bounds(today)  # fires on Sunday: end of the week just closing

    await ctx.bot.send_message(
        chat_id=chat_id,
        text=build_summary(chat_id, hh, start, end, "Week wrap-up"),
        parse_mode=ParseMode.MARKDOWN,
    )

    budget = db.budget_for_week(chat_id, start)
    if budget:
        total = db.total_between(chat_id, start, end)
        db.record_week_close(chat_id, start, budget, total)


# --------------------------------------------------------------------------
# free-text logging
# --------------------------------------------------------------------------

async def on_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A reply to our 'what was it?' prompt fills in that expense's
    description. Any other reply is just a normal message that happens to be
    a reply (e.g. quoting a partner) — fall through to on_message for it."""
    msg = update.effective_message
    reply_to = msg.reply_to_message
    expense_id = PENDING_DESCRIPTION.pop(reply_to.message_id, None) if reply_to else None
    if expense_id is None:
        await on_message(update, ctx)
        return

    _, chat_id, _ = household_context(update, ctx)
    description = msg.text.strip()[:200]
    db.set_description(chat_id, expense_id, description, category=categorize(description))
    await react_ok(update)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return

    parsed = parse_message(msg.text)
    if parsed is None:
        log.debug("ignored: %r", msg.text)
        return

    hh, chat_id, user_id = household_context(update, ctx)
    expense_id = db.add_expense(
        chat_id, user_id, parsed.amount_cents, parsed.description,
        category=parsed.category, source="message", message_id=msg.message_id,
    )
    log.info(
        "logged #%s chat=%s user=%s %s %r",
        expense_id, chat_id, user_id, parsed.amount_cents, parsed.description,
    )
    await react_ok(update)
    await check_thresholds(update, chat_id, hh)


async def on_migrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Group upgraded to supergroup: carry the data across to the new chat_id."""
    msg = update.effective_message
    if msg and msg.migrate_to_chat_id:
        db.migrate_chat(msg.chat_id, msg.migrate_to_chat_id)
        log.warning("migrated chat %s -> %s", msg.chat_id, msg.migrate_to_chat_id)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=ctx.error)


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("week", "This week's summary"),
        BotCommand("today", "Today's entries"),
        BotCommand("budget", "Budget status"),
        BotCommand("savings", "Cumulative savings & streak"),
        BotCommand("add", "Log an expense explicitly"),
        BotCommand("undo", "Remove your last entry"),
        BotCommand("export", "Export all expenses as CSV"),
        BotCommand("setbudget", "Set the weekly budget"),
        BotCommand("help", "All commands"),
    ])

    if app.job_queue:
        households = db.all_households()
        for hh in households:
            schedule_household_jobs(app.job_queue, hh["chat_id"], hh["timezone"])
        log.info("re-registered jobs for %d household(s)", len(households))
    else:
        log.warning(
            "JobQueue unavailable — install 'python-telegram-bot[job-queue]'; "
            "reminders and the weekly summary will not fire"
        )


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN is not set")

    db.connect()

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setbudget", cmd_setbudget))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("setcurrency", cmd_setcurrency))
    app.add_handler(CommandHandler("savings", cmd_savings))
    app.add_handler(CommandHandler("export", cmd_export))

    app.add_handler(CallbackQueryHandler(on_meal_button, pattern=r"^meal:"))

    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    # Replies checked first: only the first matching handler in a group runs,
    # and on_reply falls through to on_message itself for non-pending replies.
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, on_reply))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.add_error_handler(on_error)

    log.info("polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
