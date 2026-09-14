"""Turn a chat message into an expense, or decide it isn't one.

Design rule: when in doubt, stay silent. A missed expense costs one /add.
A false positive pollutes the data and makes the group annoying to talk in.
"""

import re
from dataclasses import dataclass

MAX_LEN = 60          # longer messages are conversation, not logging
MAX_AMOUNT = 100_000  # cents guard: reject anything over 1000 units

# Matches an optional currency prefix then a number.
AMOUNT_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<sym>S\$|RM|HK\$|NT\$|[$€£¥₱₹₩฿])?\s*"
    r"(?P<num>\d{1,6}(?:[.,]\d{1,2})?)"
    r"(?P<suffix>k)?"
    r"(?![\w.])",
    re.IGNORECASE,
)

# If any of these match, the number is almost certainly not money.
NOT_MONEY = [
    # Colon only. Using [:.] here would eat every decimal amount (4.50).
    re.compile(r"\b\d{1,2}\s*:\s*\d{2}\b"),                       # 7:30
    re.compile(r"\b\d{1,2}\s*(am|pm)\b", re.I),                   # 7pm
    re.compile(r"\b(at|by|before|after|around|till|until)\s+\d", re.I),
    re.compile(r"\b\d+\s*(min|mins|minute|minutes|hour|hours|hr|hrs|day|days|"
               r"week|weeks|month|months|year|years|km|kg|g|ml|l|pax|"
               r"people|person|deg|degrees)\b", re.I),
    re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", re.I),
    re.compile(r"https?://"),
    re.compile(r"\?"),                                            # questions
    re.compile(r"^\s*[+-]?\d+\s*[+\-*/]\s*\d"),                   # arithmetic
]

CATEGORY_KEYWORDS = {
    "groceries": ["ntuc", "fairprice", "giant", "sheng siong", "cold storage",
                  "grocer", "grocery", "groceries", "market", "supermarket", "mart"],
    "delivery":  ["grab", "grabfood", "foodpanda", "deliveroo", "delivery",
                  "uber eats", "ubereats", "doordash"],
    "coffee":    ["coffee", "kopi", "latte", "starbucks", "cafe", "café",
                  "espresso", "tea", "teh", "bubble tea", "boba"],
    "dining":    ["dinner", "lunch", "brunch", "restaurant", "hotpot", "buffet"],
}


@dataclass
class ParsedExpense:
    amount_cents: int
    description: str | None
    category: str


def categorize(text: str) -> str:
    low = text.lower()
    for category, words in CATEGORY_KEYWORDS.items():
        if any(w in low for w in words):
            return category
    return "food"


def parse_amount(token_num: str, suffix: str | None) -> int:
    """'4.50' -> 450. '4,50' -> 450. '4' -> 400. '2k' -> 200000."""
    normalized = token_num.replace(",", ".")
    cents = round(float(normalized) * 100)
    if suffix and suffix.lower() == "k":
        cents *= 1000
    return cents


def parse_message(text: str) -> ParsedExpense | None:
    """Return a ParsedExpense, or None if this message should be ignored."""
    text = (text or "").strip()

    if not text or text.startswith("/"):
        return None
    if len(text) > MAX_LEN:
        return None
    if any(rx.search(text) for rx in NOT_MONEY):
        return None

    matches = list(AMOUNT_RE.finditer(text))
    # Exactly one number, or we can't tell which one is the price.
    if len(matches) != 1:
        return None

    m = matches[0]
    cents = parse_amount(m.group("num"), m.group("suffix"))
    if cents <= 0 or cents > MAX_AMOUNT:
        return None

    description = (text[: m.start()] + " " + text[m.end():]).strip()
    description = re.sub(r"\s+", " ", description).strip(" -–—:,.")
    description = description or None

    # A bare number with no words is fine ("12"), but a lone number
    # embedded in a sentence of >6 words is probably chatter.
    if description and len(description.split()) > 6:
        return None

    return ParsedExpense(
        amount_cents=cents,
        description=description,
        category=categorize(description or ""),
    )


def parse_explicit(args: list[str]) -> ParsedExpense | None:
    """For /add 12.50 chicken rice -- looser, because the user asked explicitly."""
    if not args:
        return None
    m = AMOUNT_RE.fullmatch(args[0].strip())
    if not m:
        return None
    cents = parse_amount(m.group("num"), m.group("suffix"))
    if cents <= 0 or cents > MAX_AMOUNT:
        return None
    description = " ".join(args[1:]).strip() or None
    return ParsedExpense(cents, description, categorize(description or ""))


def fmt_money(cents: int, symbol: str = "S$") -> str:
    return f"{symbol}{cents / 100:,.2f}"
