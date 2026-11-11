"""Редакторська обробка чернетки моделі (D-034, D-035, D-036).

Оформлення виправляє код: лапки, апострофи, список з одного пункту, вступ до цитати,
службовий префікс. Цитату, якої немає в джерелі, код прибирає — новину не відхиляє.
Модель отримує зауваження лише щодо змісту (числа, довжина, атрибуція каналу) і лише
один раз; далі пункт приймається з попередженням. Жодна редакторська вимога не
зупиняє випуск.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from newsdigest.pipeline.candidates import Candidate
from newsdigest.pipeline.render import one_line

SHORT_SOURCE_CHARS = 320
"""Підказка моделі для компактного формату; не перевіряється кодом."""

_FOOTER = re.compile(
    r"^[^\w«\"“]*(?:підписуй(?:ся|тесь|теся)|подписывай(?:ся|тесь)|subscribe\b"
    r"|надіслати (?:новину|контент)|прислать (?:новость|контент))\b",
    re.IGNORECASE,
)
_QUOTE_INTRO = re.compile(
    r"^(?:автор(?:ка)?\s+(?:допису|публікації|поста)(?:\s+\w+)?|коментар\s+автора)\s*[:,—–-]?\s*",
    re.IGNORECASE,
)
# Маркер списку — лише з пробілом після нього: «-5 градусів» і «1.5 млн» — це числа.
_BULLET_MARK = re.compile(r"^(?:[▪•·]️?\s*|[-–—]\s+|\d{1,2}[.)]\s+)")
_DATE_TIME = re.compile(r"(?<!\d)\d{1,2}[.:/]\d{2}(?:[.:/]\d{2,4})?(?!\d)")
_FOLD = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "ʼ": "'",
        "`": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "–": "-",
        "—": "-",
        " ": " ",
        " ": " ",
    }
)
_OUTER_QUOTES = "«»\"“”„'’"
_ATTRIBUTION = (
    r"(?:за (?:даними|твердженням|повідомленням|інформацією)|як (?:пише|повідомляє)"
    r"|по данным|according to)\s*[«\"“]?"
)
_MULTIPLIERS = {
    "тис": 3,
    "тисяч": 3,
    "тыс": 3,
    "thousand": 3,
    "млн": 6,
    "мільйон": 6,
    "миллион": 6,
    "million": 6,
    "млрд": 9,
    "мільярд": 9,
    "миллиард": 9,
    "billion": 9,
}
_NUMBER = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)(?:\s*(тисяч|тис|тыс|thousand|мільйон|миллион|million|млн"
    r"|мільярд|миллиард|billion|млрд)\w*)?",
    re.IGNORECASE,
)
_DIGIT_GROUP = re.compile(r"(?<=\d)[\s  ](?=\d{3}(?!\d))")
_CLOCK_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")


def clean_source(candidate: Candidate) -> str:
    """Прибирає лише впізнавані підписи/заклики в кінці, не редакторські ремарки."""
    lines = candidate.text.strip().splitlines()
    labels = {one_line(r.label).casefold() for r in candidate.refs}
    while lines:
        line = lines[-1].strip()
        segments = [s.strip() for s in line.split("|")]
        branding = re.sub(r"^[^\w]+", "", line).casefold()
        footer = len(line) <= 180 and (
            bool(_FOOTER.match(line))
            or branding in labels
            or (len(segments) == 2 and bool(_FOOTER.match(segments[-1])))
        )
        if line and not footer:
            break
        lines.pop()
    return one_line("\n".join(lines))


def originals_for(
    ids: Sequence[str],
    by_id: Mapping[str, Candidate],
    originals: Mapping[str, tuple[Candidate, ...]],
) -> list[Candidate]:
    """Оригінальні дописи за candidate_id (фрагмент → увесь допис), без повторів."""
    found = [original for cid in ids for original in originals.get(cid, (by_id[cid],))]
    return list(dict.fromkeys(found))


def source_budget(candidates: Sequence[Candidate]) -> int:
    return max((len(clean_source(c)) for c in candidates), default=0)


def target_chars(budget: int) -> int:
    ratio = 0.85 if budget <= SHORT_SOURCE_CHARS else 0.65 if budget <= 1000 else 0.45
    return int(budget * ratio)


def body_text(data: Mapping[str, Any]) -> str:
    """Видимий редакторський текст пункту без службового префікса «Оновлення:»."""
    title = one_line(str(data.get("title", "")))
    summary = "\n\n".join(
        one_line(p) for p in str(data.get("summary", "")).split("\n\n") if p.strip()
    )
    sections = [title + ("\n" + summary if summary else "")]
    bullets = data.get("bullets") or []
    if bullets:
        sections.append("\n".join("▪️ " + one_line(b["text"]) for b in bullets))
    quote = one_line(str(data.get("quote", "")))
    if quote:
        sections.append(f"«{quote}»")
    return "\n\n".join(sections)


def fold(text: str) -> str:
    """Для порівняння цитат: типографські лапки/апострофи/тире й пробіли не важливі."""
    return " ".join(text.translate(_FOLD).casefold().split())


def quote_in(quote: str, candidate: Candidate) -> bool:
    return bool(quote) and fold(quote) in fold(candidate.text)


def _clean_quote(text: str) -> str:
    quote = _QUOTE_INTRO.sub("", one_line(text))
    while len(quote) > 1 and quote[0] in _OUTER_QUOTES and quote[-1] in _OUTER_QUOTES:
        quote = quote[1:-1].strip()
    return quote


def normalize_event(
    data: Mapping[str, Any],
    selected: Sequence[Candidate],
    originals: Mapping[str, tuple[Candidate, ...]],
) -> tuple[dict[str, Any], list[str]]:
    """Виправляє форму чернетки. Повертає нормалізовану подію і відмітки про виправлення."""
    ids = [c.candidate_id for c in selected]
    by_id = {c.candidate_id: c for c in selected}
    notes: list[str] = []
    title = one_line(str(data.get("title", "")))
    summary = "\n\n".join(
        one_line(p) for p in str(data.get("summary", "")).split("\n\n") if p.strip()
    )
    bullets: list[dict[str, Any]] = []
    for raw in data.get("bullets") or []:
        if not isinstance(raw, dict):
            continue
        text = _BULLET_MARK.sub("", one_line(str(raw.get("text", ""))))
        if not text:
            continue
        refs = [c for c in raw.get("candidate_ids") or [] if c in by_id] or ids
        bullets.append({"text": text, "candidate_ids": refs})
    if len(bullets) == 1:
        summary = "\n\n".join(filter(None, (summary, bullets[0]["text"])))
        bullets = []
    if not title and summary:
        title, summary = summary, ""
    layout = data.get("layout")
    if layout not in {"compact", "detailed"} or (layout == "compact" and (summary or bullets)):
        layout = "detailed"
    emoji = one_line(str(data.get("emoji", "")))
    if len(emoji) > 8:
        emoji = ""

    quote = _clean_quote(str(data.get("quote", "")))
    quote_id = str(data.get("quote_candidate_id", ""))
    if quote:
        preferred = [quote_id] if quote_id in by_id else []
        holder = next(
            (
                cid
                for cid in preferred + ids
                if any(quote_in(quote, c) for c in originals_for([cid], by_id, originals))
            ),
            None,
        )
        if holder is None:
            notes.append("quote_dropped")
            quote, quote_id = "", ""
        else:
            quote_id = holder
    else:
        quote_id = ""
    normalized = {
        **data,
        "candidate_ids": ids,
        "title": title,
        "summary": summary,
        "bullets": bullets,
        "layout": layout,
        "emoji": emoji,
        "quote": quote,
        "quote_candidate_id": quote_id,
        "is_update": bool(data.get("is_update", False)),
    }
    return normalized, notes


def length_allowance(
    data: Mapping[str, Any],
    selected: Sequence[Candidate],
    originals: Mapping[str, tuple[Candidate, ...]],
) -> int:
    """Довжина оригіналу: найдовший допис, для підподій — найдовший на кожну (без повторів)."""
    by_id = {c.candidate_id: c for c in selected}
    whole = source_budget(originals_for(list(by_id), by_id, originals))
    representatives: dict[str, int] = {}
    for bullet in data.get("bullets") or []:
        sources = originals_for(bullet["candidate_ids"], by_id, originals)
        best = max(sources, key=lambda c: len(clean_source(c)), default=None)
        if best is not None:
            representatives[best.candidate_id + best.text[:64]] = len(clean_source(best))
    return max(whole, sum(representatives.values()))


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def _canonical(value: Decimal) -> str:
    return format(value.normalize(), "f")


def number_values(text: str) -> list[str]:
    """Значення кожного числа з урахуванням множника: «1 млн» = «1 000 000» = 1000000.

    Множник — частина значення: «1 млн» і «1 млрд» або «1 грн» — різні числа.
    """
    found: list[str] = []
    for match in _NUMBER.finditer(_DIGIT_GROUP.sub("", text)):
        value = _decimal(match[1])
        if value is None:
            continue
        suffix = (match[2] or "").casefold()
        found.append(_canonical(value.scaleb(_MULTIPLIERS[suffix]) if suffix else value))
    return found


def source_numbers(texts: Sequence[str]) -> set[str]:
    """Значення чисел джерела; окремі складники — лише для дат і часу («11.11», «15:30»)."""
    values: set[str] = set()
    for text in texts:
        values.update(number_values(text))
        for match in _DATE_TIME.finditer(text):
            values.update(_canonical(Decimal(part)) for part in re.findall(r"\d+", match[0]))
    return values


def invented_numbers(
    text: str,
    evidence: Sequence[Candidate],
    timezone: ZoneInfo | None,
) -> list[str]:
    """Числа чернетки, яких немає в джерелах у жодному рівнозначному записі."""
    if timezone is not None:
        supplied = {f"{c.earliest.astimezone(timezone):%H:%M}" for c in evidence}
        text = _CLOCK_TIME.sub(
            lambda m: " " if f"{int(m[1]):02d}:{m[2]}" in supplied else m[0], text
        )
    known = source_numbers([c.text for c in evidence])
    return sorted({value for value in number_values(text) if value not in known})


def content_issues(
    data: Mapping[str, Any],
    selected: Sequence[Candidate],
    originals: Mapping[str, tuple[Candidate, ...]],
    timezone: ZoneInfo | None,
) -> list[tuple[str, str]]:
    """Зауваження щодо змісту: (код, пояснення для моделі). Не блокують публікацію."""
    by_id = {c.candidate_id: c for c in selected}
    evidence = originals_for(list(by_id), by_id, originals)
    body = body_text(data)
    issues: list[tuple[str, str]] = []
    missing = invented_numbers(body, evidence, timezone)
    if missing:
        issues.append(
            (
                "numbers",
                f"числа {missing} відсутні в текстах джерел — напиши їх як у джерелі або прибери",
            )
        )
    allowance = length_allowance(data, selected, originals)
    if allowance and len(body) > allowance:
        issues.append(
            (
                "length",
                f"пункт має {len(body)} символів, оригінал {allowance}: скороти повтори й "
                "зайві слова, збережи факти",
            )
        )
    text = body.casefold()
    named = sorted(
        {
            r.label
            for c in evidence
            for r in c.refs
            if len(r.label) >= 4
            and re.search(_ATTRIBUTION + re.escape(r.label.casefold()) + r"(?!\w)", text)
        }
    )
    if named:
        issues.append(
            (
                "attribution",
                f"не згадуй канали-джерела ({', '.join(named)}) — посилання на джерело "
                "додається автоматично",
            )
        )
    return issues
