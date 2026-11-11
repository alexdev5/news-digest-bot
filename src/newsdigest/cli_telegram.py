"""Команди CLI, що працюють із Telegram, станом і кешем (етап 2).

Команди з мережею (login, dialogs, doctor, scan, run) беруть process lock: вони не
можуть працювати паралельно з запущеним worker і не відкривають сесію двічі.
Жодна команда не друкує тексти дописів — лише ідентифікатори, статуси й лічильники.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from newsdigest.ai.trace import failure_detail
from newsdigest.cache.run_cache import CacheManager
from newsdigest.cli_common import DEFAULT_CONFIG, ConfigOption, ExitCode
from newsdigest.config.effective import effective_channel
from newsdigest.config.models import AppConfig, TargetChannel
from newsdigest.scheduling import Revision, Window, previous_boundary, window_for_boundary_rev
from newsdigest.settings import (
    Paths,
    SettingsError,
    TelegramSettings,
    paths_from_env,
    telegram_settings_from_env,
)
from newsdigest.state import LockBusyError, ProcessLock, StateCorruptError, StateStore
from newsdigest.state.store import ManualRequest
from newsdigest.telegram.rate_limit import RateLimiter, flood_wait_seconds
from newsdigest.telegram.scanner import HistoryScanner
from newsdigest.telegram.types import FloodWaitPause, TelegramError, TelegramGateway

HEALTHY_HEARTBEAT_SECONDS = 120


def utcnow() -> datetime:
    return datetime.now(UTC)


def _exit(code: int, message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


def _show_ai_failure(cache_dir: Path, digest_key: str, error_code: str | None) -> None:
    if not error_code or not error_code.startswith("ai_"):
        return
    directory = CacheManager(cache_dir).run(digest_key).directory
    detail = failure_detail(directory)
    if detail:
        typer.echo(f"  Причина AI-збою: {detail}")
        typer.echo(f"  Діагностика в тимчасовому кеші: {directory / 'ai'}")
    else:
        typer.echo("  Деталі AI-збою не збережені або кеш уже очищено.")


def telegram_env() -> tuple[Paths, TelegramSettings]:
    try:
        return paths_from_env(), telegram_settings_from_env()
    except SettingsError as exc:
        raise _exit(ExitCode.TELEGRAM_ERROR, f"Налаштування Telegram: {exc}") from exc


def gateway_factory(
    paths: Paths, settings: TelegramSettings
) -> Callable[[], AbstractAsyncContextManager[TelegramGateway]]:
    from newsdigest.telegram.telethon_gateway import connected_gateway

    return lambda: connected_gateway(paths, settings)


def locked(store: StateStore) -> ProcessLock:
    lock = ProcessLock(store.lock_path)
    try:
        lock.acquire()
    except LockBusyError as exc:
        raise _exit(ExitCode.LOCK_BUSY, f"{exc}") from exc
    return lock


def _select(config: AppConfig, channel_id: str) -> TargetChannel:
    try:
        return config.channel(channel_id)
    except KeyError:
        raise _exit(ExitCode.USAGE_ERROR, f"Невідомий цільовий канал: {channel_id}") from None


def resolve_window(cfg: AppConfig, target: TargetChannel, window_end: str | None) -> Window:
    """Вікно за межею `window_end` (ISO 8601) або остання минула межа розкладу."""
    trigger = effective_channel(cfg, target).trigger
    if window_end is None:
        boundary = previous_boundary(trigger, utcnow())
    else:
        try:
            boundary = datetime.fromisoformat(window_end)
        except ValueError as exc:
            raise _exit(ExitCode.USAGE_ERROR, "--window-end: потрібен ISO 8601") from exc
        if boundary.tzinfo is None:
            raise _exit(ExitCode.USAGE_ERROR, "--window-end має містити UTC offset")
    try:
        window = window_for_boundary_rev((Revision(trigger),), boundary, target.scan_start_at)
    except ValueError as exc:
        raise _exit(ExitCode.USAGE_ERROR, f"{exc}") from exc
    if window is None:
        raise _exit(ExitCode.USAGE_ERROR, "Межа раніше за scan_start_at — вікна немає.")
    return window


def load_published_keys(channel_id: str) -> set[tuple[object, ...]] | None:
    """Уже використані джерельні дописи з БД (D-012); None, якщо БД недоступна."""
    from sqlalchemy.exc import SQLAlchemyError

    from newsdigest.settings import database_settings_from_env
    from newsdigest.storage.db import create_db_engine
    from newsdigest.storage.published_repo import published_source_keys

    try:
        engine = create_db_engine(database_settings_from_env().url())
        try:
            return published_source_keys(engine, channel_id)
        finally:
            engine.dispose()
    except (SettingsError, SQLAlchemyError):
        return None


def build_worker(
    cfg: AppConfig,
    config_path: Path,
    paths: Paths,
    settings: TelegramSettings,
    store: StateStore,
) -> Any:
    """Worker з усіма залежностями: сканер, AI, доставка, БД (для run і run-now)."""
    from newsdigest.config import load_config
    from newsdigest.delivery import Delivery, sql_part_store_factory
    from newsdigest.digest_runner import (
        DigestBuilder,
        default_engine_factory,
        default_provider_factory,
    )
    from newsdigest.storage.published_repo import last_published_window_end
    from newsdigest.worker import RunExecutor, Worker

    cache = CacheManager(paths.cache_dir)
    limiter = RateLimiter(store, utcnow)
    factory = gateway_factory(paths, settings)
    engine_factory = default_engine_factory()

    def published_end(channel_id: str) -> datetime | None:
        engine = engine_factory()
        if engine is None:
            return None
        try:
            return last_published_window_end(engine, channel_id)
        finally:
            engine.dispose()

    return Worker(
        cfg,
        store,
        cache,
        RunExecutor(store, cache, factory, limiter, utcnow),
        utcnow,
        builder=DigestBuilder(store, cache, default_provider_factory(), engine_factory, utcnow),
        prompts=load_config(config_path).prompts,
        delivery=Delivery(
            store, cache, factory, limiter, sql_part_store_factory(engine_factory), utcnow
        ),
        published_end=published_end,
    )


def register(app: typer.Typer, load: Callable[[Path], AppConfig]) -> None:
    @app.command("login")
    def login(
        qr: Annotated[
            bool, typer.Option("--qr", help="Вхід через QR-код замість коду з Telegram.")
        ] = False,
    ) -> None:
        """Інтерактивний вхід у Telegram-акаунт (worker має бути зупинений)."""
        from newsdigest.telegram.telethon_gateway import interactive_login, interactive_qr_login

        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        lock = locked(store)
        try:
            flow = interactive_qr_login if qr else interactive_login
            account = asyncio.run(flow(paths, settings))
        except TelegramError as exc:
            raise _exit(ExitCode.TELEGRAM_ERROR, f"Вхід не виконано: {exc}") from exc
        except Exception as exc:
            seconds = flood_wait_seconds(exc)
            if seconds is not None:
                raise _exit(
                    ExitCode.TELEGRAM_ERROR,
                    f"Telegram тимчасово обмежив запити коду: зачекайте {seconds} с.",
                ) from exc
            if type(exc).__module__.startswith("telethon"):
                raise _exit(ExitCode.TELEGRAM_ERROR, f"Telegram: {type(exc).__name__}") from exc
            raise
        finally:
            lock.release()
        if account.is_bot:
            raise _exit(ExitCode.TELEGRAM_ERROR, "Це бот-акаунт; потрібен звичайний акаунт.")
        runtime = store.load_runtime()
        if runtime.account_id not in (None, account.account_id):
            raise _exit(
                ExitCode.TELEGRAM_ERROR,
                f"Сесія належить акаунту {account.account_id}, а стан — {runtime.account_id}.",
            )
        runtime.account_id = account.account_id
        store.save_runtime(runtime)
        typer.echo(f"OK: вхід виконано, акаунт {account.account_id} ({account.display_name})")

    @app.command("dialogs")
    def dialogs() -> None:
        """Канали, доступні акаунту: ID для конфігу й право публікації. Історію не читає."""
        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        lock = locked(store)

        async def fetch() -> None:
            async with gateway_factory(paths, settings)() as gateway:
                for dialog in await gateway.dialogs():
                    kind = "канал" if dialog.is_broadcast else "група"
                    post = "може публікувати" if dialog.can_post else ""
                    username = f"@{dialog.username}" if dialog.username else "-"
                    typer.echo(f"{dialog.peer_id}\t{kind}\t{username}\t{dialog.title}\t{post}")

        try:
            asyncio.run(fetch())
        except TelegramError as exc:
            raise _exit(ExitCode.TELEGRAM_ERROR, f"Telegram: {exc}") from exc
        finally:
            lock.release()

    @app.command("doctor")
    def doctor(config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Перевірка сесії, акаунта, джерел і права публікації. Історію не читає."""
        cfg = load(config)
        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        lock = locked(store)
        problems: list[str] = []

        async def check() -> None:
            limiter = RateLimiter(store, utcnow)
            async with gateway_factory(paths, settings)() as gateway:
                account = await limiter.call(gateway.account)
                typer.echo(f"акаунт: {account.account_id} ({account.display_name})")
                if account.is_bot:
                    problems.append("бот-акаунт замість звичайного")
                known = store.load_runtime().account_id
                if known is not None and known != account.account_id:
                    problems.append(f"стан належить акаунту {known}")
                scanner = HistoryScanner(gateway, limiter, store, utcnow)
                for channel in cfg.target_channels:
                    if not channel.enabled:
                        continue
                    try:
                        target = await scanner.resolve_pinned(channel.telegram)
                        ok = target.is_broadcast and target.can_post
                        mark = "OK" if ok else "FAIL"
                        typer.echo(f"[{mark}] {channel.id}: ціль {target.title} ({target.peer_id})")
                        if not ok:
                            problems.append(f"{channel.id}: немає права публікації в цілі")
                    except TelegramError as exc:
                        typer.echo(f"[FAIL] {channel.id}: ціль — {exc.code}")
                        problems.append(f"{channel.id}: ціль {exc.code}")
                    for source in channel.source_channels:
                        if not source.enabled:
                            continue
                        try:
                            peer = await scanner.resolve_pinned(source.ref)
                            mark = "OK" if peer.is_broadcast else "FAIL"
                            typer.echo(f"[{mark}]   {source.id}: {peer.title} ({peer.peer_id})")
                            if not peer.is_broadcast:
                                problems.append(f"{source.id}: не канал")
                        except TelegramError as exc:
                            typer.echo(f"[FAIL]   {source.id}: {exc.code}")
                            problems.append(f"{source.id}: {exc.code}")

        try:
            asyncio.run(check())
        except (TelegramError, FloodWaitPause) as exc:
            raise _exit(ExitCode.TELEGRAM_ERROR, f"Telegram: {exc}") from exc
        finally:
            lock.release()
        if problems:
            raise _exit(ExitCode.TELEGRAM_ERROR, "Проблеми:\n  - " + "\n  - ".join(problems))
        typer.echo("OK: усе готово до сканування")

    @app.command("scan")
    def scan(
        config: ConfigOption = DEFAULT_CONFIG,
        channel: Annotated[str, typer.Option(help="id цільового каналу.")] = "",
        window_end: Annotated[
            str | None,
            typer.Option(help="Межа вікна ISO 8601 з offset; за замовчуванням — остання минула."),
        ] = None,
        keep: Annotated[bool, typer.Option(help="Залишити кеш до TTL.")] = False,
    ) -> None:
        """Ручне сканування одного вікна (без публікації). Друкує лише лічильники."""
        from newsdigest.worker import RunExecutor

        cfg = load(config)
        if not channel:
            raise _exit(ExitCode.USAGE_ERROR, "Вкажіть --channel <id>.")
        target = _select(cfg, channel)
        window = resolve_window(cfg, target, window_end)

        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        cache = CacheManager(paths.cache_dir).inspection()
        lock = locked(store)
        try:
            executor = RunExecutor(
                store, cache, gateway_factory(paths, settings), RateLimiter(store, utcnow), utcnow
            )
            outcome = asyncio.run(executor.scan(target, window))
            if not keep:
                executor.discard(outcome)
        finally:
            lock.release()
        tz = effective_channel(cfg, target).timezone
        typer.echo(
            f"{target.id}: {window.start_utc.astimezone(tz):%Y-%m-%d %H:%M} → "
            f"{window.end_utc.astimezone(tz):%Y-%m-%d %H:%M} ({tz.key}), статус {outcome.status}"
            + (f", {outcome.error_code}" if outcome.error_code else "")
        )
        if outcome.summary is not None:
            for source_id, status in outcome.summary.statuses.items():
                typer.echo(f"  {source_id}: {status}")
            typer.echo(f"  дописів у вікні: {outcome.summary.in_window}")
            if outcome.summary.edited_after_cutoff:
                typer.echo(
                    "  з них відредаговано після межі (взято поточну версію): "
                    f"{outcome.summary.edited_after_cutoff}"
                )
            for reason, count in outcome.summary.excluded.items():
                typer.echo(f"  виключено ({reason}): {count}")
        if keep:
            typer.echo(f"  кеш: {cache.run(outcome.digest_key).directory}")
        if outcome.status == "failed":
            raise typer.Exit(ExitCode.TELEGRAM_ERROR)

    @app.command("preview")
    def preview(
        config: ConfigOption = DEFAULT_CONFIG,
        channel: Annotated[str, typer.Option(help="id цільового каналу.")] = "",
        window_end: Annotated[
            str | None,
            typer.Option(help="Межа вікна ISO 8601 з offset; за замовчуванням — остання минула."),
        ] = None,
        no_ai: Annotated[
            bool, typer.Option("--no-ai", help="Без OpenAI: чорновий текст, кожен допис окремо.")
        ] = False,
        keep: Annotated[bool, typer.Option(help="Залишити кеш і plan.json до TTL.")] = False,
    ) -> None:
        """Сканування + збирання випуску без публікації: показує готові повідомлення.

        З `OPENAI_API_KEY` — справжній AI-прохід (платний запит); без ключа або з
        `--no-ai` — чорновий текст без злиття (такий текст ніколи не публікується).
        """
        from newsdigest.config import load_config
        from newsdigest.digest_runner import (
            DigestBuilder,
            default_engine_factory,
            default_provider_factory,
        )
        from newsdigest.worker import RunExecutor

        cfg = load(config)
        if not channel:
            raise _exit(ExitCode.USAGE_ERROR, "Вкажіть --channel <id>.")
        target = _select(cfg, channel)
        window = resolve_window(cfg, target, window_end)
        eff = effective_channel(cfg, target)
        prompt = load_config(config).prompts.get(target.id, "")
        provider_factory = (lambda _settings: None) if no_ai else default_provider_factory()

        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        cache = CacheManager(paths.cache_dir).inspection()
        lock = locked(store)
        try:
            executor = RunExecutor(
                store, cache, gateway_factory(paths, settings), RateLimiter(store, utcnow), utcnow
            )
            builder = DigestBuilder(
                store, cache, provider_factory, default_engine_factory(), utcnow
            )

            async def scan_and_build() -> tuple[Any, Any]:
                outcome = await executor.scan(target, window)
                digest = await builder.build(
                    eff, prompt, window, outcome.digest_key, allow_draft=True
                )
                return outcome, digest

            outcome, digest = asyncio.run(scan_and_build())
            if not keep:
                executor.discard(outcome)
        finally:
            lock.release()

        tz = eff.timezone
        mode = f"AI {eff.ai.model}" if digest.used_ai else "БЕЗ ШІ (чорновий текст)"
        typer.echo(
            f"ПОПЕРЕДНІЙ ПЕРЕГЛЯД, {mode} — {target.id}: "
            f"{window.start_utc.astimezone(tz):%d.%m %H:%M} → "
            f"{window.end_utc.astimezone(tz):%d.%m %H:%M} ({tz.key})"
        )
        if outcome.summary is not None:
            statuses = ", ".join(f"{k}={v}" for k, v in outcome.summary.statuses.items())
            typer.echo(f"  джерела: {statuses}")
        built = digest.built
        if built is None:
            detail = f"\n  {digest.detail}" if digest.detail else ""
            raise _exit(ExitCode.TELEGRAM_ERROR, f"Випуск не зібрано: {digest.error_code}{detail}")
        typer.echo(
            f"  кандидатів: {len(built.candidates.candidates)}, подій: {len(built.events)}, "
            f"частин: {len(built.plan.parts)}"
        )
        if built.tokens:
            typer.echo(
                f"  токени: вхід {built.tokens['input']}, вихід {built.tokens['output']}, "
                f"запитів {built.tokens['requests']}"
            )
        for reason, count in sorted(built.candidates.excluded.items()):
            typer.echo(f"  відкинуто ({reason}): {count}")
        if not built.plan.parts:
            typer.echo("Новин немає — публікації не було б (D-012).")
        for part in built.plan.parts:
            typer.echo("")
            typer.echo(f"===== {part.index}/{part.count}, {len(part.text)} симв. =====")
            typer.echo(part.text)
            typer.echo("----- посилання -----")
            for entity in part.entities:
                if entity.get("type") == "text_url":
                    typer.echo(f"  {entity['url']}")
        if keep:
            typer.echo("")
            typer.echo(f"план: {cache.run(outcome.digest_key).directory / 'plan.json'}")

    @app.command("ai-check")
    def ai_check(config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Перевірка ключа OpenAI й доступу до моделі каналів (без платної генерації)."""
        from newsdigest.ai.provider import openai_api_key

        cfg = load(config)
        key = openai_api_key()
        if not key:
            raise _exit(ExitCode.AI_ERROR, "Не задано OPENAI_API_KEY (або OPENAI_API_KEY_FILE).")
        models = sorted(
            {effective_channel(cfg, c).ai.model for c in cfg.target_channels if c.enabled}
        )

        async def check() -> list[str]:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=key, timeout=30, max_retries=1)
            problems: list[str] = []
            for model in models:
                try:
                    await client.models.retrieve(model)
                    typer.echo(f"[OK] модель {model} доступна")
                except Exception as exc:
                    typer.echo(f"[FAIL] модель {model}: {type(exc).__name__}")
                    problems.append(model)
            await client.close()
            return problems

        if asyncio.run(check()):
            raise typer.Exit(ExitCode.AI_ERROR)

    @app.command("reconcile")
    def reconcile(
        config: ConfigOption = DEFAULT_CONFIG,
        channel: Annotated[str, typer.Option(help="id цільового каналу.")] = "",
    ) -> None:
        """Звірити невизначені відправки й продовжити незавершені серії каналу.

        Не сканує джерела й не генерує новий текст: лише збережені плани з кешу.
        Worker має бути зупинений (process lock).
        """
        from newsdigest.delivery import Delivery, sql_part_store_factory
        from newsdigest.digest_runner import default_engine_factory
        from newsdigest.worker import RunExecutor, Worker

        cfg = load(config)
        if not channel:
            raise _exit(ExitCode.USAGE_ERROR, "Вкажіть --channel <id>.")
        target = _select(cfg, channel)
        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        cache = CacheManager(paths.cache_dir)
        lock = locked(store)
        try:
            limiter = RateLimiter(store, utcnow)
            factory = gateway_factory(paths, settings)
            executor = RunExecutor(store, cache, factory, limiter, utcnow)
            parts_factory = sql_part_store_factory(default_engine_factory())
            delivery = Delivery(store, cache, factory, limiter, parts_factory, utcnow)
            worker = Worker(cfg, store, cache, executor, utcnow, delivery=delivery)
            if target.id not in store.load_runtime().channels:
                raise _exit(ExitCode.USAGE_ERROR, "Канал ще не активований worker-ом.")
            asyncio.run(worker.publish_pending(target, utcnow(), force=True))
        finally:
            lock.release()
        state = store.load_runtime().channels[target.id]
        for record in state.runs[-5:]:
            typer.echo(
                f"  {record.window_end_utc:%Y-%m-%d %H:%M}Z {record.status}"
                + (f" {record.error_code}" if record.error_code else "")
            )
        unresolved = store.load_guards().unresolved(target.id)
        if unresolved:
            typer.echo(f"Невирішених частин: {len(unresolved)}")
            for guard in unresolved:
                typer.echo(
                    f"  {guard.part_key[:12]}…:{guard.part_index}/{guard.part_count} {guard.phase}"
                    + (f" {guard.error_code}" if guard.error_code else "")
                )
            raise typer.Exit(ExitCode.TELEGRAM_ERROR)
        typer.echo("OK: невирішених відправок немає")

    @app.command("run")
    def run(config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Worker: чекає планових меж і сканує вікна. Між межами джерела не читає."""
        from newsdigest.logging_setup import setup_logging

        setup_logging()
        cfg = load(config)
        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        lock = locked(store)

        async def main() -> None:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with suppress(NotImplementedError):  # Windows: без сигналів
                    loop.add_signal_handler(sig, stop.set)
            worker = build_worker(cfg, config, paths, settings, store)
            await worker.run_forever(stop)

        try:
            asyncio.run(main())
        except StateCorruptError as exc:
            raise _exit(ExitCode.STATE_ERROR, f"Стан пошкоджений: {exc}") from exc
        finally:
            lock.release()

    @app.command("run-now")
    def run_now(
        config: ConfigOption = DEFAULT_CONFIG,
        channel: Annotated[str, typer.Option(help="id цільового каналу.")] = "",
    ) -> None:
        """ПУБЛІКАЦІЯ останнього завершеного інтервалу schedule (D-031).
        Уже покрита частина вікна пропускається. Запущений worker приймає запит."""
        from newsdigest.logging_setup import setup_logging

        cfg = load(config)
        if not channel:
            raise _exit(ExitCode.USAGE_ERROR, "Вкажіть --channel <id>.")
        target = _select(cfg, channel)
        if not target.enabled:
            raise _exit(ExitCode.USAGE_ERROR, f"Канал {target.id} вимкнений у конфігу.")
        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        lock = ProcessLock(store.lock_path)
        try:
            lock.acquire()
        except LockBusyError:
            store.request_manual_run(target.id, utcnow())
            typer.echo(
                "Worker запущено — запит передано йому, виконає протягом ~30 с. "
                "Результат: digest status."
            )
            return

        setup_logging("WARNING")
        try:
            worker = build_worker(cfg, config, paths, settings, store)
            worker.initialize()
            record = asyncio.run(worker.run_now(target))
        finally:
            lock.release()
        if record is None:
            typer.echo("Немає нового завершеного інтервалу розкладу.")
            return
        tz = effective_channel(cfg, target).timezone
        typer.echo(
            f"{target.id}: {record.window_start_utc.astimezone(tz):%d.%m %H:%M} → "
            f"{record.window_end_utc.astimezone(tz):%d.%m %H:%M} ({tz.key}), "
            f"статус {record.status}" + (f", {record.error_code}" if record.error_code else "")
        )
        if record.events is not None:
            typer.echo(f"  подій: {record.events}, повідомлень: {record.parts}")
        if record.status not in ("published",):
            _show_ai_failure(paths.cache_dir, record.digest_key, record.error_code)
            raise typer.Exit(ExitCode.TELEGRAM_ERROR)

    @app.command("run-window")
    def run_window(
        start: Annotated[str, typer.Option("--from", help="Початок включно, ISO 8601 з offset.")],
        end: Annotated[str, typer.Option("--to", help="Кінець не включно, ISO 8601 з offset.")],
        channel: Annotated[str, typer.Option(help="id цільового каналу.")],
        config: ConfigOption = DEFAULT_CONFIG,
        ignore_published: Annotated[
            bool,
            typer.Option("--ignore-published", help="Повторно включати вже опубліковані новини."),
        ] = False,
    ) -> None:
        """Сканування → AI → ПУБЛІКАЦІЯ довільного минулого періоду [--from, --to).

        За замовчуванням пропускає опубліковане. --ignore-published вимикає перевірку
        використаних ID і пам'ять AI, але не захист доставки від повторного send.
        """
        from newsdigest.logging_setup import setup_logging
        from newsdigest.worker import Worker

        cfg = load(config)
        target = _select(cfg, channel)
        try:
            window = Window(datetime.fromisoformat(start), datetime.fromisoformat(end))
            Worker.validate_manual_window(target, window, utcnow())
        except ValueError as exc:
            raise _exit(
                ExitCode.USAGE_ERROR,
                f"Некоректний період: {exc}. Формат: 2026-11-11T15:00:00+02:00.",
            ) from exc
        request = ManualRequest(
            channel_id=target.id,
            requested_at_utc=utcnow(),
            window_start_utc=window.start_utc,
            window_end_utc=window.end_utc,
            ignore_published=ignore_published,
        )
        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        lock = ProcessLock(store.lock_path)
        try:
            lock.acquire()
        except LockBusyError:
            store.queue_manual_request(request)
            typer.echo(
                f"Запит {request.request_id} передано worker: {start} → {end}, "
                f"ignore_published={ignore_published}. Результат: digest status --details."
            )
            return

        setup_logging("WARNING")
        try:
            worker = build_worker(cfg, config, paths, settings, store)
            worker.initialize()
            record = asyncio.run(
                worker.run_window(
                    target,
                    window,
                    ignore_published=ignore_published,
                    request_id=request.request_id,
                )
            )
        finally:
            lock.release()
        tz = effective_channel(cfg, target).timezone
        typer.echo(
            f"Запит {request.request_id}: "
            f"{record.window_start_utc.astimezone(tz):%Y-%m-%d %H:%M} → "
            f"{record.window_end_utc.astimezone(tz):%Y-%m-%d %H:%M} ({tz.key}), "
            f"{record.status}" + (f" {record.error_code}" if record.error_code else "")
        )
        if record.events is not None:
            typer.echo(f"  подій: {record.events}, повідомлень: {record.parts}")
        if record.status != "published":
            _show_ai_failure(paths.cache_dir, record.digest_key, record.error_code)
            raise typer.Exit(ExitCode.TELEGRAM_ERROR)

    @app.command("status")
    def status(
        config: ConfigOption = DEFAULT_CONFIG,
        details: Annotated[
            bool, typer.Option("--details", help="ID відкинутих дописів і причини.")
        ] = False,
    ) -> None:
        """Стан каналів, останні runs, пауза FloodWait, heartbeat. Мережі не використовує."""
        from newsdigest.scheduling import next_boundary_rev

        cfg = load(config)
        store = StateStore(paths_from_env().state_dir)
        try:
            runtime = store.load_runtime()
            limits = store.load_rate_limits()
            heartbeat = store.load_heartbeat()
        except StateCorruptError as exc:
            raise _exit(ExitCode.STATE_ERROR, f"Стан пошкоджений: {exc}") from exc
        now = utcnow()
        typer.echo(f"акаунт: {runtime.account_id or 'не виконано login'}")
        if heartbeat is not None:
            age = int((now - heartbeat.at_utc).total_seconds())
            typer.echo(f"worker heartbeat: {age} с тому ({heartbeat.phase})")
        else:
            typer.echo("worker heartbeat: немає")
        unresolved = store.load_guards().unresolved()
        if unresolved:
            typer.echo(f"невирішених частин доставки: {len(unresolved)} (див. digest reconcile)")
        from newsdigest.worker import owner_action_needed

        for blocked in owner_action_needed(runtime):
            typer.echo(f"ПОТРІБНА ДІЯ ВЛАСНИКА — доставка зупинена: {blocked}")
        if limits.pause_until_utc and limits.pause_until_utc > now:
            typer.echo(f"FloodWait-пауза до {limits.pause_until_utc.isoformat()}")
        for channel in cfg.target_channels:
            state = runtime.channels.get(channel.id)
            if state is None:
                typer.echo(f"{channel.id}: ще не активований worker-ом")
                continue
            tz = effective_channel(cfg, channel).timezone
            nxt = next_boundary_rev(state.revision_tuple(), state.last_materialized_utc)
            flag = " (вимкнено)" if state.paused else ""
            typer.echo(
                f"{channel.id}{flag}: наступна межа {nxt.astimezone(tz):%Y-%m-%d %H:%M %Z}, "
                f"пропущено {len(state.skipped_utc)}"
            )
            for record in state.runs[-5:]:
                coverage = ", ".join(f"{k}={v.status}" for k, v in record.coverage.items())
                typer.echo(
                    f"  {record.window_start_utc.astimezone(tz):%Y-%m-%d %H:%M} → "
                    f"{record.window_end_utc.astimezone(tz):%Y-%m-%d %H:%M} "
                    f"{record.status}{' ' + record.error_code if record.error_code else ''}"
                    f"{' [' + coverage + ']' if coverage else ''}"
                )
                if record.manual_request_id is not None:
                    typer.echo(
                        f"    запит {record.manual_request_id}, "
                        f"ignore_published={record.ignore_published}"
                    )
                if details:
                    _show_ai_failure(
                        paths_from_env().cache_dir, record.digest_key, record.error_code
                    )
                    for sid, source in record.coverage.items():
                        typer.echo(
                            f"    {sid}: {source.in_window} у вікні, {source.status}"
                            + (f" ({source.error_code})" if source.error_code else "")
                        )
                    for excluded in record.excluded_sources:
                        typer.echo(
                            f"    {excluded['source_id']} "
                            f"{excluded['peer_id']}/{excluded['message_id']}: {excluded['reason']}"
                        )

    @app.command("cache-status")
    def cache_status() -> None:
        """Обсяг і TTL тимчасового кешу (без текстів)."""
        cache = CacheManager(paths_from_env().cache_dir)
        entries = cache.entries() + cache.inspection().entries()
        total = sum(e.size_bytes for e in entries)
        typer.echo(f"runs у кеші: {len(entries)}, обсяг {total} байт, ліміт {cache.max_bytes}")
        for entry in entries:
            typer.echo(
                f"  {entry.digest_key[:12]} {entry.channel_id} до "
                f"{entry.expires_at_utc.isoformat()} ({entry.size_bytes} байт)"
            )

    @app.command("cache-clean")
    def cache_clean() -> None:
        """Видалити прострочений кеш (unresolved guards і стан не зачіпаються)."""
        paths = paths_from_env()
        store = StateStore(paths.state_dir)
        lock = locked(store)
        try:
            cache = CacheManager(paths.cache_dir)
            removed = cache.cleanup_expired(utcnow()) + cache.inspection().cleanup_expired(utcnow())
        finally:
            lock.release()
        typer.echo(f"видалено прострочених runs: {len(removed)}")

    @app.command("healthcheck")
    def healthcheck() -> None:
        """Локальна перевірка heartbeat worker; мережі й джерел не торкається."""
        store = StateStore(paths_from_env().state_dir)
        try:
            heartbeat = store.load_heartbeat()
        except StateCorruptError as exc:
            raise _exit(ExitCode.UNHEALTHY, f"heartbeat пошкоджений: {exc}") from exc
        if heartbeat is None:
            raise _exit(ExitCode.UNHEALTHY, "heartbeat відсутній")
        age = (utcnow() - heartbeat.at_utc).total_seconds()
        if age > HEALTHY_HEARTBEAT_SECONDS:
            raise _exit(ExitCode.UNHEALTHY, f"heartbeat застарів: {int(age)} с")
        from newsdigest.worker import owner_action_needed

        try:
            blocked = owner_action_needed(store.load_runtime())
        except StateCorruptError as exc:
            raise _exit(ExitCode.UNHEALTHY, f"стан пошкоджений: {exc}") from exc
        if blocked:
            raise _exit(
                ExitCode.UNHEALTHY,
                "доставка зупинена, потрібна дія власника: " + "; ".join(blocked),
            )
        typer.echo(f"OK: heartbeat {int(age)} с тому")
