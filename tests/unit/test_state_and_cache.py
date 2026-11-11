from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from newsdigest.cache.run_cache import CachedMessage, CacheManager
from newsdigest.state import (
    LockBusyError,
    ProcessLock,
    RateLimitState,
    StateCorruptError,
    StateStore,
)
from newsdigest.state.atomic import read_json_checked, write_json_atomic
from tests.fakes import utc

NOW = utc(2026, 11, 12, 6, 0)
KEY = "a" * 64


class TestAtomicState:
    def test_roundtrip_and_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "x.json"
        assert read_json_checked(path) is None
        write_json_atomic(path, {"b": 1, "a": [1, 2]})
        assert read_json_checked(path) == {"b": 1, "a": [1, 2]}
        assert not list(tmp_path.glob(".*.tmp"))

    def test_checksum_mismatch_is_corruption(self, tmp_path: Path) -> None:
        """T74/T76: пошкоджений файл не скидається мовчки."""
        path = tmp_path / "x.json"
        write_json_atomic(path, {"value": 1})
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["data"]["value"] = 2
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(StateCorruptError):
            read_json_checked(path)

    def test_garbage_is_corruption(self, tmp_path: Path) -> None:
        path = tmp_path / "x.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(StateCorruptError):
            read_json_checked(path)

    def test_state_rejects_unknown_fields(self, tmp_path: Path) -> None:
        """T32: у /state не можна протягнути текст новини зайвим полем."""
        store = StateStore(tmp_path)
        write_json_atomic(tmp_path / "rate_limits.json", {"schema_version": 1, "text": "новина"})
        with pytest.raises(StateCorruptError):
            store.load_rate_limits()

    def test_rate_limit_persists(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path)
        store.save_rate_limits(RateLimitState(pause_until_utc=NOW, reason="flood_wait"))
        assert StateStore(tmp_path).load_rate_limits().pause_until_utc == NOW


class TestProcessLock:
    def test_second_holder_rejected(self, tmp_path: Path) -> None:
        first = ProcessLock(tmp_path / "process.lock")
        first.acquire()
        try:
            with pytest.raises(LockBusyError):
                ProcessLock(tmp_path / "process.lock").acquire()
        finally:
            first.release()
        with ProcessLock(tmp_path / "process.lock"):
            pass


def message(message_id: int, text: str = "t", version: str = "v") -> CachedMessage:
    return CachedMessage(
        source_id="src",
        peer_id=-1001,
        message_id=message_id,
        date_utc=NOW - timedelta(minutes=message_id),
        fetched_at_utc=NOW,
        text=text,
        version_hash=version,
        url=f"https://t.me/src/{message_id}",
    )


class TestRunCache:
    def test_ttl_not_extended_by_reopen(self, tmp_path: Path) -> None:
        """T28: читання/відкриття кешу не подовжує TTL."""
        manager = CacheManager(tmp_path, ttl_seconds=3600)
        _, created = manager.open_or_create(KEY, "news", NOW - timedelta(hours=1), NOW, NOW)
        _, reopened = manager.open_or_create(
            KEY, "news", NOW - timedelta(hours=1), NOW, NOW + timedelta(minutes=30)
        )
        assert reopened.expires_at_utc == created.expires_at_utc == NOW + timedelta(hours=1)

    def test_expired_cache_recreated_empty(self, tmp_path: Path) -> None:
        manager = CacheManager(tmp_path, ttl_seconds=60)
        cache, manifest = manager.open_or_create(KEY, "news", NOW - timedelta(hours=1), NOW, NOW)
        cache.append_page(manifest, "src", [message(1)])
        cache2, fresh = manager.open_or_create(
            KEY, "news", NOW - timedelta(hours=1), NOW, NOW + timedelta(minutes=5)
        )
        assert cache2.read_messages() == []
        assert fresh.created_at_utc == NOW + timedelta(minutes=5)

    def test_duplicate_and_torn_lines_tolerated(self, tmp_path: Path) -> None:
        """T16: повторно дописана сторінка й обірваний рядок не дають дублів/падіння."""
        manager = CacheManager(tmp_path)
        cache, manifest = manager.open_or_create(KEY, "news", NOW - timedelta(hours=1), NOW, NOW)
        cache.append_page(manifest, "src", [message(1), message(2)])
        cache.append_page(manifest, "src", [message(2), message(3)])
        with open(cache.messages_path, "a", encoding="utf-8") as handle:
            handle.write('{"source_id": "src", "mess')
        ids = [m.message_id for m in cache.read_messages()]
        assert sorted(ids) == [1, 2, 3]

    def test_new_version_kept_separately(self, tmp_path: Path) -> None:
        manager = CacheManager(tmp_path)
        cache, manifest = manager.open_or_create(KEY, "news", NOW - timedelta(hours=1), NOW, NOW)
        cache.append_page(manifest, "src", [message(1, "old", "v1"), message(1, "new", "v2")])
        assert len(cache.read_messages()) == 2

    def test_cleanup_skips_active(self, tmp_path: Path) -> None:
        manager = CacheManager(tmp_path, ttl_seconds=60)
        manager.open_or_create(KEY, "news", NOW - timedelta(hours=1), NOW, NOW)
        manager.open_or_create("b" * 64, "news", NOW - timedelta(hours=1), NOW, NOW)
        later = NOW + timedelta(hours=1)
        assert manager.cleanup_expired(later, active={KEY}) == ["b" * 64]
        assert [e.digest_key for e in manager.entries()] == [KEY]

    def test_manifest_has_no_texts(self, tmp_path: Path) -> None:
        manager = CacheManager(tmp_path)
        cache, manifest = manager.open_or_create(KEY, "news", NOW - timedelta(hours=1), NOW, NOW)
        cache.append_page(manifest, "src", [message(1, "СЕКРЕТНИЙ ТЕКСТ")])
        assert "СЕКРЕТНИЙ" not in cache.manifest_path.read_text(encoding="utf-8")

    def test_bad_digest_key_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            CacheManager(tmp_path).run("../escape")
