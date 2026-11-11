from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from newsdigest.cache.run_cache import CachedMessage
from newsdigest.config.loader import parse_config_text
from newsdigest.config.models import TargetChannel
from newsdigest.pipeline.build import build_digest, source_maps
from newsdigest.pipeline.candidates import SourceRef, build_candidates
from newsdigest.pipeline.events import Event, ordered, preview_summary, visible_source
from newsdigest.pipeline.plan import build_plan
from newsdigest.pipeline.render import Header, render_series, utf16_len
from newsdigest.scheduling import Window

KYIV = ZoneInfo("Europe/Kyiv")
START = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)  # 08:00 Київ
END = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)  # 15:00 Київ
WINDOW = Window(START, END)
POSITIONS = {"alpha": 0, "beta": 1, "gamma": 2}
LABELS = {"alpha": "Альфа", "beta": "Бета", "gamma": "Гамма"}


def msg(
    source: str,
    message_id: int,
    minutes: int,
    text: str = "Новина",
    grouped_id: int | None = None,
    peer: int = -1001,
) -> CachedMessage:
    return CachedMessage(
        source_id=source,
        peer_id=peer + POSITIONS.get(source, 9),
        message_id=message_id,
        date_utc=START + timedelta(minutes=minutes),
        fetched_at_utc=END,
        text=text,
        grouped_id=grouped_id,
        version_hash=f"v{message_id}",
        url=f"https://t.me/{source}/{message_id}",
    )


def ref(source: str, minutes: int, message_id: int = 1) -> SourceRef:
    return SourceRef(
        source_id=source,
        source_type="telegram",
        peer_id=-1000 - POSITIONS[source],
        message_id=message_id,
        url=f"https://t.me/{source}/{message_id}",
        date_utc=START + timedelta(minutes=minutes),
        label=LABELS[source],
        position=POSITIONS[source],
    )


def event(
    event_id: str,
    *refs: SourceRef,
    summary: str = "Подія сталася.",
    title: str = "Подія",
    emoji: str = "",
) -> Event:
    return Event(event_id=event_id, title=title, summary=summary, participants=refs, emoji=emoji)


HEADER = Header("{name} · {date} {time}", "Новини", START, END, KYIV)


class TestCandidates:
    def build(self, messages: list[CachedMessage], **kwargs: Any) -> Any:
        return build_candidates(messages, positions=POSITIONS, labels=LABELS, **kwargs)

    def test_album_is_one_material(self) -> None:
        result = self.build(
            [
                msg("alpha", 11, 10, "Підпис до фото", grouped_id=5),
                msg("alpha", 12, 10, "", grouped_id=5),
                msg("alpha", 13, 10, "Ще текст", grouped_id=5),
            ]
        )
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.text == "Підпис до фото\nЩе текст"
        assert candidate.refs[0].message_id == 11

    def test_exact_copies_merge_and_keep_all_refs_t46(self) -> None:
        result = self.build(
            [
                msg("alpha", 1, 30, "Світло   повернули  мешканцям https://x.y/1"),
                msg("beta", 2, 10, "світло повернули мешканцям"),
                msg("gamma", 3, 20, "Інша новина"),
            ]
        )
        assert len(result.candidates) == 2
        merged = next(c for c in result.candidates if len(c.refs) == 2)
        assert [r.source_id for r in merged.refs] == ["beta", "alpha"]
        assert result.excluded == {"exact_duplicate": 1}

    def test_similar_but_different_kept_m20(self) -> None:
        """Схожі тексти з іншим числом не зливаються локально — лише підказка."""
        details = " ".join(f"деталь{i}" for i in range(40))
        base = "У Дніпрі внаслідок обстрілу пошкоджено будинок, постраждали {} людей. " + details
        result = self.build(
            [msg("alpha", 1, 10, base.format(3)), msg("beta", 2, 20, base.format(5))]
        )
        assert len(result.candidates) == 2
        assert all(c.similar_to for c in result.candidates)

    def test_filters_and_already_published_d012(self) -> None:
        result = self.build(
            [
                msg("alpha", 1, 10, "Великий РОЗІГРАШ призів"),
                msg("alpha", 2, 20, "Справжня новина"),
                msg("beta", 3, 30, "Вже публікувалось"),
            ],
            exclude_patterns=["розіграш"],
            published={("telegram", -1001 + 1, 3)},
        )
        assert [c.text for c in result.candidates] == ["Справжня новина"]
        assert result.excluded == {"already_published": 1, "filtered": 1}


