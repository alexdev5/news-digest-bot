# Технологічний стек і готовність до реалізації

Дата: 2026-11-11. Основні технології задані в ТЗ v4; тут доповнено
інструментальну частину як технічні defaults. Пакети ще не встановлені,
lockfile і Docker image ще не створені. Версії нижче — цільові гілки/контракти,
а не твердження про вже перевірену сумісність конкретних patch releases.

## Повний вибір для MVP

| Частина | Технологія | Призначення |
| --- | --- | --- |
| Сервер виконання | Linux, Docker Engine, Docker Compose v2 | Один сервіс/worker, окремі data/cache/state/session |
| Мова | Python 3.13 | Задана в початковому ТЗ версія |
| Асинхронний runtime | asyncio | Мережеві виклики, черги й планувальник без блокування event loop |
| Telegram | Telethon 1.x, MTProto | Вхід звичайним акаунтом, історія каналів, send і reconciliation |
| AI | Офіційний `openai` Python SDK, `AsyncOpenAI`, Responses API | `gpt-5.6-sol`, structured events/claims та короткий огляд |
| Схеми | Pydantic 2.x, JSON Schema | Config/state/cache/API-result validation, unknown fields заборонені |
| Конфігурація | PyYAML 6.x, safe loader із перевіркою duplicate keys/limits | Читання YAML schema 4 без виконання довільного коду |
| Власні публікації | SQLite через стандартний `sqlite3` | Confirmed-only таблиця, транзакції, WAL і backup API |
| Тимчасові дані | Файли JSON/JSONL, стандартні pathlib/json/tempfile/os | Run snapshots, source texts, drafts, cursors, atomic state writes |
| Розклади | Власний невеликий модуль на asyncio + zoneinfo; tzdata в образі | Точні fixed windows, DST, revisions/cutover і незалежні профілі |
| CLI | Typer | Команди `digest`, typed options, help та коди завершення |
| Контроль worker | asyncio Unix domain socket | Локальна online-адміністрація без другого Telegram-клієнта |
| Логи | Стандартний logging + JSON formatter | Структуровані технічні записи без новинних текстів/секретів |
| Залежності/оточення | uv, `pyproject.toml`, `uv.lock` | Відтворюване оточення розробки та Docker build |
| Тести | pytest + pytest-asyncio | Unit, async integration, mock I/O, crash/recovery scenarios |
| Форматування/lint | Ruff | Єдині style/import/lint перевірки |
| Типи | mypy | Статична перевірка type hints і контрактів модулів |

Синхронні файлові/SQLite операції виконуються поза критичним event loop.
Для SQLite потрібна явна модель власника connection/транзакцій, не довільне
використання одного connection у різних threads. Це деталізація реалізації,
а не потреба в окремому сервері БД або ORM.

Власний scheduler обраний через специфічні вікна/cutover і малу кількість профілів.
Він очікує час, а не сканує Telegram у фоні. Wall clock визначає планову межу,
monotonic clock — timeout; clock jumps і зміни timezone перевіряються тестами.

Локальна розробка можлива з Windows; робочий worker із Unix socket/locks
перевіряється в Linux-контейнері. Нативний Windows production worker не є метою MVP.

## Що ще потрібно технічно зробити

Це звичайні результати реалізації, а не питання, які власник має проєктувати
замість розробника:

1. Зафіксувати сумісні точні версії в `uv.lock` і Python image digest.
2. Описати Pydantic/JSON schemas: config 4, AI stages, cache, state, part manifest,
   confirmed publication; завершити SQLite DDL та CLI error/exit-code контракт.
3. Написати prompts із визначенням події, розвитку й суперечності, прикладами
   різних формулювань та обов'язковими source refs. Налаштувати batching/token budget.
4. Реалізувати всі модулі й guards серії; перевірити Telethon SDK settings,
   raw send/random ID, timeout/retry/FloodWait та scheduled-only мережеву поведінку.
5. Додати Dockerfile/Compose, локальні fixtures, CI-команди та fault-injection.
6. Виміряти latency, coverage, якість і витрати, завершити operations runbook.

Окремий вибір CI-провайдера залежить від майбутнього remote repository;
pipeline перевірок не повинен залежати від конкретного hosting vendor.

## Чого бракує від власника/середовища до live-запуску

| Потрібно | Навіщо |
| --- | --- |
| Джерела для кожної теми, їхній порядок і destination | Реальні source lists та правильна attribution/публікація |
| Остаточні розклади | У ТЗ є можливості й шаблони, але приклади не є готовим production-config |
| Контрольований Telegram-акаунт, API credentials та права | Первинна авторизація й доступи; секрети вводяться локально |
| OpenAI project key, доступ до моделі та API-кредити | Реальні AI-виклики; баланс/активацію ще не перевірено |
| Budget caps на випуск/день/період | Контроль витрат незалежно від наявності Pro |
| Реєстр дозволів джерел | Чинні підстави читання, AI-обробки та публікації |
| Політика збоїв/неповноти | Неповний дайджест із позначкою, статус чи очікування retry; O-006 |
| Невелика дозволена/синтетична добірка з очікуваним результатом | Перевірка злиття, оновлень, джерел і стилю, далі розширення до корпусу v4 |
| Параметри Linux-сервера, backup і аварійних сповіщень | Deployment, виміри й recovery цілі |

Для старту офлайн-розробки цього чекати не потрібно. Основний стек достатній,
процес продукту визначений; наступний результат — каркас і виконувані схеми.
Наявність документації сама собою не означає наявності готової реалізації.

## Офіційні джерела інструментів

- [OpenAI Python SDK](https://developers.openai.com/api/reference/python): async client.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs):
  структуровані результати з Pydantic/JSON Schema; схема не замінює перевірку фактів.
- [uv](https://docs.astral.sh/uv/concepts/projects/sync/): locking/sync оточення.
- [Typer](https://typer.tiangolo.com/): CLI на основі Python type hints.
- [pytest](https://docs.pytest.org/en/stable/),
  [pytest-asyncio](https://pytest-asyncio.readthedocs.io/en/stable/): тестовий стек.
- [Ruff](https://docs.astral.sh/ruff/),
  [mypy](https://mypy.readthedocs.io/en/stable/): lint/format та перевірка типів.

Ці можливості звірено 2026-11-11. Version resolution і інтеграційні перевірки
для обраного Python/SDK будуть виконані при створенні реального оточення.
