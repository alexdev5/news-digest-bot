from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from newsdigest.cache.run_cache import CacheManager, RunCache, RunManifest
from newsdigest.config.loader import parse_config_text
from newsdigest.config.models import TargetChannel
from newsdigest.identity import digest_key
from newsdigest.scheduling import Window
from newsdigest.state import StateStore
from newsdigest.telegram.rate_limit import RateLimiter
from newsdigest.telegram.scanner import HistoryScanner, ScanSummary
from newsdigest.telegram.types import FloodWaitPause, RawMessage
from tests.fakes import FakeClock, FakeGateway, channel_peer, messages_every, utc

START = utc(2026, 11, 11, 19, 0)
END = utc(2026, 11, 12, 6, 0)
WINDOW = Window(START, END)
A, B = -1001000000001, -1001000000002


def target(**overrides: Any) -> TargetChannel:
    import json

    data: dict[str, Any] = {
        "schema_version": 4,
        "target_channels": [
            {
                "id": "news",
                "name": "Новини",
                "telegram": "@my_digest",
                "schedule": {"times": ["08:00", "21:00"]},
                "scan_start_at": "2026-11-01T00:00:00+02:00",
                "source_channels": [
                    {"id": "alpha", "ref": "@alpha_news"},
                    {"id": "beta", "ref": "@beta_news"},
                ],
                **overrides,
            }
        ],
    }
    return parse_config_text(json.dumps(data, ensure_ascii=False)).target_channels[0]


class Env:
    def __init__(self, tmp_path: Path, gateway: FakeGateway) -> None:
        self.clock = FakeClock(END + timedelta(seconds=5))
        self.store = StateStore(tmp_path / "state")
        self.cache_manager = CacheManager(tmp_path / "cache")
        self.gateway = gateway
        self.limiter = RateLimiter(self.store, self.clock, self.clock.sleep, min_gap_seconds=1)
        self.scanner = HistoryScanner(gateway, self.limiter, self.store, self.clock, page_size=10)

    def open(self, channel: TargetChannel) -> tuple[RunCache, RunManifest]:
        return self.cache_manager.open_or_create(
            digest_key(channel.id, WINDOW), channel.id, START, END, self.clock()
        )

    async def scan(self, channel: TargetChannel) -> tuple[ScanSummary, RunCache]:
        cache, manifest = self.open(channel)
        return await self.scanner.scan(channel, WINDOW, cache, manifest), cache


def gateway(alpha: list[RawMessage], beta: list[RawMessage] | None = None) -> FakeGateway:
    return FakeGateway(
        peers={
            "@alpha_news": channel_peer(A, "alpha_news"),
            "@beta_news": channel_peer(B, "beta_news"),
        },
        history={A: alpha, B: beta or []},
    )


async def test_paginates_whole_window_t15(tmp_path: Path) -> None:
    """Понад 100 дописів (тут — понад 10 на сторінку) збираються до start без обрізання."""
    alpha = messages_every(START - timedelta(hours=2), 160, 5)  # 13 годин по 5 хв
    env = Env(tmp_path, gateway(alpha))
    summary, cache = await env.scan(target())
    in_window = [m for m in alpha if WINDOW.contains(m.date_utc)]
    assert summary.statuses == {"alpha": "complete", "beta": "complete"}
    assert summary.in_window == len(in_window) == 132
    assert sorted(m.message_id for m in cache.read_messages()) == [m.message_id for m in in_window]
    assert env.gateway.history_calls > 10


async def test_window_bounds_t13(tmp_path: Path) -> None:
    edge = [
        RawMessage(1, START - timedelta(seconds=1), None, "до вікна"),
        RawMessage(2, START, None, "рівно start"),
        RawMessage(3, END - timedelta(seconds=1), None, "перед end"),
        RawMessage(4, END, None, "рівно end"),
    ]
    env = Env(tmp_path, gateway(edge))
    _, cache = await env.scan(target())
    assert [m.message_id for m in cache.read_messages()] == [2, 3]


async def test_exclusions_t21_and_late_edits_d024(tmp_path: Path) -> None:
    """D-024 (замінює T20): редагування після межі не відкидає допис, береться поточна версія."""
    mid = START + timedelta(hours=1)
    items = [
        RawMessage(1, mid, None, "", is_service=True),
        RawMessage(2, mid, None, "", has_media=True),
        RawMessage(3, mid, END + timedelta(minutes=1), "редаговано після cutoff"),
        RawMessage(4, mid, END - timedelta(minutes=1), "редаговано до cutoff"),
        RawMessage(5, mid, None, "   "),
    ]
    env = Env(tmp_path, gateway(items))
    summary, cache = await env.scan(target())
    cached = {m.message_id: m for m in cache.read_messages()}
    assert sorted(cached) == [3, 4]
    assert cached[3].text == "редаговано після cutoff"
    assert cached[3].edit_date_utc == END + timedelta(minutes=1)
    assert summary.edited_after_cutoff == 1
    assert summary.excluded == {"invalid_content": 1, "service": 1, "unsupported_media": 1}


