from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from newsdigest.ai.grouping import (
    MAX_BATCH_CHARS,
    build_payload,
    group_with_ai,
    split_long_candidates,
    validate,
)
from newsdigest.ai.prompts import response_schema
from newsdigest.ai.provider import AiSettings, OpenAIProvider
from newsdigest.pipeline.candidates import Candidate
from newsdigest.pipeline.render import Header, render_series
from tests.unit.test_ai import CANDIDATES, CONTEXT, GOOD, SETTINGS, FakeCreate, ref


def event(ids: list[str], title: str, summary: str = "") -> dict[str, Any]:
    return {
        "candidate_ids": ids,
        "title": title,
        "summary": summary,
        "emoji": "📰",
        "is_update": False,
        "layout": "detailed" if summary else "compact",
        "bullets": [],
        "quote": "",
        "quote_candidate_id": "",
    }


async def test_international_news_cannot_be_excluded_as_off_topic() -> None:
    c = Candidate("spain", "В Іспанії протестують через вартість житла.", (ref("alpha", 1, 0),))
    bad = {"events": [], "excluded": [{"candidate_id": "spain", "reason": "off_topic"}]}
    good = {"events": [event(["spain"], "В Іспанії протестують через житло")], "excluded": []}
    create = FakeCreate(bad, good)
    result = await group_with_ai([c], OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 1 and result.excluded == {}
    assert len(create.calls) == 2
    reasons = response_schema()["properties"]["excluded"]["items"]["properties"]["reason"]["enum"]
    assert "off_topic" not in reasons


def test_long_post_keeps_final_correction_in_payload() -> None:
    text = "Подробиці. " * 600 + "Уточнення: обмін ще не відбувся."
    c = Candidate("long", text, CANDIDATES[0].refs)
    assert build_payload([c], CONTEXT)["candidates"][0]["text"] == text
    huge = replace(c, text=text * 20)
    fragments = split_long_candidates([huge])
    assert len(fragments) > 1
    assert "".join(f.text for f in fragments) == huge.text
    assert all(len(f.text) < MAX_BATCH_CHARS and f.refs == huge.refs for f in fragments)
    assert len({f.candidate_id for f in fragments}) == len(fragments)


async def test_same_event_in_two_batches_is_merged_with_all_refs() -> None:
    candidates = [
        Candidate("a", "Відновили світло. " * 2500, (ref("alpha", 1, 0),)),
        Candidate("b", "Світло відновлено. " * 2500, (ref("beta", 2, 1),)),
    ]
    create = FakeCreate(
        {"events": [event(["a"], "Відновили світло")], "excluded": []},
        {"events": [event(["b"], "Світло відновлено")], "excluded": []},
        {"duplicates": [{"draft_ids": ["d1", "d2"]}]},
        {"events": [event(["a", "b"], "Відновили світло")], "excluded": []},
    )
    result = await group_with_ai(candidates, OpenAIProvider(create, SETTINGS), CONTEXT)
    assert result.batches == 2 and len(result.events) == 1
    assert {r.source_id for r in result.events[0].participants} == {"alpha", "beta"}
    assert len(create.calls) == 4
    assert create.calls[-1]["text"]["format"]["schema"]["properties"]["excluded"]["maxItems"] == 0


async def test_merge_that_loses_an_event_keeps_original_drafts() -> None:
    candidates = [replace(c, text=(c.text * 2000)[:40000]) for c in CANDIDATES[:2]]
    nothing: dict[str, Any] = {"events": [], "excluded": []}
    create = FakeCreate(
        {"events": [event(["c1"], "Відновили світло")], "excluded": []},
        {"events": [event(["c2"], "Світло повернули")], "excluded": []},
        {"duplicates": [{"draft_ids": ["d1", "d2"]}]},
        {"events": [event(["c1"], "Відновили світло")], "excluded": []},
        nothing,
    )
    result = await group_with_ai(candidates, OpenAIProvider(create, SETTINGS), CONTEXT)
    assert [e.title for e in result.events] == ["Відновили світло", "Світло повернули"]


def test_city_name_is_not_channel_attribution_and_long_summary_is_valid() -> None:
    c = Candidate(
        "city",
        "У Дніпрі відремонтували водогін. " * 40,
        (replace(ref("alpha", 1, 0), label="Дніпро"),),
    )
    answer = {
        "events": [
            event(["city"], "У Дніпрі відремонтували водогін", "Відремонтували водогін. " * 30)
        ],
        "excluded": [],
    }
    problems, covered = validate(answer, [c])
    assert problems == [] and covered == {"city"}
    answer["events"][0]["summary"] = "За даними «Дніпро», відремонтували водогін."
    assert validate(answer, [c])[0]


async def test_exclusion_audit_contains_ids_and_reason_without_content() -> None:
    result = await group_with_ai(CANDIDATES, OpenAIProvider(FakeCreate(GOOD), SETTINGS), CONTEXT)
    assert result.excluded_sources == [
        {
            "source_id": "gamma",
            "peer_id": -1002,
            "message_id": 1,
            "candidate_id": "c3",
            "reason": "ai_ad",
        }
    ]
    assert "iPhone" not in json.dumps(result.excluded_sources)


async def test_default_request_has_no_application_output_token_quota() -> None:
    create = FakeCreate(GOOD)
    await OpenAIProvider(create, AiSettings(SETTINGS.model, "medium", None)).complete_json(
        "instructions", {}, response_schema(), "digest_events"
    )
    assert "max_output_tokens" not in create.calls[0]


async def test_fuller_summary_keeps_paragraphs_and_telegram_entities() -> None:
    create = FakeCreate(
        {
            "events": [
                event(
                    ["c1"], "Відновили світло", "Світло повернули у 3 громади.\n\nРемонт завершили."
                )
            ],
            "excluded": [],
        }
    )
    source = replace(
        CANDIDATES[0],
        text=("У місті відновили електропостачання у 3 громадах. Ремонт завершили. " * 10),
    )
    result = await group_with_ai([source], OpenAIProvider(create, SETTINGS), CONTEXT)
    header = Header(
        "{date_long}", "News", CANDIDATES[0].earliest, CANDIDATES[0].latest, CONTEXT.timezone
    )
    parts = render_series(result.events, header, 3500)
    assert "громади.\n\nРемонт" in parts[0].text
    assert len(parts[0].links) == 1
