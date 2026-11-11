"""Збирання випуску для run: кеш → (AI) → план публікації. Спільне для worker і preview.

D-011: без налаштованого AI випуск не збирається для публікації (статус
`ai_not_configured`); чорновий режим без ШІ доступний лише в preview.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine

from newsdigest.ai.grouping import ChannelContext, PublishedItem, group_with_ai
from newsdigest.ai.provider import AiError, AiSettings, OpenAIProvider
from newsdigest.cache.run_cache import CacheManager
from newsdigest.config.effective import EffectiveChannel
from newsdigest.pipeline.build import BuiltDigest, Grouped, build_digest, draft_grouper
from newsdigest.pipeline.candidates import Candidate
from newsdigest.pipeline.render import RenderError
from newsdigest.scheduling import Window
from newsdigest.state.store import StateStore

log = logging.getLogger("newsdigest.digest")

MEMORY_HOURS = 24
"""Міжвипускна пам'ять моделі: власні пункти за останні 24 години (D-027)."""

ProviderFactory = Callable[[AiSettings], OpenAIProvider | None]
EngineFactory = Callable[[], Engine | None]


@dataclass(frozen=True, slots=True)
class DigestOutcome:
    status: str
    """`ready` — план збережено; `failed` — помилка AI/рендеру; `skipped` — AI не налаштовано."""
    error_code: str | None
    built: BuiltDigest | None
    used_ai: bool
    detail: str | None = None
    """Пояснення збою (зауваження до відповіді моделі тощо) — для CLI, не для логів."""


class DigestBuilder:
    def __init__(
        self,
        store: StateStore,
        cache: CacheManager,
        provider_factory: ProviderFactory,
        engine_factory: EngineFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._cache = cache
        self._provider_factory = provider_factory
        self._engine_factory = engine_factory
        self._clock = clock

    async def build(
        self,
        eff: EffectiveChannel,
        prompt: str,
        window: Window,
        digest_key: str,
        *,
        allow_draft: bool = False,
    ) -> DigestOutcome:
        channel = eff.channel
        run_cache = self._cache.run(digest_key)
        messages = run_cache.read_messages()
        published, memory = await asyncio.to_thread(self._load_memory, channel.id)

        provider = self._provider_factory(
            AiSettings(eff.ai.model, eff.ai.reasoning_effort, eff.ai.max_output_tokens)
        )
        if provider is None and not allow_draft:
            return DigestOutcome("skipped", "ai_not_configured", None, used_ai=False)

        if provider is not None:
            context = ChannelContext(
                topic=channel.description or channel.name,
                channel_prompt=prompt,
                timezone=eff.timezone,
                published_recent=tuple(memory),
            )

            async def grouper(candidates: Sequence[Candidate]) -> Grouped:
                result = await group_with_ai(candidates, provider, context)
                return Grouped(result.events, result.excluded, result.tokens)

        try:
            built = await build_digest(
                channel,
                window,
                eff.timezone,
                eff.max_chars_per_message,
                messages,
                runtime=self._store.load_runtime(),
                published=published,
                grouper=grouper if provider is not None else draft_grouper,
            )
        except AiError as exc:
            log.warning("ai_failed", extra={"channel": channel.id, "error_code": exc.code})
            return DigestOutcome("failed", f"ai_{exc.code}", None, used_ai=True, detail=str(exc))
        except RenderError as exc:
            log.warning("render_failed", extra={"channel": channel.id, "error_code": exc.code})
            return DigestOutcome(
                "failed", f"render_{exc.code}", None, used_ai=provider is not None, detail=str(exc)
            )

        built.plan.save(run_cache.directory)
        return DigestOutcome("ready", None, built, used_ai=provider is not None)

    def _load_memory(
        self, channel_id: str
    ) -> tuple[set[tuple[object, ...]] | None, list[PublishedItem]]:
        from sqlalchemy.exc import SQLAlchemyError

        from newsdigest.storage.published_repo import published_source_keys, recent_items

        engine = self._engine_factory()
        if engine is None:
            return None, []
        try:
            keys = published_source_keys(engine, channel_id)
            since = self._clock() - timedelta(hours=MEMORY_HOURS)
            items = [
                PublishedItem(title, summary, at)
                for title, summary, at in recent_items(engine, channel_id, since)
            ]
            return keys, items
        except SQLAlchemyError:
            log.warning("published_memory_unavailable", extra={"channel": channel_id})
            return None, []
        finally:
            engine.dispose()


def default_provider_factory() -> ProviderFactory:
    """Провайдер OpenAI, якщо задано `OPENAI_API_KEY(_FILE)`; інакше None."""
    from newsdigest.ai.provider import build_openai_provider, openai_api_key

    def factory(settings: AiSettings) -> OpenAIProvider | None:
        key = openai_api_key()
        return build_openai_provider(key, settings) if key else None

    return factory


def default_engine_factory() -> EngineFactory:
    """Engine PostgreSQL зі змінних `POSTGRES_*`; None, якщо БД не налаштована."""

    def factory() -> Engine | None:
        from newsdigest.settings import SettingsError, database_settings_from_env
        from newsdigest.storage.db import create_db_engine

        try:
            return create_db_engine(database_settings_from_env().url())
        except SettingsError:
            return None

    return factory
