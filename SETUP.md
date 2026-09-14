# Food budget bot — setup

Phase 1 & 2: free-text expense logging, weekly budget tracking, on-demand
summaries, 80%/100% budget alerts, lunch/dinner reminders with quick-pick
buttons, a Sunday-evening weekly summary, a savings streak, and CSV export.

Total time: about 30 minutes, 20 of which is waiting for GCP.

---

## 1. Create the bot (5 min)

In Telegram, message **@BotFather**:

1. `/newbot` → give it a name and a username ending in `bot`.
   Copy the token it gives you. Treat it like a password — anyone with it
   can read and post as your bot.
2. `/setprivacy` → pick your bot → **Disable**.

**Step 2 is not optional.** By default a bot in a group only sees messages that
are commands or direct replies to it. With privacy disabled it sees all text,
which is what makes `chicken rice 4.50` work.

3. Create your group, add the bot, then **remove it and add it again**.
   Privacy mode is cached per-membership; without the re-add it keeps ignoring you.

Optional but nice: `/setcommands` and paste

```
week - This week's summary
today - Today's entries
budget - Budget status
add - Log an expense explicitly
undo - Remove your last entry
setbudget - Set the weekly budget
help - All commands
```

---

## 2. Try it locally first (5 min)

Before touching a server, confirm it works on your laptop.

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

export TELEGRAM_TOKEN="paste-your-token"
export DEFAULT_TZ="Asia/Singapore"
export DEFAULT_CURRENCY="S$"
python bot.py
```

In the group: `/start`, then `/setbudget 200`, then type `chicken rice 4.50`.
You should get a 👍 reaction. Then `/week`.

If there's no reaction but `/week` shows the entry, the bot lacks reaction
permission in the group — harmless, but check its admin rights if you want it.

Ctrl-C to stop. Your test data is in `foodbot.db`; delete it before going live
if you want a clean slate.

---

## 3. Create the free GCP VM (15 min)

The always-free tier covers **one e2-micro**, and only in `us-west1`,
`us-central1`, or `us-east1`. Anywhere else and you get billed.

1. Sign up at console.cloud.google.com (card required for identity; the free
   tier instance itself doesn't charge, including its external IP).
2. **Set a budget alert first**: Billing → Budgets & alerts → new budget, $1,
   alert at 100%. This is your safety net against a misconfiguration.
3. Create the VM:

```bash
gcloud compute instances create foodbot \
  --machine-type=e2-micro \
  --zone=us-central1-a \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard
```

30GB standard persistent disk is the free-tier maximum. Don't exceed it.

You need **no firewall rules**. The bot uses long polling — it dials out to
Telegram, nothing dials in. Leave every port closed.

```bash
gcloud compute ssh foodbot --zone=us-central1-a
```

---

## 4. Deploy (10 min)

On the VM:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip sqlite3

# dedicated unprivileged user
sudo useradd --system --home /opt/foodbot --shell /usr/sbin/nologin foodbot
sudo mkdir -p /opt/foodbot
sudo chown foodbot:foodbot /opt/foodbot
```

Copy the files up from your laptop:

```bash
gcloud compute scp bot.py db.py parsing.py requirements.txt backup.sh \
  foodbot:/tmp/ --zone=us-central1-a
```

Back on the VM:

```bash
sudo mv /tmp/{bot.py,db.py,parsing.py,requirements.txt,backup.sh} /opt/foodbot/
cd /opt/foodbot
sudo -u foodbot python3 -m venv venv
sudo -u foodbot ./venv/bin/pip install -r requirements.txt
```

Create `/opt/foodbot/.env` (copy `.env.example` and fill in your token):

```bash
sudo -u foodbot tee /opt/foodbot/.env >/dev/null <<'EOF'
TELEGRAM_TOKEN=paste-your-token-here
DB_PATH=/opt/foodbot/foodbot.db
DEFAULT_TZ=Asia/Singapore
DEFAULT_CURRENCY=S$
EOF
sudo chmod 600 /opt/foodbot/.env
sudo chown foodbot:foodbot /opt/foodbot/.env
```

Install the service:

```bash
sudo cp foodbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foodbot
sudo systemctl status foodbot
sudo journalctl -u foodbot -f      # watch it live
```

Send a message in the group. You should see a `logged #1 …` line in the journal.

---

## 5. Backups

Losing three months of expense history is annoying and trivially preventable.

```bash
sudo cp backup.sh /opt/foodbot/ && sudo chmod +x /opt/foodbot/backup.sh
sudo crontab -u foodbot -e
```

Add:

```
15 3 * * * /opt/foodbot/backup.sh
```

`backup.sh` uses `sqlite3 .backup`, which is safe against a live WAL database.
A plain `cp` of a WAL database can produce a corrupt copy — don't do that.

Uncomment the `gcloud storage cp` line and point it at a bucket if you want the
backups off the VM. 5GB of Cloud Storage is also in the always-free tier.

---

## Daily use

**Logging** — just type it. Any of these work:

```
chicken rice 4.50
$12 pizza
grab 18.90
ntuc 62.15
23.40
```

The bot reacts 👍 and says nothing, so the group stays usable for actual
conversation.

**When it stays silent**, it decided the message wasn't an expense. It ignores
anything with a `?`, a time (`7pm`, `7:30`), a unit (`20 mins`, `5km`), a month
name, a URL, more than one number, or more than ~6 words of description. Use
`/add 12.50 chicken rice` to force it.

That bias is deliberate: a missed expense costs you one `/add`, while a false
positive quietly corrupts your numbers and makes the group irritating.

**Commands**: `/week`, `/week -1` (last week), `/today`, `/budget`, `/last`,
`/savings`, `/undo`, `/delete 42`, `/export`, `/setbudget 200`, `/settz`,
`/setcurrency`, `/help`.

**Reminders**: around 12:00 and 19:00 local time, the bot asks "Log lunch?" /
"Log dinner?" with quick-pick amount buttons, unless everyone in the group has
already logged that meal today. Tap a bucket, then optionally reply to the
follow-up prompt with what it was for.

---

## Design notes worth knowing

**Money is integer cents everywhere.** Floats accumulate rounding error; over a
year of daily entries that becomes visible. `fmt_money` is the only place
division happens.

**Budgets are a history table, not a field.** When you change the budget, past
weeks keep the number that was in force at the time, so old summaries stay
truthful.

**Deletes are soft.** `/undo` sets `deleted_at`; nothing is ever removed. If you
delete the wrong thing, it's recoverable with one SQL statement.

**Each expense stores both a UTC timestamp and a local date.** The local date is
what week queries use, which keeps them a plain `BETWEEN` instead of timezone
arithmetic on every read.

**Supergroup migration is handled.** If Telegram upgrades your group, `chat_id`
changes and the bot moves all rows across. Without that handler you'd silently
lose every past entry.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Bot ignores plain messages, responds to commands | Privacy mode still on. `/setprivacy` → Disable, then remove and re-add the bot |
| No 👍 but entries do save | Bot lacks reaction permission in the group. Cosmetic |
| Wrong day on entries | `/settz Asia/Singapore` |
| `Conflict: terminated by other getUpdates` | Two copies running. `sudo systemctl stop foodbot`, kill your local one |
| Service won't start | `sudo journalctl -u foodbot -n 50`. Usually a missing `.env` or bad token |

---

## What's next (Phase 3)

- `/export` filters (date range, category)
- Multi-currency / FX if the household splits time across countries
