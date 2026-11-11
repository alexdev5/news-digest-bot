from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from newsdigest.ai.grouping import ChannelContext, PublishedItem, group_with_ai, validate
from newsdigest.ai.prompts import SYSTEM_INSTRUCTIONS, response_schema
from newsdigest.ai.provider import AiError, AiSettings, OpenAIProvider
from newsdigest.pipeline.candidates import Candidate, SourceRef
from newsdigest.pipeline.events import visible_source

KYIV = ZoneInfo("Europe/Kyiv")
T0 = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
SETTINGS = AiSettings("gpt-6.1-terra", "medium", 8192)
CONTEXT = ChannelContext(
    topic="Новини Дніпра",
    channel_prompt="Пиши стисло.",
    timezone=KYIV,
    published_recent=(PublishedItem("Відновили світло", "У трьох громадах.", T0),),
)


def response(data: Any = None, *, status: str = "completed", refusal: bool = False) -> Any:
    content = (
        [SimpleNamespace(type="refusal", refusal="no")]
        if refusal
        else [SimpleNamespace(type="output_text", text=json.dumps(data, ensure_ascii=False))]
    )
    return SimpleNamespace(
        status=status,
        model="gpt-6.1-terra-2026",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[SimpleNamespace(type="message", content=content)],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
    )


class FakeCreate:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, SimpleNamespace) else response(answer)

    def payload(self, call: int) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.calls[call]["input"])
        return result


def ref(source: str, minutes: int, position: int, message_id: int = 1) -> SourceRef:
    return SourceRef(
        source_id=source,
        source_type="telegram",
        peer_id=-1000 - position,
        message_id=message_id,
        url=f"https://t.me/{source}/{message_id}",
        date_utc=T0 + timedelta(minutes=minutes),
        label=source.title(),
        position=position,
    )


CANDIDATES = [
    Candidate("c1", "У місті відновили електропостачання у 3 громадах.", (ref("beta", 20, 1),)),
    Candidate(
        "c2", "Світло повернули мешканцям міста, 3 громади з електрикою.", (ref("alpha", 5, 0),)
    ),
    Candidate("c3", "Розіграш iPhone! Підписуйтесь.", (ref("gamma", 30, 2),)),
    Candidate("c4", "Призначили додаткові потяги на вихідні.", (ref("alpha", 40, 0, 2),)),
]

GOOD = {
    "events": [
        {
            "candidate_ids": ["c1", "c2"],
            "emoji": "⚡",
            "title": "Відновили електропостачання",
            "summary": "Світло повернули у 3 громади.",
            "is_update": False,
        },
        {
            "candidate_ids": ["c4"],
            "emoji": "🚆",
            "title": "Додали потяги на вихідні",
            "summary": "Призначили додаткові рейси.",
            "is_update": False,
        },
    ],
    "excluded": [{"candidate_id": "c3", "reason": "ad"}],
}


class TestProvider:
    async def test_request_contract(self) -> None:
        create = FakeCreate(GOOD)
        data, usage = await OpenAIProvider(create, SETTINGS).complete_json(
            "instr", {"a": 1}, response_schema(), "digest_events"
        )
        assert data == GOOD
        assert (usage.model, usage.input_tokens, usage.output_tokens) == (
            "gpt-6.1-terra-2026",
            1000,
            200,
        )
        call = create.calls[0]
        assert call["model"] == "gpt-6.1-terra"
        assert call["store"] is False and call["stream"] is False
        assert call["reasoning"] == {"effort": "medium"}
        assert call["text"]["format"]["strict"] is True
        assert "tools" not in call and "previous_response_id" not in call

    @pytest.mark.parametrize(
        ("answer", "code"),
        [
            (response(GOOD, status="incomplete"), "incomplete"),
            (response(refusal=True), "refusal"),
            (SimpleNamespace(status="completed", output=[], usage=None, model="m"), "invalid_json"),
        ],
    )
    async def test_bad_responses(self, answer: Any, code: str) -> None:
        with pytest.raises(AiError) as info:
            await OpenAIProvider(FakeCreate(answer), SETTINGS).complete_json("i", {}, {}, "n")
        assert info.value.code == code

    async def test_api_error_codes(self) -> None:
        class RateLimitError(Exception):
            def __init__(self) -> None:
                super().__init__("quota")
                self.body = {"code": "insufficient_quota"}

        with pytest.raises(AiError) as info:
            await OpenAIProvider(FakeCreate(RateLimitError()), SETTINGS).complete_json(
                "i", {}, {}, "n"
            )
        assert info.value.code == "insufficient_quota"