class TestEvents:
    def test_visible_source_is_earliest_m21(self) -> None:
        e = event("e", ref("beta", 5), ref("alpha", 20), ref("gamma", 1))
        assert visible_source(e).source_id == "gamma"

    def test_equal_time_prefers_list_order(self) -> None:
        e = event("e", ref("gamma", 5), ref("beta", 5))
        assert visible_source(e).source_id == "beta"

    def test_order_by_newest_participant_m22(self) -> None:
        early = event("a", ref("alpha", 1), ref("beta", 100))  # найновіший допис — 100
        late = event("b", ref("alpha", 50))
        middle = event("c", ref("gamma", 70))
        assert [e.event_id for e in ordered([early, late, middle])] == ["b", "c", "a"]

    def test_preview_summary(self) -> None:
        text = "Перше речення. Друге речення! Третє не потрібне.\nДругий абзац."
        assert preview_summary(text) == "Перше речення. Друге речення!"
        long = "Слово " * 100
        assert len(preview_summary(long)) <= 280
        assert preview_summary(long).endswith("…")


def fragment(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


class TestRender:
    def test_item_format_d025(self) -> None:
        events = [
            event(
                "a",
                ref("alpha", 1),
                title="Відновили світло",
                summary="Повернули у три громади.",
                emoji="⚡",
            ),
            event("b", ref("beta", 2), title="Без викладу", summary=""),
        ]
        (part,) = render_series(events, HEADER, 3500)
        assert part.text == (
            "Новини · 11.11 15:00\n\n"
            "1. ⚡ Відновили світло\nПовернули у три громади. — Альфа\n\n"
            "2. Без викладу — Бета"
        )
        kinds = [(e.kind, fragment(part.text, e.offset, e.length)) for e in part.entities]
        assert kinds == [
            ("bold", "Новини · 11.11 15:00"),
            ("bold", "Відновили світло"),
            ("text_url", "Альфа"),
            ("bold", "Без викладу"),
            ("text_url", "Бета"),
        ]
        assert part.links == ["https://t.me/alpha/1", "https://t.me/beta/1"]

    def test_emoji_offsets_utf16_m06(self) -> None:
        e = event("a", ref("alpha", 1), title="Прапор 🇺🇦 і 👨‍👩‍👧", summary="Текст 🇺🇦.", emoji="🇺🇦")
        (part,) = render_series([e], HEADER, 3500)
        bold = [x for x in part.entities if x.kind == "bold"][1]
        link = next(x for x in part.entities if x.kind == "text_url")
        assert fragment(part.text, bold.offset, bold.length) == "Прапор 🇺🇦 і 👨‍👩‍👧"
        assert fragment(part.text, link.offset, link.length) == "Альфа"

    def test_update_prefix_and_label_cleanup(self) -> None:
        r = SourceRef(
            "alpha", "telegram", -1, 1, "https://t.me/a/1", START, "Канал\nз переносом", 0
        )
        (part,) = render_series(
            [Event("u", "Ціна змінилась", "Деталі.", (r,), emoji="💧", is_update=True)],
            HEADER,
            3500,
        )
        assert "1. 💧 Оновлення: Ціна змінилась\nДеталі. — Канал з переносом" in part.text

    def test_empty_digest_has_no_parts(self) -> None:
        assert render_series([], HEADER, 3500) == []

    def test_split_between_whole_items_m03_m04_m07(self) -> None:
        events = [
            event(f"e{i}", ref("alpha", i, i), title=f"Подія {i}", summary="x" * 80)
            for i in range(30)
        ]
        parts = render_series(events, HEADER, 600)
        assert len(parts) > 1
        assert all(len(p.text) <= 600 for p in parts)
        assert all(p.count == len(parts) for p in parts)
        assert all(f"Частина {p.index}/{len(parts)}" in p.text.splitlines()[0] for p in parts)
        placed = [eid for p in parts for eid in p.event_ids]
        assert placed == [e.event_id for e in events]
        numbers = [block.split(".")[0] for p in parts for block in p.text.split("\n\n")[1:]]
        assert numbers == [str(i) for i in range(1, 31)]

    def test_part_count_stable_across_9_to_10_m05(self) -> None:
        events = [event(f"e{i}", ref("alpha", i, i), summary="y" * 150) for i in range(10)]
        parts = render_series(events, HEADER, 230)
        assert len(parts) == 10
        assert all(len(p.text) <= 230 for p in parts)
        assert parts[-1].text.startswith("Новини · 11.11 15:00 · Частина 10/10")

    def test_too_long_item_continues_without_loss_d036(self) -> None:
        parts = render_series([event("big", ref("alpha", 1), summary="z" * 500)], HEADER, 200)
        assert len(parts) > 1 and all(len(p.text) <= 200 for p in parts)
        assert sum(p.text.count("z") for p in parts) == 500
        assert all(p.event_ids == ("big",) for p in parts)

    def test_utf16_len(self) -> None:
        assert utf16_len("abc") == 3
        assert utf16_len("🇺🇦") == 4
        assert utf16_len("👨‍👩‍👧") == 8


class TestPlan:
    def test_plan_ids_hashes_and_metadata(self) -> None:
        events = [
            event("a", ref("alpha", 1), ref("beta", 3, 7), title="Перша", emoji="⚡"),
            event("b", ref("gamma", 2)),
        ]
        parts = render_series(events, HEADER, 3500)
        counter = iter([5, 5, 9])
        plan = build_plan(
            "d" * 64, "news", START, END, events, parts, random_id=lambda: next(counter)
        )
        (part,) = plan.parts
        assert part.part_key == "d" * 64 + ":1"
        assert part.random_id == 5
        assert part.payload_hash == parts[0].payload_hash
        assert [i["position"] for i in part.items] == [1, 2]
        assert part.items[0]["displayed_source_id"] == "alpha"
        assert part.items[0]["title"] == "Перша" and part.items[0]["emoji"] == "⚡"
        assert {"type": "telegram", "peer_id": -1001, "message_id": 7} in part.source_message_ids
        assert part.entities[0] == {"type": "bold", "offset": 0, "length": 20}
        assert len(plan.manifest_hash) == 64
        again = build_plan("d" * 64, "news", START, END, events, parts, random_id=lambda: 1)
        assert again.manifest_hash == plan.manifest_hash

    def test_random_ids_unique_across_parts(self) -> None:
        events = [event(f"e{i}", ref("alpha", i, i), summary="y" * 150) for i in range(4)]
        parts = render_series(events, HEADER, 230)
        values = iter([1, 1, 2, 2, 3, 4])
        plan = build_plan(
            "d" * 64, "news", START, END, events, parts, random_id=lambda: next(values)
        )
        assert [p.random_id for p in plan.parts] == [1, 2, 3, 4]


def channel() -> TargetChannel:
    data = {
        "schema_version": 4,
        "target_channels": [
            {
                "id": "news",
                "name": "Новини",
                "telegram": "@my_digest",
                "schedule": {"times": ["08:00", "15:00"]},
                "scan_start_at": "2026-11-01T00:00:00+02:00",
                "source_channels": [
                    {"id": "alpha", "ref": "@alpha_news", "name": "Альфа"},
                    {"id": "beta", "ref": "@beta_news"},
                ],
                "filters": {"exclude_patterns": ["реклама"]},
            }
        ],
    }
    return parse_config_text(json.dumps(data, ensure_ascii=False)).target_channels[0]


def test_source_maps_use_config_name_then_id() -> None:
    positions, labels = source_maps(channel())
    assert positions == {"alpha": 0, "beta": 1}
    assert labels == {"alpha": "Альфа", "beta": "beta"}


async def test_build_digest_end_to_end_draft() -> None:
    messages = [
        msg("beta", 2, 30, "Друга новина. Деталі."),
        msg("alpha", 1, 10, "Перша новина."),
        msg("alpha", 3, 40, "Купуйте! Реклама."),
    ]
    built = await build_digest(channel(), WINDOW, KYIV, 3500, messages)
    assert [(e.title, e.summary) for e in built.events] == [
        ("Перша новина.", ""),
        ("Друга новина.", "Деталі."),
    ]
    assert built.candidates.excluded == {"filtered": 1}
    (part,) = built.plan.parts
    assert part.text.endswith("2. 📰 Друга новина.\nДеталі. — beta")


async def test_build_digest_without_news_has_no_parts() -> None:
    built = await build_digest(channel(), WINDOW, KYIV, 3500, [])
    assert built.plan.parts == ()


def test_header_date_long_d030() -> None:
    header = Header("{date_long}", "Новини", START, END, KYIV)
    assert header.render(1, 1) == "11 листопада"
    assert header.render(2, 3) == "11 листопада · Частина 2/3"
