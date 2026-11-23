"""Deterministic guards applied to the planner's choice (the LLM decides, code enforces).

Period guard: a metric that needs a period must not run on an assumed one. If the
question has no time expression and there is no previous query to inherit a period from,
the agent asks for the period instead. The word lists are deliberately generous: a false
"has time" only means the planner's choice stands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

_EN = (
    r"\b(today|tonight|yesterday|now|currently|current|right now|so far|to date|recent|"
    r"recently|latest|day|days|daily|week|weeks|weekly|weekend|month|months|monthly|"
    r"quarter|quarters|quarterly|q[1-4]|year|years|yearly|annual|ytd|mtd|qtd|since|"
    r"between|during|from|until|last|this|past|previous|next|trend|over time|"
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|"
    r"oct(ober)?|nov(ember)?|dec(ember)?)\b"
)
_AR = (
    r"(اليوم|أمس|امس|الآن|الان|حالياً|حاليا|الحالي|الحالية|الأسبوع|الاسبوع|أسبوع|"
    r"اسبوع|الشهر|شهر|شهرياً|شهريا|الربع|ربع|السنة|سنة|العام|عام|سنوي|منذ|خلال|بين|"
    r"الماضي|الماضية|الأخير|الأخيرة|هذا|هذه|يومياً|يوميا|أسبوعياً|حتى الآن|"
    r"يناير|فبراير|مارس|أبريل|ابريل|مايو|يونيو|يوليو|أغسطس|اغسطس|سبتمبر|أكتوبر|"
    r"اكتوبر|نوفمبر|ديسمبر)"
)
_TIME = re.compile(f"{_EN}|{_AR}|\\d", re.IGNORECASE)
_ARABIC = re.compile(r"[؀-ۿ]")


def mentions_time(question: str) -> bool:
    return bool(_TIME.search(question))


def period_clarification(question: str) -> tuple[str, list[str]]:
    if _ARABIC.search(question):
        return "لأي فترة؟", ["هذا الشهر", "الشهر الماضي", "هذا العام"]
    return "For which period?", ["This month", "Last month", "This year"]


# ---- answering a clarification ---------------------------------------------------------
# A reply to "For which period?" ("This month") or "Which metric?" ("Accounts opened") is
# not a question on its own. Combine it with the question that was being clarified, so the
# planner sees "accounts opened by branch — This month". A reply that is an offered option
# or a few words counts as an answer; anything longer is treated as a new question.
# For "did you mean" (an unknown name), the chosen value replaces the name in the question.
_MAX_ANSWER_WORDS = 4


@dataclass(frozen=True)
class PendingClarification:
    question: str  # the question being clarified
    options: tuple[str, ...]  # replies that count as an answer
    replace: str | None = None  # the unknown name to swap for the reply


def answer_clarification(pending: PendingClarification, reply: str) -> str | None:
    r = reply.strip()
    if not r:
        return None
    chosen = next((o for o in pending.options if o.strip().casefold() == r.casefold()), None)
    if pending.replace and chosen is not None:
        swapped = re.sub(re.escape(pending.replace), chosen, pending.question, count=1, flags=re.I)
        if swapped != pending.question:
            return swapped
    if chosen is None and len(r.split()) > _MAX_ANSWER_WORDS:
        return None
    return f"{pending.question} — {r}"


# ---- unknown names ---------------------------------------------------------------------
_DIM_AR = {"branch": "فرع", "channel": "قناة", "segment": "شريحة"}


def unknown_value_clarification(
    dimension: str, value: str, allowed: list[str], arabic: bool
) -> tuple[str, list[str]]:
    """'There's no branch called Hawaii. Did you mean Hawalli?' with close matches as options;
    with no close match, list what exists (up to 4 as options, the rest in the text)."""
    by_fold = {a.casefold(): a for a in allowed}
    close = [by_fold[m] for m in get_close_matches(value.casefold(), by_fold, n=3, cutoff=0.6)]
    if arabic:
        head = f"لا يوجد {_DIM_AR.get(dimension, dimension)} باسم '{value}'."
        if close:
            return f"{head} هل تقصد:", close
        return f"{head} المتاح: {'، '.join(allowed)}.", allowed[:4]
    head = f"There's no {dimension} called '{value}'."
    if close:
        return f"{head} Did you mean:", close
    return f"{head} Available: {', '.join(allowed)}.", allowed[:4]


_WORD = re.compile(r"[\w']+")


def near_miss(question: str, names: list[str]) -> tuple[str, list[str]] | None:
    """A word (or two-word phrase) in the question that looks like a misspelt name, e.g.
    'Hawaii' for 'Hawalli'. Used only after the planner declined or failed, so a false hit
    costs one extra question, never a wrong answer."""
    by_fold = {n.casefold(): n for n in names}
    words = _WORD.findall(question)
    grams = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
    grams += [" ".join(t) for t in zip(words, words[1:], words[2:], strict=False)]
    if any(g.casefold() in by_fold for g in grams):
        return None  # a real name is already there
    for g in grams:
        if len(g) < 4:
            continue
        hits = get_close_matches(g.casefold(), by_fold, n=3, cutoff=0.75)
        if hits:
            return g, [by_fold[h] for h in hits]
    return None


# ---- extras the question did not ask for -------------------------------------------------
# The planner tends to add a time breakdown or a "top N" nobody asked for (or copy them from
# the previous query when the user now asks for a total). Code removes them:
#  - "total / overall / in total" -> no time breakdown, even in a follow-up;
#  - a new question gets a time breakdown only if it asks for one (trend words);
#  - order_by / limit only when the question asks for a ranking.
_TREND = re.compile(
    r"\b(trend|trends|trending|over time|by (day|week|month|quarter|year)|"
    r"per (day|week|month|quarter|year)|each (day|week|month|quarter|year)|"
    r"daily|weekly|monthly|quarterly|yearly|annually|month by month|month on month|"
    r"week by week|day by day|mom|yoy)\b"
    r"|(اتجاه|على مدى|شهري|شهرياً|شهريا|حسب الشهر|لكل شهر|يومي|يومياً|يوميا|حسب اليوم|"
    r"أسبوعي|أسبوعياً|حسب الأسبوع|سنوي|سنوياً)",
    re.IGNORECASE,
)
_TOTAL = re.compile(
    r"\b(total|totals|overall|altogether|in total|combined|sum|grand total)\b"
    r"|(إجمالي|الإجمالي|اجمالي|الاجمالي|المجموع|مجموع|الكلي|الكلية)",
    re.IGNORECASE,
)
_RANK = re.compile(
    r"\b(top|bottom|most|least|highest|lowest|largest|smallest|biggest|best|worst|"
    r"rank|ranking|ranked|leading|max|maximum|min|minimum)\b"
    r"|(أعلى|الأعلى|أكثر|الأكثر|أقل|الأقل|أدنى|الأدنى|أكبر|الأكبر|أصغر|الأصغر|أفضل|أسوأ|ترتيب)",
    re.IGNORECASE,
)


def wants_trend(question: str) -> bool:
    return bool(_TREND.search(question))


def wants_total(question: str) -> bool:
    return bool(_TOTAL.search(question))


def wants_ranking(question: str) -> bool:
    return bool(_RANK.search(question))


def trim_extras(
    query: dict[str, Any], question: str, follow_up: bool
) -> tuple[dict[str, Any], list[str]]:
    """Return the query without unrequested grain / ranking, plus what was removed."""
    q = dict(query)
    removed: list[str] = []
    trend, total = wants_trend(question), wants_total(question)
    if q.get("grain") and not trend and (total or not follow_up):
        q.pop("grain")
        removed.append("grain")
    # a follow-up may keep a ranking that was asked for earlier
    if (q.get("order_by") or q.get("limit")) and not wants_ranking(question) and not follow_up:
        q.pop("order_by", None)
        q.pop("limit", None)
        removed.append("ranking")
    return q, removed


# "Now only for Salmiya" / "and now by month": a leading "now" is a connector, not a period.
_LEADING_NOW = re.compile(
    r"^\s*((ok(ay)?|so)[,\s]+)?(and\s+)?now\b[,\s]*|^\s*(و?الآن|و?الان)\s*", re.I
)


def names_period(question: str) -> bool:
    """Stricter than mentions_time, for follow-ups: does the user name a (new) period?"""
    return mentions_time(_LEADING_NOW.sub("", question, count=1))