class TestGrouping:
    async def test_groups_and_excludes(self) -> None:
        create = FakeCreate(GOOD)
        result = await group_with_ai(CANDIDATES, OpenAIProvider(create, SETTINGS), CONTEXT)
        assert len(result.events) == 2
        merged = result.events[0]
        assert {r.source_id for r in merged.participants} == {"alpha", "beta"}
        assert visible_source(merged).source_id == "alpha"  # найраніший допис (D-015)
        assert merged.emoji == "⚡"
        assert result.excluded == {"ai_ad": 1}
        assert result.tokens == {"input": 1000, "output": 200, "requests": 1}

    async def test_payload_has_data_not_instructions(self) -> None:
        create = FakeCreate(GOOD)
        await group_with_ai(CANDIDATES, OpenAIProvider(create, SETTINGS), CONTEXT)
        payload = create.payload(0)
        assert create.calls[0]["instructions"] == SYSTEM_INSTRUCTIONS
        assert [c["id"] for c in payload["candidates"]] == ["c1", "c2", "c3", "c4"]
        assert payload["channel_prompt"] == "Пиши стисло."
        assert payload["published_recent"][0]["title"] == "Відновили світло"
        assert "api_key" not in json.dumps(payload)

    async def test_invalid_answer_retried_with_feedback(self) -> None:
        bad = json.loads(json.dumps(GOOD))
        bad["events"][0]["candidate_ids"] = ["c1", "zzz"]
        create = FakeCreate(bad, GOOD)
        result = await group_with_ai(CANDIDATES, OpenAIProvider(create, SETTINGS), CONTEXT)
        assert len(create.calls) == 2
        assert "zzz" in " ".join(create.payload(1)["previous_answer_problems"])
        assert len(result.events) == 2

    async def test_invented_number_rejected(self) -> None:
        bad = json.loads(json.dumps(GOOD))
        bad["events"][0]["summary"] = "Світло повернули у 7 громад."
        problems, _ = validate(bad, CANDIDATES)
        assert any("7" in p for p in problems)

    async def test_missing_candidate_repaired(self) -> None:
        partial = json.loads(json.dumps(GOOD))
        partial["events"] = partial["events"][:1]
        repair = {
            "events": [
                {
                    "candidate_ids": ["c4"],
                    "emoji": "🚆",
                    "title": "Додали потяги",
                    "summary": "",
                    "is_update": False,
                }
            ],
            "excluded": [],
        }
        create = FakeCreate(partial, repair)
        result = await group_with_ai(CANDIDATES, OpenAIProvider(create, SETTINGS), CONTEXT)
        assert [c["id"] for c in create.payload(1)["candidates"]] == ["c4"]
        assert len(result.events) == 2 and result.unprocessed == []

    async def test_still_missing_is_counted_not_lost(self) -> None:
        partial = json.loads(json.dumps(GOOD))
        partial["events"] = partial["events"][:1]
        empty: dict[str, Any] = {"events": [], "excluded": []}
        create = FakeCreate(partial, empty, empty)
        result = await group_with_ai(CANDIDATES, OpenAIProvider(create, SETTINGS), CONTEXT)
        assert result.unprocessed == ["c4"]
        assert result.excluded["ai_unprocessed"] == 1

    async def test_no_valid_answer_fails_run_d011(self) -> None:
        bad = {
            "events": [
                {
                    "candidate_ids": ["nope"],
                    "emoji": "",
                    "title": "x",
                    "summary": "",
                    "is_update": False,
                }
            ],
            "excluded": [],
        }
        create = FakeCreate(bad, bad, bad)
        with pytest.raises(AiError) as info:
            await group_with_ai(CANDIDATES, OpenAIProvider(create, SETTINGS), CONTEXT)
        assert info.value.code == "invalid_answer"

    def test_double_use_rejected(self) -> None:
        bad = json.loads(json.dumps(GOOD))
        bad["excluded"].append({"candidate_id": "c1", "reason": "ad"})
        problems, _ = validate(bad, CANDIDATES)
        assert any("двічі" in p for p in problems)


async def test_one_bad_event_does_not_discard_others() -> None:
    """Подія з вигаданим числом повторюється окремо; решта приймається одразу."""
    bad = json.loads(json.dumps(GOOD))
    bad["events"][1]["summary"] = "Призначили 12 додаткових рейсів."
    fixed = {
        "events": [
            {
                "candidate_ids": ["c4"],
                "emoji": "🚆",
                "title": "Додали потяги на вихідні",
                "summary": "Призначили додаткові рейси.",
                "is_update": False,
            }
        ],
        "excluded": [],
    }
    create = FakeCreate(bad, fixed)
    result = await group_with_ai(CANDIDATES, OpenAIProvider(create, SETTINGS), CONTEXT)
    assert len(result.events) == 2
    assert [c["id"] for c in create.payload(1)["candidates"]] == ["c4"]
    assert "12" in " ".join(create.payload(1)["previous_answer_problems"])


def test_date_parts_count_as_source_numbers() -> None:
    candidates = [
        Candidate("d1", "Засідання 11.11 о 15:30, 1 000 учасників.", (ref("alpha", 1, 0),))
    ]
    answer = {
        "events": [
            {
                "candidate_ids": ["d1"],
                "emoji": "🏛",
                "title": "Засідання 11 листопада",
                "summary": "Початок о 15:30, учасників 1000.",
                "is_update": False,
            }
        ],
        "excluded": [],
    }
    problems, covered = validate(answer, candidates)
    assert problems == [] and covered == {"d1"}


def test_channel_attribution_rejected_d030() -> None:
    bad = json.loads(json.dumps(GOOD))
    bad["events"][1]["summary"] = "За даними «Alpha», призначили додаткові рейси."
    problems, covered = validate(bad, CANDIDATES)
    assert any("не згадуй канали" in p and "Alpha" in p for p in problems)
    assert "c4" not in covered