async def test_resume_after_failure_without_duplicates_t16(tmp_path: Path) -> None:
    alpha = messages_every(START, 60, 10)
    gw = gateway(alpha)
    gw.fail_after_pages = 2
    env = Env(tmp_path, gw)
    summary, _ = await env.scan(target())
    assert summary.statuses["alpha"] == "partial"
    gw.fail_after_pages = None
    summary, cache = await env.scan(target())
    assert summary.statuses["alpha"] == "complete"
    ids = [m.message_id for m in cache.read_messages()]
    assert len(ids) == len(set(ids)) == len([m for m in alpha if WINDOW.contains(m.date_utc)])


async def test_unavailable_source_does_not_stop_others_t22(tmp_path: Path) -> None:
    gw = gateway([], messages_every(START, 3, 30))
    del gw.peers["@alpha_news"]
    env = Env(tmp_path, gw)
    summary, _ = await env.scan(target())
    assert summary.statuses == {"alpha": "unavailable", "beta": "complete"}
    assert summary.in_window == 3


async def test_empty_channel_is_complete(tmp_path: Path) -> None:
    env = Env(tmp_path, gateway([]))
    summary, _ = await env.scan(target())
    assert summary.statuses["alpha"] == "complete" and summary.in_window == 0


async def test_peer_change_blocks_source(tmp_path: Path) -> None:
    """Username тепер вказує на інший канал — не перемикаємось мовчки (v4 §6)."""
    env = Env(tmp_path, gateway(messages_every(START, 2, 30)))
    await env.scan(target())
    env.gateway.peers["@alpha_news"] = channel_peer(-1009999999999, "alpha_news")
    env.cache_manager.run(digest_key("news", WINDOW)).delete()
    summary, _ = await env.scan(target())
    assert summary.statuses["alpha"] == "unavailable"


async def test_non_broadcast_rejected(tmp_path: Path) -> None:
    gw = gateway([])
    peer = gw.peers["@alpha_news"]
    gw.peers["@alpha_news"] = type(peer)(peer.peer_id, peer.title, peer.username, False)
    env = Env(tmp_path, gw)
    summary, _ = await env.scan(target())
    assert summary.statuses["alpha"] == "unavailable"


async def test_disabled_source_and_sites(tmp_path: Path) -> None:
    channel = target(
        source_channels=[
            {"id": "alpha", "ref": "@alpha_news"},
            {"id": "beta", "ref": "@beta_news", "enabled": False},
        ],
        source_sites=[
            {
                "id": "site",
                "name": "Сайт",
                "url": "https://example.com",
                "rss": "https://example.com/rss",
            }
        ],
    )
    env = Env(tmp_path, gateway([]))
    summary, _ = await env.scan(channel)
    assert summary.statuses == {"site": "unavailable", "alpha": "complete", "beta": "excluded"}
    assert env.gateway.resolve_calls == 1


async def test_short_flood_wait_retried_inline(tmp_path: Path) -> None:
    gw = gateway(messages_every(START, 3, 30))
    gw.flood_on_history = [5]
    env = Env(tmp_path, gw)
    summary, _ = await env.scan(target())
    assert summary.statuses["alpha"] == "complete"
    assert env.store.load_rate_limits().pause_until_utc is None


async def test_long_flood_wait_persisted_t75(tmp_path: Path) -> None:
    gw = gateway(messages_every(START, 3, 30), messages_every(START, 3, 30))
    gw.flood_on_history = [3600]
    env = Env(tmp_path, gw)
    summary, _ = await env.scan(target())
    assert summary.statuses == {"alpha": "partial", "beta": "partial"}
    pause = StateStore(tmp_path / "state").load_rate_limits().pause_until_utc
    assert pause is not None and pause > env.clock() + timedelta(minutes=59)
    with pytest.raises(FloodWaitPause):
        await env.limiter.call(env.gateway.account)


async def test_min_gap_between_requests(tmp_path: Path) -> None:
    env = Env(tmp_path, gateway(messages_every(START, 40, 10)))
    started = env.clock()
    await env.scan(target())
    calls = env.gateway.history_calls + env.gateway.resolve_calls
    assert (env.clock() - started).total_seconds() >= calls - 1


async def test_private_channel_url(tmp_path: Path) -> None:
    gw = gateway(messages_every(START, 1, 30))
    gw.peers["@alpha_news"] = channel_peer(A, "")
    gw.peers["@alpha_news"] = type(gw.peers["@alpha_news"])(A, "Приватний", None, True)
    env = Env(tmp_path, gw)
    _, cache = await env.scan(target())
    assert cache.read_messages()[0].url == "https://t.me/c/1000000001/1"


def test_describe_code_delivery() -> None:
    from types import SimpleNamespace

    from telethon.tl.types import auth

    from newsdigest.telegram.telethon_gateway import describe_code_delivery

    sent = SimpleNamespace(
        type=auth.SentCodeTypeApp(length=5), next_type=auth.CodeTypeSms(), timeout=60
    )
    text = describe_code_delivery(sent)
    assert "чат «Telegram»" in text and "Sms" in text and "60 с" in text
    email = SimpleNamespace(
        type=auth.SentCodeTypeSetUpEmailRequired(), next_type=None, timeout=None
    )
    assert "Пошта для входу" in describe_code_delivery(email)


def test_render_qr_contains_blocks() -> None:
    from newsdigest.telegram.telethon_gateway import render_qr

    text = render_qr("tg://login?token=abc")
    assert len(text.splitlines()) > 10
    assert "█" in text or "▀" in text
