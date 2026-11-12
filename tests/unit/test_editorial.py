"""D-036: оформлення виправляє код, зміст — одна спроба моделі, випуск не зупиняється."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from newsdigest.ai.editorial import body_text, clean_source, invented_numbers, normalize_event
from newsdigest.ai.grouping import build_payload, group_with_ai, review
from newsdigest.ai.prompts import DEDUPE_INSTRUCTIONS, response_schema
from newsdigest.ai.provider import OpenAIProvider
from newsdigest.pipeline.candidates import Candidate
from newsdigest.pipeline.events import Event
from newsdigest.pipeline.plan import PublicationPlan, build_plan
from newsdigest.pipeline.render import Header, render_series
from tests.unit.test_ai import CONTEXT, SETTINGS, T0, FakeCreate, ref


def candidate(cid: str, text: str, position: int = 0) -> Candidate:
    return Candidate(cid, text, (ref(f"source_{cid}", position, position),))


def event(ids: list[str], title: str, **extra: Any) -> dict[str, Any]:
    return {
        "candidate_ids": ids,
        "title": title,
        "summary": "",
        "emoji": "📰",
        "is_update": False,
        "layout": "compact",
        "bullets": [],
        "quote": "",
        "quote_candidate_id": "",
        **extra,
    }


def answer(*events: dict[str, Any]) -> dict[str, Any]:
    return {"events": list(events), "excluded": []}


def eid(*ids: str) -> str:
    return hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest()[:16]


@pytest.mark.parametrize(
    "footer",
    ["Підписуйся на канал", "🇺🇦 Новини | Прислать контент", "Source_A", "Subscribe to us"],
)
def test_footer_does_not_inflate_source_budget(footer: str) -> None:
    c = candidate("a", f"У селищі відкрили бібліотеку.\n\n{footer}\n")
    assert clean_source(c) == "У селищі відкрили бібліотеку."
    assert build_payload([c], CONTEXT)["candidates"][0]["source_chars"] == 29
    assert build_payload([c], CONTEXT)["candidates"][0]["text"] == c.text  # raw stays intact


def test_source_editorial_question_is_not_a_footer() -> None:
    c = candidate("a", "Відкрили бібліотеку.\nА хто поверне книжки?\n\nПідписуйтесь!")
    assert clean_source(c).endswith("А хто поверне книжки?")


# --- Оформлення виправляє код ---------------------------------------------------------


def test_single_bullet_becomes_summary() -> None:
    c = candidate("a", "У селищі відкрили бібліотеку для школярів та дорослих мешканців.")
    data = event(
        ["a"],
        "Бібліотека",
        layout="compact",
        bullets=[{"text": "▪️ Відкрили для школярів.", "candidate_ids": ["a"]}],
    )
    fixed, _ = normalize_event(data, [c], {})
    assert fixed["bullets"] == [] and fixed["summary"] == "Відкрили для школярів."
    assert fixed["layout"] == "detailed"
    assert review(answer(data), [c]).covered == {"a"}


@pytest.mark.parametrize(
    "quote",
    [
        "«А хто поверне книжки?»",
        "Автор допису іронізує: а хто поверне книжки?",
        "А хто поверне книжки?",
    ],
)
def test_quote_form_is_fixed_in_code(quote: str) -> None:
    c = candidate("a", "У селищі відкрили бібліотеку для мешканців. А хто поверне книжки?")
    data = event(["a"], "Відкрили бібліотеку.", quote=quote, quote_candidate_id="a")
    checked = review(answer(data), [c])
    assert checked.covered == {"a"}
    assert checked.good_events[0]["quote"].casefold() == "а хто поверне книжки?"


def test_typographic_apostrophe_in_quote_is_not_a_misquote() -> None:
    c = candidate("a", "Мер сказав: «Ми п'ємо воду з нового водогону». Ремонт тривав місяць.")
    data = event(["a"], "Відремонтували водогін.", quote="Ми п’ємо воду з нового водогону")
    fixed, notes = normalize_event(data, [c], {})
    assert fixed["quote"] and fixed["quote_candidate_id"] == "a" and not notes


def test_invented_quote_is_dropped_but_news_kept() -> None:
    c = candidate("a", "У селищі відкрили бібліотеку для мешканців.")
    data = event(
        ["a"], "Відкрили бібліотеку.", quote="Хіба це добра новина?", quote_candidate_id="a"
    )
    checked = review(answer(data), [c])
    assert checked.covered == {"a"} and checked.good_events[0]["quote"] == ""


def test_quote_assigned_to_candidate_that_contains_it() -> None:
    a = candidate("a", "У селищі відкрили бібліотеку для мешканців.")
    b = candidate("b", "Бібліотеку відкрили. А хто поверне книжки?", 1)
    data = event(["a", "b"], "Відкрили бібліотеку.", quote="А хто поверне книжки?")
    data["quote_candidate_id"] = "a"
    fixed, _ = normalize_event(data, [a, b], {})
    assert fixed["quote_candidate_id"] == "b"


def test_short_update_is_not_rejected_for_service_prefix() -> None:
    c = candidate("a", "Відбій.")
    checked = review(answer(event(["a"], "Відбій.", is_update=True)), [c])
    assert checked.covered == {"a"} and not checked.warnings


@pytest.mark.parametrize(
    ("source", "draft"),
    [
        ("Збитки — 1 млн грн.", "Збитки — 1 000 000 грн."),
        ("Збитки — 1 000 000 грн.", "Збитки — 1 млн грн."),
        ("Збитки — 1,5 млн грн.", "Збитки — 1500000 грн."),
        ("Ремонт коштуватиме 2 тис. грн.", "Ремонт коштуватиме 2000 грн."),
        ("Засідання 11.11 о 15:30, 1 000 учасників.", "Засідання 11.11 о 15:30: 1000 учасників."),
    ],
)
def test_equivalent_numbers_are_not_invented(source: str, draft: str) -> None:
    assert invented_numbers(draft, [candidate("a", source)], None) == []


def test_really_new_number_is_reported() -> None:
    assert invented_numbers("Збитки — 7 млн грн.", [candidate("a", "Збитки — 1 млн.")], None)


# --- Зміст: одна спроба моделі, далі — попередження ----------------------------------


async def test_content_issue_gets_one_retry_then_accepted_with_warning() -> None:
    c = candidate("a", "У селищі відкрили бібліотеку.")
    long_draft = event(["a"], "У селищі відкрили нову бібліотеку для всіх мешканців.")
    create = FakeCreate(answer(long_draft), answer(long_draft))
    result = await group_with_ai([c], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 1 and len(create.calls) == 2
    correction = create.payload(1)["corrections"][0]
    assert any("оригінал" in p for p in correction["problems"])
    assert result.excluded == {"ai_warning_length": 1}


async def test_retry_cannot_exclude_news_instead_of_fixing() -> None:
    c = candidate("a", "Відкрили 2 бібліотеки.")
    excluded = {"events": [], "excluded": [{"candidate_id": "a", "reason": "not_news"}]}
    create = FakeCreate(
        answer(event(["a"], "Відкрили 5 бібліотек.")),
        excluded,
        answer(event(["a"], "Відкрили 2 бібліотеки.")),
    )
    result = await group_with_ai([c], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 1 and result.excluded == {}
    assert "не можна виключати" in " ".join(create.payload(2)["previous_answer_problems"])


# --- Невраховані дописи не блокують випуск ---------------------------------------------


async def test_unprocessed_candidate_does_not_block_other_news() -> None:
    a = candidate("a", "Відкрили бібліотеку.")
    b = candidate("b", "Відновили рух трамвая.", 1)
    nothing: dict[str, Any] = {"events": [], "excluded": []}
    create = FakeCreate(answer(event(["a"], "Відкрили бібліотеку.")), nothing, nothing, nothing)
    create.answers.append(nothing)
    result = await group_with_ai([a, b], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert [e.title for e in result.events] == ["Відкрили бібліотеку."]
    assert result.unprocessed == ["b"] and result.excluded["ai_unprocessed"] == 1
    assert {s["reason"] for s in result.excluded_sources} == {"ai_unprocessed"}
    rescue = [create.payload(i)["candidates"] for i in (3, 4)]
    assert all([c["id"] for c in cs] == ["b"] for cs in rescue)


async def test_rescue_request_recovers_stubborn_candidate() -> None:
    a = candidate("a", "Відкрили бібліотеку.")
    b = candidate("b", "Відновили рух трамвая.", 1)
    nothing: dict[str, Any] = {"events": [], "excluded": []}
    create = FakeCreate(
        answer(event(["a"], "Відкрили бібліотеку.")),
        nothing,
        nothing,
        answer(event(["b"], "Відновили рух трамвая.")),
        {"duplicates": []},
    )
    result = await group_with_ai([a, b], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 2 and result.unprocessed == []


# --- Дублікати: групи ID, переписування лише груп ----------------------------------


async def test_duplicate_after_retry_is_merged_from_originals() -> None:
    a = candidate("a", "У селищі відкрили 2 бібліотеки.")
    b = candidate("b", "Дві бібліотеки, 2 нові, відкрили у селищі.", 1)
    create = FakeCreate(
        answer(event(["a"], "Відкрили 2 бібліотеки."), event(["b"], "Відкрили 9 бібліотек.")),
        answer(event(["b"], "Відкрили 2 бібліотеки.")),
        {"duplicates": [{"draft_ids": ["d1", "d2"]}]},
        answer(event(["a", "b"], "У селищі відкрили 2 бібліотеки.")),
    )
    result = await group_with_ai([a, b], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 1 and len(create.calls) == 4
    assert {r.source_id for r in result.events[0].participants} == {"source_a", "source_b"}
    assert create.calls[2]["instructions"] == DEDUPE_INSTRUCTIONS
    merge_payload = create.payload(3)
    assert merge_payload["same_event_hint"] is True
    assert [c["text"] for c in merge_payload["candidates"]] == [a.text, b.text]
    schema = create.calls[3]["text"]["format"]["schema"]
    assert schema["properties"]["excluded"]["maxItems"] == 0


async def test_dedupe_failure_keeps_checked_drafts() -> None:
    a = candidate("a", "Відкрили 2 бібліотеки.")
    b = candidate("b", "Відновили рух трамвая.", 1)
    create = FakeCreate(
        answer(event(["a"], "Відкрили 2 бібліотеки."), event(["b"], "Відновили 3 трамваї.")),
        answer(event(["b"], "Відновили рух трамвая.")),
        RuntimeError("timeout"),
    )
    result = await group_with_ai([a, b], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 2 and result.excluded["ai_dedupe_failed"] == 1


async def test_failed_merge_rewrite_keeps_both_drafts() -> None:
    a = candidate("a", "Відкрили 2 бібліотеки.")
    b = candidate("b", "Відкрили дві бібліотеки, 2 нові.", 1)
    nothing: dict[str, Any] = {"events": [], "excluded": []}
    create = FakeCreate(
        answer(event(["a"], "Відкрили 2 бібліотеки."), event(["b"], "Відкрили 4 бібліотеки.")),
        answer(event(["b"], "Відкрили 2 бібліотеки.")),
        {"duplicates": [{"draft_ids": ["d1", "d2"]}]},
        nothing,
        nothing,
    )
    result = await group_with_ai([a, b], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 2


# --- Рендер ----------------------------------------------------------------------


async def test_list_quote_render_entities_memory_and_plan_roundtrip(tmp_path: Path) -> None:
    c = candidate(
        "a",
        (
            "У селищі відкрили бібліотеку для всіх мешканців. "
            "Наступного тижня бібліотека планує відкрити ще й читальний зал. "
            "Він має бути доступним для школярів та дорослих. А хто поверне книжки?"
        ),
    )
    data = event(
        ["a"],
        "Бібліотека 📚",
        layout="detailed",
        bullets=[
            {"text": "Відкрили бібліотеку.", "candidate_ids": ["a"]},
            {"text": "Наступного тижня відкриють читальний зал.", "candidate_ids": ["a"]},
        ],
        quote="А хто поверне книжки?",
        quote_candidate_id="a",
    )
    result = await group_with_ai([c], OpenAIProvider(FakeCreate(answer(data)), SETTINGS), CONTEXT)
    header = Header("{date_long}", "Новини", T0, T0, CONTEXT.timezone)
    (part,) = render_series(result.events, header, 3500)
    assert "\n\n▪️ Відкрили бібліотеку.\n▪️ Наступного" in part.text
    assert "\n\n«А хто поверне книжки?»\n\n— Source_A" in part.text
    encoded = part.text.encode("utf-16-le")
    link = next(e for e in part.entities if e.kind == "text_url")
    assert (
        encoded[link.offset * 2 : (link.offset + link.length) * 2].decode("utf-16-le") == "Source_A"
    )
    plan = build_plan("d" * 64, "news", T0, T0, result.events, [part], random_id=lambda: 7)
    item = plan.parts[0].items[0]
    assert item["quote_source_refs"] == [c.refs[0].published_id]
    assert "читальний зал" in item["short_summary"]
    plan.save(tmp_path)
    assert PublicationPlan.load(tmp_path) == plan


async def test_compact_render_has_no_redundant_heading_or_body() -> None:
    c = candidate("a", "У селищі відкрили нову бібліотеку для мешканців.")
    data = event(["a"], "У селищі відкрили бібліотеку.")
    result = await group_with_ai([c], OpenAIProvider(FakeCreate(answer(data)), SETTINGS), CONTEXT)
    header = Header("{date_long}", "Новини", T0, T0, CONTEXT.timezone)
    (part,) = render_series(result.events, header, 3500)
    assert part.text.endswith("1. 📰 У селищі відкрили бібліотеку. — Source_A")
    assert len([e for e in part.entities if e.kind == "bold"]) == 1  # date only


def test_oversized_item_continues_in_next_part() -> None:
    c = candidate("a", "x")
    paragraphs = [f"Абзац {i}: " + "подробиці події " * 8 for i in range(12)]
    big = Event(
        event_id="big",
        title="Велика новина",
        summary="\n\n".join(paragraphs),
        participants=c.refs,
        layout="detailed",
    )
    small = replace(big, event_id="small", title="Мала новина", summary="Коротко.")
    header = Header("{date_long}", "Новини", T0, T0, CONTEXT.timezone)
    parts = render_series([small, big], header, 400)
    assert len(parts) > 2 and all(len(p.text) <= 400 for p in parts)
    joined = "".join(p.text for p in parts)
    assert all(p.strip() in joined for p in paragraphs)
    titles = [e for p in parts for e in p.entities if e.kind == "bold" and e.offset > 0]
    assert len(titles) == 2  # «Мала новина» і «Велика новина» — кожен заголовок один раз
    links = [(p, e) for p in parts for e in p.entities if e.kind == "text_url"]
    assert len(links) == 2
    last, link = links[-1]
    encoded = last.text.encode("utf-16-le")
    assert encoded[link.offset * 2 : (link.offset + link.length) * 2].decode("utf-16-le") == (
        "Source_A"
    )
    plan = build_plan(
        "d" * 64, "news", T0, T0, [small, big], parts, random_id=iter(range(1, 99)).__next__
    )
    assert [i["event_id"] for p in plan.parts for i in p.items] == ["small", "big"]
    assert plan.parts[-1].items[-1]["event_id"] == "big"


def test_schema_requires_structured_layout_bullets_and_quote() -> None:
    item = response_schema()["properties"]["events"]["items"]
    assert {"layout", "bullets", "quote", "quote_candidate_id"} <= set(item["required"])
    assert item["properties"]["bullets"]["items"]["additionalProperties"] is False


def test_body_text_has_no_service_prefix() -> None:
    assert body_text(event(["a"], "Відбій.", is_update=True)) == "Відбій."


# --- Регресії з ревізії 2026-11-12 ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-5 градусів очікують уночі.", "-5 градусів очікують уночі."),
        ("1.5 млн грн виділили на ремонт.", "1.5 млн грн виділили на ремонт."),
        ("- Відкрили бібліотеку.", "Відкрили бібліотеку."),
        ("2. Відкрили бібліотеку.", "Відкрили бібліотеку."),
        ("▪️Відкрили бібліотеку.", "Відкрили бібліотеку."),
    ],
)
def test_bullet_cleanup_keeps_signs_and_decimals(text: str, expected: str) -> None:
    c = candidate("a", "x")
    bullets = [{"text": text, "candidate_ids": ["a"]}, {"text": "Інше.", "candidate_ids": ["a"]}]
    fixed, _ = normalize_event(event(["a"], "Подія", bullets=bullets), [c], {})
    assert fixed["bullets"][0]["text"] == expected


@pytest.mark.parametrize("draft", ["1 млрд грн", "1 грн", "5 млн грн", "1 мільярд"])
def test_multiplier_is_part_of_the_value(draft: str) -> None:
    source = candidate("a", "Виділили 1 млн грн, ще 1,5 млн — на ремонт.")
    assert invented_numbers(f"Виділили {draft}.", [source], None)


def test_date_and_time_parts_allowed_only_from_dates() -> None:
    source = candidate("a", "Засідання 11.11.2025 о 15:30.")
    assert invented_numbers("Засідання 11 листопада 2025 о 15:30.", [source], None) == []
    assert invented_numbers("Виділили 11 млн грн.", [source], None)


def test_split_item_counts_as_published_only_with_its_last_part() -> None:
    c = candidate("a", "x")
    paragraphs = [f"Абзац {i}: " + "подробиці події " * 8 for i in range(12)]
    big = Event(
        event_id="big",
        title="Велика новина",
        summary="\n\n".join(paragraphs),
        participants=c.refs,
        layout="detailed",
    )
    header = Header("{date_long}", "Новини", T0, T0, CONTEXT.timezone)
    parts = render_series([big], header, 400)
    plan = build_plan("d" * 64, "news", T0, T0, [big], parts, random_id=iter(range(1, 99)).__next__)
    assert len(plan.parts) > 2
    assert all(not p.items and not p.source_message_ids for p in plan.parts[:-1])
    assert plan.parts[-1].items[0]["event_id"] == "big" and plan.parts[-1].source_message_ids


async def test_rescue_cannot_exclude_candidate_already_seen_as_news() -> None:
    a = candidate("a", "Відкрили бібліотеку.")
    b = candidate("b", "Відновили 2 трамваї.", 1)
    not_news = {"events": [], "excluded": [{"candidate_id": "b", "reason": "not_news"}]}
    create = FakeCreate(
        answer(event(["a"], "Відкрили бібліотеку."), event(["b"], "Відновили 7 трамваїв.")),
        not_news,
        not_news,
        not_news,
        not_news,
    )
    result = await group_with_ai([a, b], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert [e.title for e in result.events] == ["Відкрили бібліотеку."]
    assert result.unprocessed == ["b"] and "ai_not_news" not in result.excluded
    assert "не можна виключати" in " ".join(create.payload(4)["previous_answer_problems"])


# --- D-037: підбірка новин в одному дописі, меми -------------------------------------


async def test_bundle_post_gives_several_items_and_merges_with_other_posts() -> None:
    bundle = candidate(
        "a",
        "Головне за ранок: уночі обстріляли Харків, 2 поранених. "
        "У Києві відновили світло на лівому березі.",
    )
    other = candidate("b", "Харків: нічний обстріл, 2 людини поранені.", 1)
    data = answer(
        event(["a", "b"], "Уночі обстріляли Харків, 2 поранених."),
        event(["a"], "У Києві відновили світло на лівому березі."),
    )
    result = await group_with_ai(
        [bundle, other], OpenAIProvider(FakeCreate(data), SETTINGS), CONTEXT
    )
    assert len(result.events) == 2 and len({e.event_id for e in result.events}) == 2
    assert {r.source_id for r in result.events[1].participants} == {"source_a"}


def test_post_cannot_be_both_item_and_excluded() -> None:
    c = candidate("a", "Відкрили бібліотеку.")
    data = answer(event(["a"], "Відкрили бібліотеку."))
    data["excluded"] = [{"candidate_id": "a", "reason": "ad"}]
    assert review(data, [c]).global_problems


def test_memes_are_not_a_reason_to_exclude() -> None:
    from newsdigest.ai.prompts import SYSTEM_INSTRUCTIONS

    assert "Memes and humorous" in SYSTEM_INSTRUCTIONS
    assert "polls, memes" not in SYSTEM_INSTRUCTIONS


BUNDLE = (
    "Головне за ранок: уночі обстріляли Харків, 2 поранених. "
    "У Києві відновили світло для 3 районів."
)


async def test_second_news_of_bundle_is_retried_not_lost() -> None:
    c = candidate("a", BUNDLE)
    create = FakeCreate(
        answer(
            event(["a"], "Уночі обстріляли Харків, 2 поранених."),
            event(["a"], "У Києві відновили світло для 9 районів."),
        ),
        answer(event(["a"], "У Києві відновили світло для 3 районів.")),
        {"duplicates": []},
    )
    result = await group_with_ai([c], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert [e.title for e in result.events] == [
        "Уночі обстріляли Харків, 2 поранених.",
        "У Києві відновили світло для 3 районів.",
    ]
    retry = create.payload(1)
    assert [x["id"] for x in retry["candidates"]] == ["a"]
    assert retry["already_accepted"] == {"a": ["Уночі обстріляли Харків, 2 поранених."]}
    assert result.unprocessed == []


async def test_merge_returning_neighbour_news_keeps_drafts() -> None:
    bundle = candidate("a", BUNDLE)
    other = candidate("b", "Харків: нічний обстріл, 2 людини поранені.", 1)
    create = FakeCreate(
        answer(
            event(["a"], "Уночі обстріляли Харків, 2 поранених."),
            event(["a"], "У Києві відновили світло для 3 районів."),
            event(["b"], "Харків обстріляли, 7 поранених."),
        ),
        answer(event(["b"], "Харків обстріляли вночі, 2 поранених.")),
        {"duplicates": [{"draft_ids": ["d1", "d3"]}]},
        answer(
            event(["a", "b"], "Уночі обстріляли Харків, 2 поранених."),
            event(["a"], "У Києві відновили світло для 3 районів."),
        ),
    )
    result = await group_with_ai([bundle, other], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 3 and len({e.event_id for e in result.events}) == 3
    assert create.payload(3)["merge_drafts"] == [
        "Уночі обстріляли Харків, 2 поранених.",
        "Харків обстріляли вночі, 2 поранених.",
    ]
    header = Header("{date_long}", "Новини", T0, T0, CONTEXT.timezone)
    assert render_series(result.events, header, 3500)  # без coverage_mismatch


def test_bundle_post_is_used_only_with_part_of_its_last_news() -> None:
    c = candidate("a", BUNDLE)
    first = Event("e1", "Харків", "x " * 150, c.refs, layout="detailed")
    second = replace(first, event_id="e2", title="Київ")
    header = Header("{date_long}", "Новини", T0, T0, CONTEXT.timezone)
    parts = render_series([first, second], header, 400)
    assert len(parts) == 2
    plan = build_plan(
        "d" * 64, "news", T0, T0, [first, second], parts, random_id=iter(range(1, 9)).__next__
    )
    assert plan.parts[0].source_message_ids == ()
    assert plan.parts[1].source_message_ids == (c.refs[0].published_id,)
    assert [i["event_id"] for i in plan.parts[0].items] == ["e1"]


async def test_bundle_with_unprocessed_rest_is_not_marked_used() -> None:
    c = candidate("a", "У селищі відкрили бібліотеку. Відновили рух 2 трамваїв.")
    nothing: dict[str, Any] = {"events": [], "excluded": []}
    create = FakeCreate(
        answer(
            event(["a"], "У селищі відкрили бібліотеку."),
            {"candidate_ids": ["a"], "title": "", "summary": ""},
        ),
        nothing,
        nothing,
        nothing,
        nothing,
    )
    result = await group_with_ai([c], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert [e.title for e in result.events] == ["У селищі відкрили бібліотеку."]
    assert result.unprocessed == ["a"] and result.unfinished_refs == set(c.refs)
    header = Header("{date_long}", "Новини", T0, T0, CONTEXT.timezone)
    parts = render_series(result.events, header, 3500)
    plan = build_plan(
        "d" * 64, "news", T0, T0, result.events, parts, unfinished=result.unfinished_refs
    )
    assert plan.parts[0].items and plan.parts[0].source_message_ids == ()


def test_prompts_forbid_reporting_phrases_d039() -> None:
    from newsdigest.ai.prompts import SYSTEM_INSTRUCTIONS

    guide = (Path(__file__).resolve().parents[2] / "config/prompts/news-ua.md").read_text(
        encoding="utf-8"
    )
    for text in (SYSTEM_INSTRUCTIONS, guide):
        assert "23:20 — червоний рівень" in text and "повідомили (про)" in text
    assert '"за словами Зеленського"' not in SYSTEM_INSTRUCTIONS


def test_prompts_require_list_for_long_news_d040() -> None:
    from newsdigest.ai.prompts import SYSTEM_INSTRUCTIONS

    guide = (Path(__file__).resolve().parents[2] / "config/prompts/news-ua.md").read_text(
        encoding="utf-8"
    )
    assert "is ALWAYS a list" in SYSTEM_INSTRUCTIONS
    assert "ALWAYS a list" in guide and "енергоострови" in guide
