# NewsDigest

Тематичні дайджести з Telegram-каналів і сайтів: планове читання джерел звичайним
акаунтом, AI-узагальнення (`gpt-6.1-terra`), публікація всіх унікальних новин у ваші
канали в хронологічному порядку. Кожен цільовий канал описується в JSON-конфігу
(`target_channels`). Довгий випуск виходить пронумерованими частинами.

**Стан на 2026-11-11:** реалізовані Telegram-сканер, планувальник, AI-групування,
багаточастинна публікація та відновлення доставки. Сайти й повна backup/restore-звірка
ще не готові. `main.py` — шаблон PyCharm, не точка запуску.

`run-now` обробляє останній завершений інтервал розкладу: за меж 08:00/15:00/21:00
запуск о 21:31 бере 15:00–21:00. У дайджест входять усі унікальні новини джерел,
включно з міжнародними. Деталі виправлень — [у звіті](docs/bugfix-2026-11-11.md).

Для довільного ручного періоду: `run-window --channel <id> --from <ISO> --to <ISO>`.
`--ignore-published` дозволяє повторно включати вже опубліковані новини.
[Приклади локального й серверного запуску](docs/manual-window.md).

Команди запускаються в Docker через обгортку (Windows — `.\run.cmd`, Linux/macOS — `./run.sh`):

```powershell
.\run.cmd start        # запустити PostgreSQL
.\run.cmd build        # зібрати образ
.\run.cmd migrate      # застосувати міграції БД
.\run.cmd check        # ruff + mypy + pytest
.\run.cmd test         # лише pytest (аргументи передаються pytest)
.\run.cmd validate     # перевірити config/config.json або приклад
.\run.cmd schedule     # найближчі вікна всіх цільових каналів
.\run.cmd help         # усі команди
```

Без Docker: `uv sync`, скопіювати `.env.example` у `.env`, далі `uv run --env-file .env pytest`,
`uv run --env-file .env digest …` (БД — `.\run.cmd start`, порт 5433).

- [Поточний контекст і рішення](docs/context.md).
- [Документація та порядок читання](docs/README.md).
- [Поточний функціональний контракт](docs/specification.md).
- [Огляд ТЗ: прогалини й суперечності](docs/review.md).
- [План реалізації](docs/implementation-plan.md).
- [Розгортання на сервері](docs/deployment.md).
- [Правила для асистентів](AGENTS.md).

Оригінал ТЗ v4 збережено без змін у [docs/specs/newsdigest-v4.txt](docs/specs/newsdigest-v4.txt).
