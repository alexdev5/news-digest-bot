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
from typing import Annotated

import typer

from newsdigest.cache.run_cache import CacheManager
from newsdigest.cli_common import DEFAULT_CONFIG, ConfigOption, ExitCode
from newsdigest.config.effective import effective_channel
from newsdigest.config.models import AppConfig, TargetChannel
from newsdigest.scheduling import Revision, previous_boundary, window_for_boundary_rev
from newsdigest.settings import (
    Paths,
    SettingsError,
    TelegramSettings,
    paths_from_env,
    telegram_settings_from_env,
)
from newsdigest.state import LockBusyError, ProcessLock, StateCorruptError, StateStore
from newsdigest.telegram.rate_limit import RateLimiter, flood_wait_seconds
from newsdigest.telegram.scanner import HistoryScanner
from newsdigest.telegram.types import FloodWaitPause, TelegramError, TelegramGateway

HEALTHY_HEARTBEAT_SECONDS = 120


def utcnow() -> datetime:
    return datetime.now(UTC)


def _exit(code: int, message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


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
        trigger = effective_channel(cfg, target).trigger
        revisions = (Revision(trigger),)
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
            window = window_for_boundary_rev(revisions, boundary, target.scan_start_at)
        except ValueError as exc:
            raise _exit(ExitCode.USAGE_ERROR, f"{exc}") from exc
        if window is None:
            raise _exit(ExitCode.USAGE_ERROR, "Межа раніше за scan_start_at — вікна немає.")

        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        cache = CacheManager(paths.cache_dir)
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

    @app.command("run")
    def run(config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Worker: чекає планових меж і сканує вікна. Між межами джерела не читає."""
        from newsdigest.logging_setup import setup_logging
        from newsdigest.worker import RunExecutor, Worker

        setup_logging()
        cfg = load(config)
        paths, settings = telegram_env()
        store = StateStore(paths.state_dir)
        cache = CacheManager(paths.cache_dir)
        lock = locked(store)

        async def main() -> None:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with suppress(NotImplementedError):  # Windows: без сигналів
                    loop.add_signal_handler(sig, stop.set)
            limiter = RateLimiter(store, utcnow)
            executor = RunExecutor(store, cache, gateway_factory(paths, settings), limiter, utcnow)
            await Worker(cfg, store, cache, executor, utcnow).run_forever(stop)

        try:
            asyncio.run(main())
        except StateCorruptError as exc:
            raise _exit(ExitCode.STATE_ERROR, f"Стан пошкоджений: {exc}") from exc
        finally:
            lock.release()

    @app.command("status")
    def status(config: ConfigOption = DEFAULT_CONFIG) -> None:
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
                    f"  {record.window_end_utc.astimezone(tz):%Y-%m-%d %H:%M} "
                    f"{record.status}{' ' + record.error_code if record.error_code else ''}"
                    f"{' [' + coverage + ']' if coverage else ''}"
                )

    @app.command("cache-status")
    def cache_status() -> None:
        """Обсяг і TTL тимчасового кешу (без текстів)."""
        cache = CacheManager(paths_from_env().cache_dir)
        entries = cache.entries()
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
            removed = CacheManager(paths.cache_dir).cleanup_expired(utcnow())
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
        typer.echo(f"OK: heartbeat {int(age)} с тому")
